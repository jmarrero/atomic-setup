#!/usr/bin/env python3
"""The coordinator container's main loop, which its entrypoint ends in.

It starts the coordinator session in tmux on the first available backend
of BACKENDS (backends.toml: Claude Code, then opencode, by default), and
keeps `bot-poll --exclude-lead '*'` running outside it, restarting it after
every exit. It wakes the session only when there is something to handle,
by typing a short message into its tmux session (a message typed while the
session works waits its turn):

- bot-poll's NEWS: the kinds and run it names, nothing else;
- once a day at BOT_BRIEF_HOUR_UTC: a reminder to post the morning brief;
- a poll that keeps failing;
- a handoff to the next backend, when this one's usage probe reaches its
  threshold, and the takeover on the new one.

Typed text reads as the operator's own input, so it is only ever fixed text
plus bot-poll's kind names and run ids, each checked against what bot-poll
can print, and backend names from the operator's config. The news itself
(titles, comments, anything from GitHub) stays in the run's files and
`bot-poll --summary`, which the session reads as data.

Each NEWS is saved to PENDING before it is typed, and stays pending until
the session acks its run, so news a restart or a handoff interrupted is
typed again to the next session. On start, bot-poll's last NEWS report (in
its state.json) is added too if PENDING doesn't know it: bot-poll records
news as reported before it exits, and a restart can come before the loop
saw that exit.

A handoff asks the session to record its in-flight work on the board, waits
for its ack (or a timeout), stops it and every process it started, then
starts the next backend. Each new session must answer its start note
(`started`); one that doesn't within a few minutes isn't working, and its
backend is skipped for an hour. The operator overrides a probe in
MARKS_FILE, which the session can't write. Only one backend ever runs: one is started only
once neither the tmux session nor any process of an earlier one is left,
and a (re)start of the loop first stops whatever is left, whatever its
state file says.

It is a stand-in for homegit upstream's bot-supervisor and bot-poll-loop,
until our fork has them. Python's standard library only.

    bot-coordinator-loop run         run the loop (the entrypoint does)
    bot-coordinator-loop ack RUN...  mark news runs handled
    bot-coordinator-loop pending     print the unacked news runs
    bot-coordinator-loop status      print the backends and their usage
    bot-coordinator-loop started     tell the loop this new session works
    bot-coordinator-loop handed-off  tell the loop this session handed off
    bot-coordinator-loop check       check the backends config
    bot-coordinator-loop render-args print render-skills' --backend arguments
"""
import contextlib
import datetime
import errno
import fcntl
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import tomllib
import urllib.error
import urllib.request

PROG = "bot-coordinator-loop"
USAGE = f"usage: {PROG} run | ack RUN... | pending | status | started | handed-off | check | render-args"
SESSION = "coordinator"
POLL_CMD = ["bot-poll", "--exclude-lead", "*"]
# The first poll after a start sweeps at once, as `bot-poll --once` at the
# session's start used to.
FIRST_POLL_ARGS = ["--no-wait"]
# Seconds between checks of the poll, the session and the clock.
TICK = 10
# Nothing is typed before Claude Code had time to start in the session.
STARTUP_DELAY = 60
# A failed poll is restarted after BACKOFF_MIN, doubling up to BACKOFF_MAX.
BACKOFF_MIN = 30
BACKOFF_MAX = 15 * 60
# After this many failures in a row, the session is told (once per streak).
ALERT_AFTER_FAILURES = 4
# While the loop's own state keeps failing, the session is told again this
# often.
STATE_ALERT_REPEAT = 3 * 3600
DEFAULT_BRIEF_HOUR_UTC = 7
# The most news runs kept, acked ones first to go (bot-poll keeps runs for
# 3 days); acked ones are kept so that reconciling doesn't bring them back.
PENDING_MAX = 50
SUBPROCESS_TIMEOUT = 60
# How long a stopping poll gets after SIGTERM before it is killed.
POLL_STOP_TIMEOUT = 10

# bot-poll's kinds of news: KINDS in homegit's crates/bot-poll/src/lib.rs.
# A kind missing here still wakes the session, just without the kinds.
KNOWN_KINDS = frozenset((
    "health-P0", "drive", "operator", "review", "approval", "signoff", "promotion",
    "text", "notify", "coordination", "forge-review", "rebase", "health", "news",
))
# `NEWS (KIND, ...) at HHMM: RUN/*.txt`, RUN being STATE-DIR/runs/ID.
NEWS_RE = re.compile(r"NEWS \(([^()]*)\) at \d{4}: (\S+)/\*\.txt", re.ASCII)
RUN_ID_RE = re.compile(r"(hot-)?\d{8}-\d{6}-\d{3}", re.ASCII)
NO_NEWS_PREFIX = "No news"


def log(msg):
    print(f"{PROG}: {msg}", file=sys.stderr, flush=True)


def state_home():
    return os.environ.get("XDG_STATE_HOME") or os.path.join(os.path.expanduser("~"), ".local", "state")


# bot-poll's default state dir: its runs, for the messages, and its state.
POLL_STATE_DIR = os.path.join(state_home(), "bot-poll")
RUNS_DIR = os.path.join(POLL_STATE_DIR, "runs")
POLL_STATE_FILE = os.path.join(POLL_STATE_DIR, "state.json")
STATE_DIR = os.path.join(state_home(), "bot-coordinator")
POLL_STDERR = os.path.join(STATE_DIR, "bot-poll.stderr")
# The stderr of the last failed poll, which the next attempt doesn't touch.
FAILED_STDERR = os.path.join(STATE_DIR, "bot-poll.stderr.failed")
BRIEF_FILE = os.path.join(STATE_DIR, "brief-reminded")
PENDING_FILE = os.path.join(STATE_DIR, "pending.json")
LOOP_LOCK = os.path.join(STATE_DIR, "loop.lock")


# --- Backends -------------------------------------------------------------

DEFAULTS_DIR = "/usr/local/share/bot-coordinator"
# The operator's backends, else the image's.
BACKENDS_FILE = os.path.join(os.path.expanduser("~"), ".config", "bot-harness", "backends.toml")
DEFAULT_BACKENDS_FILE = os.path.join(DEFAULTS_DIR, "backends.toml")
# The rendered coordinator skill, whose SKILL.md is pointed at the active
# backend's variant (render-skills --backend writes SKILL.md.NAME).
SKILL_DIR = os.path.join(os.path.expanduser("~"), ".local", "share", "bot-skills", "skills", "coordinator")
BACKEND_STATE_FILE = os.path.join(STATE_DIR, "backend.json")
# The operator's overrides of the probes: in the config directory, which
# the container mounts read-only, so that the session can't steer itself.
MARKS_FILE = os.path.join(os.path.expanduser("~"), ".config", "bot-harness", "backend-marks.toml")
HANDOFF_ACK_FILE = os.path.join(STATE_DIR, "handoff-ack")
START_ACK_FILE = os.path.join(STATE_DIR, "started-ack")
# Set in every backend session's environment, so that its processes (its
# workers, background shells, orphans reparented to PID 1) can be found
# and stopped with it.
GENERATION_VAR = "BOT_COORDINATOR_GENERATION"
BACKEND_VAR = "BOT_COORDINATOR_BACKEND"
NAME_RE = re.compile(r"[a-z][a-z0-9-]{0,31}", re.ASCII)
VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}", re.ASCII)
DEFAULT_HANDOFF_TIMEOUT = 15 * 60
DEFAULT_MIN_DWELL = 30 * 60
# A new session that hasn't run `started` this long after it could first be
# typed into isn't working (a refused model, a dialog, no quota): its
# backend is skipped for FAILED_HOLD.
DEFAULT_START_ACK_TIMEOUT = 10 * 60
# After this many failed stops in a row the loop exits, so that the
# container's restart takes everything down.
STOP_FAILURES_MAX = 6
# How many of a backend's earlier data directories are kept (archive), and
# the most bytes they may take together.
ARCHIVE_KEEP = 5
ARCHIVE_MAX_BYTES = 2 * 1024 ** 3
# After this many failed starts in a row a backend is held like one whose
# session ended at once, and another is tried.
START_FAILURES_MAX = 3
DEFAULT_LEAVE_AT = 95.0
DEFAULT_RETURN_BELOW = 50.0
# A session that ends this soon after its start failed to start: its
# backend is skipped for FAILED_HOLD, rather than restarted forever.
FAILED_START_WINDOW = 5 * 60
FAILED_HOLD = 3600
# How long a stopping session's processes get after SIGTERM.
SESSION_STOP_TIMEOUT = 10
PROBE_TIMEOUT = 30
# Copilot's quota, as VS Code's quota display reads it (undocumented).
COPILOT_USER_PATH = "/copilot_internal/user"
# Hosts the quota is read from, by the login's domain: github.com's API, or
# a GitHub Enterprise Cloud (data residency, SUBDOMAIN.ghe.com) API at
# api.SUBDOMAIN.ghe.com. A self-hosted Enterprise Server login has no known
# quota endpoint: its token is sent nowhere, and there is no reading.
GHE_CLOUD_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.ghe\.com", re.ASCII)
HOST_RE = re.compile(r"[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?", re.ASCII)
PROBE_TYPES = {
    # type: (required keys, optional keys, default interval, default max age)
    "file": ({"path"}, {"window", "interval_minutes", "max_age_minutes"}, 60, 3600),
    "command": ({"argv"}, {"interval_minutes", "max_age_minutes"}, 600, 3600),
    "copilot": ({"auth_env"}, {"interval_minutes", "max_age_minutes"}, 600, 3600),
    "none": (set(), set(), 600, 0),
}
BACKEND_KEYS = {"name", "command", "requires_env", "env", "notes", "leave_at", "return_below", "probe",
                "proved_by_reading", "clean", "archive", "keep", "relink"}
TOP_KEYS = {"handoff_timeout_minutes", "min_dwell_minutes", "start_ack_timeout_minutes", "backend"}


class ConfigError(Exception):
    pass


class Missing(Exception):
    """An unset variable that a command or environment needs."""


def expand(text, environ):
    """TEXT with ${VAR} and ${VAR:-DEFAULT} from ENVIRON; Missing if a
    variable without a default is unset or empty."""
    def sub(m):
        value = environ.get(m.group(1), "")
        if value:
            return value
        if m.group(2) is not None:
            return m.group(2)
        raise Missing(m.group(1))
    return VAR_RE.sub(sub, text)


class Probe:
    def __init__(self, spec, where):
        if not isinstance(spec, dict) or spec.get("type") not in PROBE_TYPES:
            raise ConfigError(f"{where}: probe.type must be one of {', '.join(sorted(PROBE_TYPES))}")
        self.type = spec["type"]
        required, optional, interval, max_age = PROBE_TYPES[self.type]
        keys = set(spec) - {"type"}
        if required - keys or keys - required - optional:
            raise ConfigError(f"{where}: a {self.type} probe takes {sorted(required)} "
                              f"(and optionally {sorted(optional)}), not {sorted(keys)}")
        self.path = os.path.expanduser(spec.get("path", ""))
        self.window = spec.get("window")
        self.argv = spec.get("argv")
        self.auth_env = spec.get("auth_env")
        if self.argv is not None and not (isinstance(self.argv, list) and self.argv
                                          and all(isinstance(a, str) for a in self.argv)):
            raise ConfigError(f"{where}: probe.argv must be a list of strings")
        self.interval = minutes(spec, "interval_minutes", interval, where)
        self.max_age = minutes(spec, "max_age_minutes", max_age, where)


def minutes(spec, key, default, where):
    value = spec.get(key)
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        raise ConfigError(f"{where}: {key} must be a number of minutes")
    return value * 60


def percent(spec, key, default, where):
    value = spec.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 100:
        raise ConfigError(f"{where}: {key} must be a percent, 0 to 100")
    return float(value)


class Backend:
    def __init__(self, spec, where, base_dirs):
        if not isinstance(spec, dict):
            raise ConfigError(f"{where} must be a table")
        unknown = set(spec) - BACKEND_KEYS
        if unknown:
            raise ConfigError(f"{where}: unknown keys {sorted(unknown)}")
        self.name = spec.get("name")
        if not isinstance(self.name, str) or not NAME_RE.fullmatch(self.name):
            raise ConfigError(f"{where}: name must match {NAME_RE.pattern}")
        where = f"backend {self.name}"
        self.command = spec.get("command")
        if not (isinstance(self.command, list) and self.command and isinstance(self.command[0], str)
                and all(isinstance(a, str) or (isinstance(a, list) and a and all(isinstance(x, str) for x in a))
                        for a in self.command)):
            raise ConfigError(f"{where}: command must be a list of strings (and lists of strings, "
                              f"optional groups), starting with the program")
        self.requires_env = spec.get("requires_env", [])
        self.env = spec.get("env", {})
        if not (isinstance(self.requires_env, list) and all(isinstance(v, str) for v in self.requires_env)):
            raise ConfigError(f"{where}: requires_env must be a list of variable names")
        if not (isinstance(self.env, dict) and all(isinstance(v, str) for v in self.env.values())
                and all(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", k) for k in self.env)):
            raise ConfigError(f"{where}: env must be a table of strings")
        self.notes = None
        if spec.get("notes") is not None:
            if not isinstance(spec["notes"], str):
                raise ConfigError(f"{where}: notes must be a file name")
            paths = [os.path.join(d, os.path.expanduser(spec["notes"])) for d in base_dirs]
            self.notes = next((p for p in paths if os.path.isfile(p)), None)
            if self.notes is None:
                raise ConfigError(f"{where}: notes file {spec['notes']!r} is in none of {base_dirs}")
        self.clean = spec.get("clean", [])
        self.archive = spec.get("archive", [])
        self.keep = spec.get("keep", [])
        for key in ("clean", "archive", "keep"):
            value = getattr(self, key)
            if not (isinstance(value, list) and all(isinstance(v, str) and v for v in value)):
                raise ConfigError(f"{where}: {key} must be a list of paths")
        self.relink = spec.get("relink", {})
        if not (isinstance(self.relink, dict) and all(isinstance(v, str) and v for v in self.relink.values())):
            raise ConfigError(f"{where}: relink must be a table of link = target paths")
        self.leave_at = percent(spec, "leave_at", DEFAULT_LEAVE_AT, where)
        self.return_below = percent(spec, "return_below", DEFAULT_RETURN_BELOW, where)
        if self.return_below > self.leave_at:
            raise ConfigError(f"{where}: return_below must not be above leave_at")
        self.probe = Probe(spec.get("probe", {"type": "none"}), where)
        # Whether a reading observed after its start shows that the session
        # works (the probe reads something only a working session writes).
        self.proved_by_reading = spec.get("proved_by_reading", False)
        if not isinstance(self.proved_by_reading, bool):
            raise ConfigError(f"{where}: proved_by_reading must be true or false")
        if self.proved_by_reading and self.probe.type != "file":
            # Only a file the session itself writes can show it works; the
            # other probes' readings are taken by the loop.
            raise ConfigError(f"{where}: proved_by_reading needs a file probe")

    def argv(self, environ):
        """The command; Missing if a variable it can't do without is unset.
        A group (a nested list) is dropped when one of its variables is."""
        out = []
        for arg in self.command:
            if isinstance(arg, str):
                out.append(expand(arg, environ))
                continue
            with contextlib.suppress(Missing):
                out.extend([expand(a, environ) for a in arg])
        return out

    def session_env(self, environ):
        return {k: expand(v, environ) for k, v in self.env.items()}

    def missing(self, environ):
        """Why it can't start here (an unset variable), or None."""
        for var in self.requires_env:
            if not environ.get(var):
                return f"{var} is unset"
        try:
            self.argv(environ)
            self.session_env(environ)
        except Missing as e:
            return f"{e} is unset"
        return None


class Config:
    def __init__(self, data, base_dirs):
        if not isinstance(data, dict):
            raise ConfigError("not a table")
        unknown = set(data) - TOP_KEYS
        if unknown:
            raise ConfigError(f"unknown keys {sorted(unknown)}")
        specs = data.get("backend")
        if not isinstance(specs, list) or not specs:
            raise ConfigError("no [[backend]] entries")
        self.backends = [Backend(s, f"[[backend]] #{i + 1}", base_dirs) for i, s in enumerate(specs)]
        names = [b.name for b in self.backends]
        if len(set(names)) != len(names):
            raise ConfigError(f"backend names repeat: {names}")
        self.handoff_timeout = minutes(data, "handoff_timeout_minutes", DEFAULT_HANDOFF_TIMEOUT, "config")
        self.min_dwell = minutes(data, "min_dwell_minutes", DEFAULT_MIN_DWELL, "config")
        self.start_ack_timeout = minutes(data, "start_ack_timeout_minutes", DEFAULT_START_ACK_TIMEOUT, "config")

    def backend(self, name):
        return next((b for b in self.backends if b.name == name), None)


def config_path():
    return BACKENDS_FILE if os.path.isfile(BACKENDS_FILE) else DEFAULT_BACKENDS_FILE


def load_config(path=None):
    path = path or config_path()
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
        # Notes next to the config, else the image's.
        return Config(data, list(dict.fromkeys([os.path.dirname(os.path.abspath(path)), DEFAULTS_DIR])))
    except (OSError, tomllib.TOMLDecodeError, ConfigError) as e:
        raise ConfigError(f"{path}: {e}") from e


# --- Usage readings -------------------------------------------------------

class LoginRefused(Exception):
    """A probe's login was refused: the backend can't work until it's fixed."""


class Unsupported(Exception):
    """A probe that can't be read for this login: no reading, said once.
    CATEGORY is fixed text for status."""

    def __init__(self, message, category="no known quota endpoint for this login"):
        super().__init__(message)
        self.category = category


def copilot_user_url(enterprise):
    """Where the quota of a Copilot login for ENTERPRISE (opencode's
    enterpriseUrl: a domain or URL, None for github.com) is read; Unsupported
    if there is no known endpoint, so that its token goes nowhere else."""
    if enterprise is None or enterprise == "":
        return "https://api.github.com" + COPILOT_USER_PATH
    if not isinstance(enterprise, str):
        raise Unsupported("its enterpriseUrl isn't a string")
    host = enterprise.strip().lower()
    host = host.split("://", 1)[1] if "://" in host else host
    host = host.split("/", 1)[0]
    if not HOST_RE.fullmatch(host):
        raise Unsupported("its enterpriseUrl isn't a host name")
    if host == "github.com":
        return "https://api.github.com" + COPILOT_USER_PATH
    if GHE_CLOUD_RE.fullmatch(host):
        return f"https://api.{host}{COPILOT_USER_PATH}"
    raise Unsupported(f"no known quota endpoint for a GitHub Enterprise Server login ({host}); "
                      f"its token is sent nowhere",
                      "no known quota endpoint for a GitHub Enterprise Server login; its token is sent nowhere")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None  # a redirect fails, and nothing follows it anywhere


_OPENER = urllib.request.build_opener(_NoRedirect)


def copilot_open(req, timeout):
    return _OPENER.open(req, timeout=timeout)


class Reading:
    """A backend's usage: the percent used of its window, when that window
    resets (None: unknown) and when it was observed, in epoch seconds."""

    def __init__(self, used, resets_at, observed_at, stamped=True):
        self.used, self.resets_at, self.observed_at = used, resets_at, observed_at
        # Whether observed_at is the source's own (not the time it was read).
        self.stamped = stamped

    def __eq__(self, other):
        return isinstance(other, Reading) and \
            (self.used, self.resets_at, self.observed_at) == (other.used, other.resets_at, other.observed_at)

    def __repr__(self):
        return f"Reading({self.used}, {self.resets_at}, {self.observed_at})"


def parse_time(value):
    """Epoch seconds of an ISO 8601 time (or a number of them); ValueError."""
    if isinstance(value, bool):
        raise ValueError(f"not a time: {value!r}")
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str):
        raise ValueError(f"not a time: {value!r}")
    t = datetime.datetime.fromisoformat(value)
    if t.tzinfo is None:
        t = t.replace(tzinfo=datetime.timezone.utc)
    return t.timestamp()


def iso(t):
    if t is None:
        return None
    return datetime.datetime.fromtimestamp(t, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def reading_of(obj, now, window=None):
    """The Reading in OBJ: {"used_percent", "resets_at", "observed_at"}, or
    with WINDOW, bot-heartbeat statusline's {"observed_at", WINDOW: {...}}.
    observed_at defaults to NOW. ValueError if it isn't one."""
    if not isinstance(obj, dict):
        raise ValueError("not an object")
    w = obj.get(window) if window else obj
    if not isinstance(w, dict):
        raise ValueError(f"no {window or 'reading'}")
    used = w.get("used_percent")
    if isinstance(used, bool) or not isinstance(used, (int, float)) or not 0 <= used <= 1000:
        raise ValueError(f"bad used_percent {used!r}")
    resets = w.get("resets_at")
    observed = obj.get("observed_at", w.get("observed_at"))
    return Reading(float(used), None if resets is None else parse_time(resets),
                   now if observed is None else parse_time(observed), stamped=observed is not None)


def copilot_reading(data, now):
    """The premium requests' Reading in Copilot's copilot_internal/user."""
    snap = data["quota_snapshots"]["premium_interactions"]
    if snap.get("unlimited") is True:
        used = 0.0
    else:
        remaining = snap["percent_remaining"]
        if isinstance(remaining, bool) or not isinstance(remaining, (int, float)):
            raise ValueError(f"bad percent_remaining {remaining!r}")
        used = max(0.0, 100.0 - float(remaining))
    resets = data.get("quota_reset_date_utc")
    return Reading(used, None if resets is None else parse_time(resets), now)


def probe_error(e):
    """A failed probe's error, as fixed text: no server's words in it."""
    if isinstance(e, LoginRefused):
        return "login refused (HTTP 401)"
    if isinstance(e, Unsupported):
        return e.category
    if isinstance(e, urllib.error.HTTPError):
        return f"HTTP {int(e.code)}"
    if isinstance(e, (TimeoutError, subprocess.TimeoutExpired)):
        return "timed out"
    if isinstance(e, urllib.error.URLError):
        return "unreachable"
    if isinstance(e, subprocess.CalledProcessError):
        return f"command failed (exit {int(e.returncode)})"
    if isinstance(e, (ValueError, KeyError, TypeError)):
        return "unexpected response"
    if isinstance(e, OSError):
        return "unreadable"
    return "failed"


def read_probe(probe, now, environ):
    """A probe's Reading, or None for none. Raises on a failed probe."""
    if probe.type == "none":
        return None
    if probe.type == "file":
        try:
            with open(probe.path) as f:
                return reading_of(json.load(f), now, probe.window)
        except FileNotFoundError:
            return None
    if probe.type == "command":
        out = subprocess.run(probe.argv, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                             timeout=PROBE_TIMEOUT, check=True).stdout
        return reading_of(json.loads(out), now)
    # copilot: the login is opencode's auth.json, {"github-copilot": {"refresh":
    # TOKEN, "enterpriseUrl": DOMAIN (an Enterprise login only), ...}}.
    login = json.loads(environ[probe.auth_env])["github-copilot"]
    url = copilot_user_url(login.get("enterpriseUrl"))
    token = login["refresh"]
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": PROG})
    # Never sent on to a redirect (which isn't followed anyway).
    req.add_unredirected_header("Authorization", f"token {token}")
    try:
        with copilot_open(req, PROBE_TIMEOUT) as r:
            return copilot_reading(json.load(r), now)
    except urllib.error.HTTPError as e:
        if e.code == 401:
            # Revoked or expired: opencode would only wait in its "connect
            # a provider" dialog.
            raise LoginRefused(f"the Copilot login was refused (401) by {url}") from None
        raise


# --- Switching ------------------------------------------------------------

def exhaustion(backend, reading, prev, mark, now):
    """The backend's exhaustion record after READING: {"since", "until",
    "source"} (until None: no known end), or None while it's available.

    Within a window usage only grows until the window resets, so a stale
    reading at or above leave_at still counts: it is a lower bound. Leaving
    lasts until the window resets, or until a fresh reading, observed after
    the record, is below return_below. MARK is the operator's: "available"
    clears records (and ignores readings) from before it."""
    if mark and mark.get("state") == "available":
        if prev and prev["since"] < mark["at"]:
            prev = None
        if reading and reading.observed_at <= mark["at"]:
            reading = None
    if prev and prev["until"] is not None and now >= prev["until"]:
        prev = None
    if prev and prev["source"] == "login" and reading is not None:
        prev = None  # the login works again
    if reading is not None:
        window_open = reading.resets_at is None or reading.resets_at > now
        fresh = now - reading.observed_at <= backend.probe.max_age
        if window_open and reading.used >= backend.leave_at:
            if prev and prev["source"] == "probe":
                return dict(prev, until=reading.resets_at)
            return {"since": now, "until": reading.resets_at, "source": "probe"}
        if (prev and prev["source"] == "probe" and fresh and window_open
                and reading.used < backend.return_below and reading.observed_at > prev["since"]):
            return None
    return prev


# Why a backend with an exhaustion record of that source is out.
UNAVAILABLE_WHY = {"probe": "at its threshold", "failed": "failed to start", "login": "its login was refused",
                   "silent": "didn't answer its start note"}


def marked_exhausted(mark, now):
    return bool(mark and mark.get("state") == "exhausted"
                and (mark.get("until") is None or now < mark["until"]))


def choose(names, available, active, last_switch, now, min_dwell):
    """The backend to run: the first available one in NAMES (most preferred
    first), but leave ACTIVE for a more preferred one only MIN_DWELL after
    the last switch, and stay on it when none is available. None only when
    nothing is running and nothing can start."""
    candidates = [n for n in names if available.get(n)]
    if not candidates:
        return active
    best = candidates[0]
    if active is None or best == active or not available.get(active):
        return best
    if last_switch is not None and now - last_switch < min_dwell:
        return active
    return best


def minutes_text(seconds):
    n = max(1, round(seconds / 60))
    return f"{n} minute{'s' if n != 1 else ''}"


def handoff_message(current, target, timeout, reason="threshold"):
    why = {"threshold": f"{current} reached its usage threshold",
           "unavailable": f"{current} isn't available now",
           "available": f"{target} comes first and is available again"}[reason]
    return (f"Handoff: the entrypoint is moving the coordinator from the {current} backend to "
            f"{target}, because {why}. Stop dispatching now. Record each running worker's state "
            f"on its board item, in Why: how far it got, its branch or PR, and the devspace or "
            f"agent run it uses, so that the next session can stop or reuse them. Workers stop "
            f"with this session, so don't wait for them. Then run `{PROG} handed-off` and wait; "
            f"the entrypoint stops this session within {minutes_text(timeout)} either way.")


TAKEOVER_REASONS = {
    "threshold": "which reached its usage threshold",
    "unavailable": "which isn't available now",
    "available": "because this backend is available again and comes first",
}


def takeover_message(prev, name, reason, timeout):
    return (f"Takeover: first run `{PROG} started`, which tells the entrypoint this backend works "
            f"(without that within {minutes_text(timeout)}, it moves on). It started this session on "
            f"the {name} backend, after {prev}, {TAKEOVER_REASONS[reason]}. {prev}'s workers "
            f"stopped with it: follow \"After a restart\" in the operator's notes for the In "
            f"Progress items, and see `{PROG} status`.")


def started_message(name, timeout):
    return (f"Started: first run `{PROG} started`, if you haven't, which tells the entrypoint this "
            f"{name} backend works (without that within {minutes_text(timeout)}, it moves to the "
            f"next backend).")


def silent_message(name):
    return (f"The entrypoint couldn't tell that this {name} session works (it didn't run "
            f"`{PROG} started` in time), and no other backend can take over, so it left it running. "
            f"If you read this, run `{PROG} started` now.")


def all_exhausted_message():
    return (f"Every coordinator backend is at its usage threshold, so there is none to hand off to: "
            f"dispatch nothing new and let running workers finish (see \"Capacity\" in the operator's "
            f"notes). `{PROG} status` shows when one is available again.")


def read_json(path, default):
    """PATH's JSON, DEFAULT if there is none; an unreadable file is moved
    aside, so that it can't stop every start."""
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except ValueError as e:
        bad = f"{path}.bad-{int(time.time())}"
        log(f"warning: {path} is unreadable ({e}); moved it to {bad}")
        os.replace(path, bad)
        return default


def write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=1)
    os.replace(tmp, path)


_MARK_WARNINGS = set()


def read_marks(path):
    """The operator's marks, {NAME: {"state", "at", "until"}} (epoch
    seconds), from a TOML file of [NAME] tables: state = "exhausted" (with
    an optional until) or state = "available" with at, the time it was
    given: it clears what the loop recorded before then, so it needs a
    date of its own, which editing another entry doesn't move. What doesn't
    check out is left out, logged once."""
    def warn(msg):
        if msg not in _MARK_WARNINGS:
            _MARK_WARNINGS.add(msg)
            log(f"warning: {msg}")
    try:
        with open(path, "rb") as f:
            marks = tomllib.load(f)
    except FileNotFoundError:
        return {}
    except (OSError, tomllib.TOMLDecodeError) as e:
        warn(f"ignoring {path}: {e}")
        return {}
    out = {}
    for name, m in marks.items():
        try:
            if not isinstance(m, dict) or m.get("state") not in ("exhausted", "available") \
                    or set(m) - {"state", "at", "until"}:
                raise ValueError("expected state = \"exhausted\" (and optionally until), "
                                 "or state = \"available\" and at")
            if m["state"] == "available" and m.get("at") is None:
                raise ValueError("an available mark needs at, the time it was given")
            out[name] = {"state": m["state"], "at": None if m.get("at") is None else mark_time(m["at"]),
                         "until": None if m.get("until") is None else mark_time(m["until"])}
        except ValueError as e:
            warn(f"ignoring [{name}] in {path}: {e}")
    return out


def mark_time(value):
    if isinstance(value, datetime.datetime):
        return (value if value.tzinfo else value.replace(tzinfo=datetime.timezone.utc)).timestamp()
    return parse_time(value)


def clean(paths, keep=(), relink=None, environ=None, archive=(), archive_dir=None, stamp=None):
    """Prepares a backend's files for its start:

    - ARCHIVE: each path is moved into ARCHIVE_DIR as STAMP-NAME (only the
      ARCHIVE_KEEP newest are kept there), and made afresh;
    - PATHS: each is emptied (a directory) or removed (a file);
    - KEEP: full paths of regular files (not directories or symlinks)
      that survive either: kept in place, or copied back from the archive;
    - RELINK: {link: target} links, made afresh whatever was there.

    Nothing follows symlinks. Paths take ${VAR:-default} from ENVIRON
    (XDG_DATA_HOME, say) and ~. Raises OSError, naming what was left, if
    anything couldn't be done: what a session left there must not reach
    the next one."""
    environ = os.environ if environ is None else environ
    path_of = lambda p: os.path.expanduser(expand(p, environ))  # noqa: E731
    keep = {path_of(k) for k in keep}
    errors = []

    def kept(path):
        return path in keep and os.path.isfile(path) and not os.path.islink(path)

    def remove(path):
        try:
            if os.path.isdir(path) and not os.path.islink(path):
                shutil.rmtree(path)
            else:
                os.unlink(path)
        except FileNotFoundError:
            pass
        except OSError as e:
            errors.append(f"{path}: {e.strerror or e}")
    for path in map(path_of, archive):
        # Kept files a killed cleanup left staged: back into the live
        # directory, unless it has its own (then the staged copy goes), so
        # that no stray copy of the login survives.
        staging = os.path.join(os.path.dirname(path), f".{os.path.basename(path)}.keep")
        kept_names = {os.path.basename(k) for k in keep if os.path.dirname(k) == path}
        try:
            # Half copies of kept files a killed restore left in the live
            # directory (move_file's NAME.tmp-PID): not for the archive.
            if os.path.isdir(path) and not os.path.islink(path):
                for name in os.listdir(path):
                    if any(name.startswith(f"{k}.tmp-") for k in kept_names):
                        remove(os.path.join(path, name))
            if os.path.lexists(staging) and (os.path.islink(staging) or not os.path.isdir(staging)):
                # Not what a cleanup leaves: in the way, so it goes.
                log(f"warning: {staging} isn't a directory; removing it")
                os.unlink(staging)
            elif os.path.isdir(staging):
                os.makedirs(path, exist_ok=True)
                for name in os.listdir(staging):
                    entry = os.path.join(staging, name)
                    # Only the kept files come back: a half copy
                    # (NAME.tmp-PID) or anything else is dropped.
                    if name not in kept_names or os.path.lexists(os.path.join(path, name)) \
                            or os.path.islink(entry) or not os.path.isfile(entry):
                        remove(entry)
                    else:
                        move_file(entry, os.path.join(path, name))
                os.rmdir(staging)
        except OSError as e:
            errors.append(f"{staging}: {e.strerror or e}")
            continue
        if not os.path.lexists(path):
            continue
        try:
            # Nothing but what is kept (a fresh one, after a failed start):
            # not worth an archive, which would push out a real one.
            if os.path.isdir(path) and not os.path.islink(path) and \
                    all(kept(os.path.join(path, n)) for n in os.listdir(path)):
                continue
        except OSError as e:
            errors.append(f"{path}: {e.strerror or e}")
            continue
        staged = False
        try:
            # The kept files (the login) leave first, so that no archive
            # holds them, and come back whatever happens next (or at the
            # next cleanup, if this one is killed).
            saved = [k for k in keep if os.path.dirname(k) == path and kept(k)]
            if saved:
                os.makedirs(staging, mode=0o700)
                staged = True
                for k in saved:
                    move_file(k, os.path.join(staging, os.path.basename(k)))
            os.makedirs(archive_dir, mode=0o700, exist_ok=True)
            prune_archive(archive_dir, tree_size(path))
            # A rename, or a copy and delete across file systems.
            shutil.move(path, os.path.join(archive_dir, f"{stamp}-{os.path.basename(path)}"))
        except OSError as e:
            errors.append(f"{path}: {e.strerror or e}")
        finally:
            if staged:
                try:
                    os.makedirs(path, exist_ok=True)
                    for name in os.listdir(staging):
                        move_file(os.path.join(staging, name), os.path.join(path, name))
                    os.rmdir(staging)
                except OSError as e:
                    errors.append(f"{path}: the login wasn't restored, it is in {staging}: {e.strerror or e}")
        with contextlib.suppress(FileExistsError):
            os.makedirs(path)
    for path in map(path_of, paths):
        try:
            if os.path.isdir(path) and not os.path.islink(path):
                for name in os.listdir(path):
                    if not kept(os.path.join(path, name)):
                        remove(os.path.join(path, name))
            elif not kept(path):
                remove(path)
        except OSError as e:
            errors.append(f"{path}: {e.strerror or e}")
    for link, target in (relink or {}).items():
        link = path_of(link)
        try:
            remove(link)
            os.makedirs(os.path.dirname(link), exist_ok=True)
            os.symlink(path_of(target), link)
        except OSError as e:
            errors.append(f"{link}: {e.strerror or e}")
    if errors:
        raise OSError("couldn't clean " + "; ".join(errors))


def move_file(src, dst):
    """Moves the file SRC to DST: a rename, or across file systems a copy to
    a temporary name, renamed into place, and only then SRC's removal, so
    that a failed copy never leaves DST half written or SRC gone."""
    try:
        os.rename(src, dst)
        return
    except OSError as e:
        if e.errno != errno.EXDEV:
            raise
    tmp = f"{dst}.tmp-{os.getpid()}"
    try:
        shutil.copy2(src, tmp)
        os.replace(tmp, dst)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    os.unlink(src)


def tree_size(path):
    """The bytes of the files under PATH, not following symlinks."""
    if not os.path.isdir(path) or os.path.islink(path):
        return os.lstat(path).st_size
    total = 0
    for root, _, files in os.walk(path):
        for name in files:
            with contextlib.suppress(OSError):
                total += os.lstat(os.path.join(root, name)).st_size
    return total


def prune_archive(archive_dir, incoming):
    """Makes room in ARCHIVE_DIR for an entry of INCOMING bytes: the oldest
    go first, until fewer than ARCHIVE_KEEP remain and they and it fit in
    ARCHIVE_MAX_BYTES (an entry bigger than that alone is still kept)."""
    entries = sorted(os.listdir(archive_dir))
    sizes = {e: tree_size(os.path.join(archive_dir, e)) for e in entries}
    while entries and (len(entries) >= ARCHIVE_KEEP or sum(sizes[e] for e in entries) + incoming > ARCHIVE_MAX_BYTES):
        old = os.path.join(archive_dir, entries.pop(0))
        if os.path.isdir(old) and not os.path.islink(old):
            shutil.rmtree(old)
        else:
            os.unlink(old)


def activate_skill(skill_dir, name):
    """Points the rendered coordinator skill at NAME's variant, if there is one."""
    variant = f"SKILL.md.{name}"
    if not os.path.isfile(os.path.join(skill_dir, variant)):
        log(f"warning: no {variant} in {skill_dir}; the coordinator skill has no notes for {name}")
        return
    # A copy, replaced at once, rather than a symlink for each tool to follow.
    tmp = os.path.join(skill_dir, "SKILL.md.tmp")
    shutil.copyfile(os.path.join(skill_dir, variant), tmp)
    os.replace(tmp, os.path.join(skill_dir, "SKILL.md"))


class StopFailed(Exception):
    """The old session can't be stopped: the loop exits."""


class Switch:
    """Which backend runs the coordinator session, and the handoffs between
    them. Its state is in STATE_FILE, written before each step it records
    is taken: phase "running" (ACTIVE runs), "handoff" (ACTIVE was asked to
    hand off to TARGET), "stopping" (ACTIVE is being stopped) or "starting"
    (TARGET is being started).

    tick() returns "running" when the session runs and nothing is being
    switched, "busy" while switching, or "ended" when the session ended by
    itself, which ends the loop (and restarts the container)."""

    def __init__(self, config, *, sessions, environ, type_message, state_file, marks_file, ack_file,
                 skill_dir, start_ack_file=None, read_probe=read_probe,
                 on_started=lambda prev, name, reason, now: None):
        self.config = config
        self.start_ack_file = start_ack_file or os.path.join(os.path.dirname(ack_file), "started-ack")
        # Where the archive paths of a backend's earlier sessions go.
        self.archive_root = os.path.join(os.path.dirname(state_file), "archive")
        self.sessions = sessions
        self.environ = environ
        self.type_message = type_message
        self.state_file, self.marks_file, self.ack_file = state_file, marks_file, ack_file
        self.skill_dir = skill_dir
        self.read_probe = read_probe
        self.on_started = on_started
        self.names = [b.name for b in config.backends]
        self.missing = {b.name: b.missing(environ) for b in config.backends}
        self.state = None
        self.recovered = False
        self.readings = {}
        self.probed_at = {}
        self.probe_errors = {}
        self.probe_details = {}
        self.available = {}
        self.handoff_typed = False
        self.all_exhausted_told = False
        self.dirty = False
        # The last error saving the state, while it lasts.
        self.save_error = None
        # Failed stops in a row.
        self.stop_failures = 0
        # Whether the running session answered its start note, and whether
        # its silence was dealt with.
        self.start_acked = False
        self.silent_handled = False
        # Failed starts in a row, by backend, and those that failed
        # START_FAILURES_MAX times in this loop.
        self.start_failures = {}
        self.gave_up = set()
        # The probe's observed_at when the session started (proved_alive).
        self.start_observed = None

    # State, written before the step it records.

    def load(self):
        state = read_json(self.state_file, {})
        state = state if isinstance(state, dict) else {}
        exhausted = state.get("exhausted")
        exhausted = {n: e for n, e in (exhausted.items() if isinstance(exhausted, dict) else [])
                     if n in self.names and isinstance(e, dict) and isinstance(e.get("since"), (int, float))
                     and (e.get("until") is None or isinstance(e.get("until"), (int, float)))
                     and isinstance(e.get("source"), str)}
        num = lambda k: state.get(k) if isinstance(state.get(k), (int, float)) else None  # noqa: E731
        name = lambda k: state.get(k) if state.get(k) in self.names else None  # noqa: E731
        return {"version": 1, "phase": state.get("phase") if isinstance(state.get("phase"), str) else None,
                "active": name("active"), "target": name("target"),
                "generation": state.get("generation") if isinstance(state.get("generation"), int) else 0,
                "phase_since": num("phase_since"), "started_at": num("started_at"),
                "last_switch": num("last_switch"), "exhausted": exhausted, "view": {}}

    def save(self):
        self.write(self.state)
        self.dirty = False

    def write(self, state):
        try:
            write_json(self.state_file, state)
        except OSError as e:
            if self.save_error != repr(e):
                log(f"warning: can't save {self.state_file} (retrying every tick): {e}")
            self.save_error = repr(e)
            raise
        self.save_error = None

    def change(self, **fields):
        """Records FIELDS, saved first: raises (changing nothing) if it can't."""
        new = dict(self.state, **fields)
        self.write(new)
        self.state, self.dirty = new, False

    # Probes.

    def refresh(self, now):
        """Reads the probes that are due, and updates who is available."""
        marks = read_marks(self.marks_file)
        view = {}
        for b in self.config.backends:
            refused = False
            if now - self.probed_at.get(b.name, -1e18) >= b.probe.interval:
                self.probed_at[b.name] = now
                try:
                    self.readings[b.name] = self.read_probe(b.probe, now, self.environ)
                    self.probe_errors.pop(b.name, None)
                except Exception as e:  # noqa: BLE001 - a failed probe is no reading
                    # The detail (which may hold a server's text) only in the
                    # log; status shows a fixed category.
                    if self.probe_errors.get(b.name) != probe_error(e) or self.probe_details.get(b.name) != repr(e):
                        log(f"warning: the {b.name} probe failed ({probe_error(e)}; no reading until it works): {e!r}")
                    self.probe_errors[b.name] = probe_error(e)
                    self.probe_details[b.name] = repr(e)
                    self.readings[b.name] = None
                    refused = isinstance(e, LoginRefused)
            reading = self.readings.get(b.name)
            prev = self.state["exhausted"].get(b.name)
            new = exhaustion(b, reading, prev, marks.get(b.name), now)
            if refused and not (new and new["source"] != "login"
                                and (new["until"] is None or new["until"] >= now + FAILED_HOLD)):
                # Held for a while, renewed while it's refused, and ended by
                # the first reading that works (unless a longer hold stands).
                new = {"since": now, "until": now + FAILED_HOLD, "source": "login"}
            if new != prev:
                if new is None:
                    self.state["exhausted"].pop(b.name)
                    log(f"{b.name} is available again")
                else:
                    self.state["exhausted"][b.name] = new
                    if prev is None:
                        log(f"{b.name} is at its threshold ({reading.used if reading else '?'}%)"
                            f" until {iso(new['until']) or 'it reads lower'}")
                self.dirty = True
            why = (self.missing[b.name] and f"can't start: {self.missing[b.name]}"
                   or (b.name in self.gave_up and "its starts kept failing")
                   or (marked_exhausted(marks.get(b.name), now) and "marked exhausted")
                   or (new and f"{UNAVAILABLE_WHY.get(new['source'], 'at its threshold')}"
                       f" until {iso(new['until']) or 'a lower reading'}"))
            self.available[b.name] = not why
            view[b.name] = {"available": not why, "why": why or None, "probe_error": self.probe_errors.get(b.name),
                            "used_percent": reading.used if reading else None,
                            "observed_at": iso(reading.observed_at) if reading else None,
                            "resets_at": iso(reading.resets_at) if reading else None}
        if view != self.state.get("view"):
            self.state["view"] = view
            self.dirty = True

    def choose(self, now):
        name = choose(self.names, self.available, self.state["active"], self.state["last_switch"],
                      now, self.config.min_dwell)
        if name is not None and self.available.get(name):
            return name
        # Nothing is available: one that's only used up still works, one
        # that can't start or whose login is refused doesn't.
        if name is not None and self.usable(name):
            return name
        return next((n for n in self.names if self.usable(n)), name)

    def pick(self, now):
        """The backend to start now; StopFailed (the loop exits, the
        container restarts) only when every backend can't start (its secret
        is missing, or its starts kept failing in this loop)."""
        name = self.choose(now)
        if name is not None and self.usable(name):
            return name
        # Every backend is held (its session ended at once, didn't answer,
        # or its login is refused): a coordinator that may not work beats
        # none, so the most preferred one that can start is started anyway,
        # unless its starts keep failing in this loop.
        why = "; ".join(f"{n}: {self.missing[n] or UNAVAILABLE_WHY.get(self.state['exhausted'].get(n, {}).get('source'), 'unavailable')}"
                        for n in self.names)
        fallback = next((n for n in self.names if not self.missing[n] and n not in self.gave_up), None)
        if fallback is not None:
            log(f"warning: no backend is usable ({why}); starting {fallback} anyway")
            return fallback
        log(f"ERROR: no backend can start ({why}); exiting, so that the container restarts and tries again")
        raise StopFailed()

    def usable(self, n):
        """Whether N can run: it may be used up, but it can start and work."""
        return (not self.missing[n] and n not in self.gave_up
                and self.state["exhausted"].get(n, {}).get("source") not in ("failed", "login", "silent"))

    def reason(self, name):
        """Why the session moves off NAME, for the messages."""
        if self.available.get(name):
            return "available"
        return "threshold" if self.state["exhausted"].get(name, {}).get("source") == "probe" else "unavailable"

    # The steps.

    def tick(self, now):
        if self.state is None:
            self.state = self.load()
        try:
            return self._tick(now)
        except StopFailed:
            return "ended"
        finally:
            if self.dirty:
                with contextlib.suppress(OSError):  # logged, and retried next tick
                    self.save()

    def _tick(self, now):
        if not self.recovered:
            # Whatever the state says: nothing may run but what starts here.
            if not self.stop():
                return "busy"
            self.refresh(now)
            self.start(self.pick(now), now)
            # Only now: until a start worked, each tick stops and starts again.
            self.recovered = True
            return "busy"
        phase = self.state["phase"]
        if phase == "running":
            return self.running(now)
        if phase == "handoff":
            self.handing_off(now)
        elif phase == "stopping":
            self.stopping(now)
        else:  # "starting", or anything else: stop and start again
            self.change(phase="stopping", phase_since=now)
            self.stopping(now)
        return "running" if self.state["phase"] == "running" else "busy"

    def running(self, now):
        active = self.state["active"]
        if not self.sessions.alive():
            started = self.state["started_at"]
            if started is not None and now - started < FAILED_START_WINDOW:
                log(f"the {active} session ended {int(now - started)}s after its start; "
                    f"skipping {active} for {FAILED_HOLD // 60} minutes")
                self.state["exhausted"][active] = {"since": now, "until": now + FAILED_HOLD, "source": "failed"}
                self.dirty = True
            return "ended"
        self.refresh(now)
        if not self.start_acked:
            if self.acked(self.start_ack_file) or self.proved_alive(active):
                log(f"the {active} session works")
                self.start_acked = True
                if self.state["exhausted"].get(active, {}).get("source") == "silent":
                    log(f"{active} answered after all; it counts as available again")
                    del self.state["exhausted"][active]
                    self.dirty = True
                    self.refresh(now)
            elif not self.silent_handled and \
                    now >= (self.state["started_at"] or now) + STARTUP_DELAY + self.config.start_ack_timeout:
                return self.silent(now)
        target = self.choose(now)
        if target is not None and target != active:
            log(f"handing off from {active} to {target}")
            with contextlib.suppress(FileNotFoundError):
                os.remove(self.ack_file)
            self.change(phase="handoff", target=target, phase_since=now)
            self.handoff_typed = False
            self.all_exhausted_told = False
            self.handing_off(now)
            return "busy"
        everything_out = not any(self.available.values())
        if everything_out and not self.all_exhausted_told and self.typable(now):
            log("every backend is at its threshold; telling the session")
            self.all_exhausted_told = self.typed(all_exhausted_message())
        elif not everything_out:
            self.all_exhausted_told = False
        return "running"

    def proved_alive(self, name):
        """Whether NAME's own probe has a new reading since its session
        started, which only a working session writes (Claude Code's status
        line, after each response): no need to wait for its turn to end.
        New means stamped by its writer, after the start, and not the one
        that was there at the start (which a clock stepping back could make
        look newer)."""
        reading = self.readings.get(name)
        started = self.state["started_at"]
        return (self.config.backend(name).proved_by_reading and reading is not None and reading.stamped
                and started is not None and reading.observed_at > started
                and reading.observed_at != self.start_observed)

    def typable(self, now):
        """Whether the session had time to start: nothing typed before."""
        return now >= (self.state["started_at"] or now) + STARTUP_DELAY

    def typed(self, text):
        try:
            return self.type_message(text)
        except ValueError as e:
            log(f"error: dropping a message: {e}")
            return True

    def silent(self, now):
        """The session never answered its start note: it runs, but doesn't
        work (a refused model, a dialog, no quota). Its backend is skipped
        for FAILED_HOLD, and it is stopped at once, without a handoff it
        couldn't read."""
        active = self.state["active"]
        log(f"ERROR: the {active} session didn't run `{PROG} started` within "
            f"{minutes_text(self.config.start_ack_timeout)}; skipping {active} for {FAILED_HOLD // 60} minutes")
        self.state["exhausted"][active] = {"since": now, "until": now + FAILED_HOLD, "source": "silent"}
        self.dirty = True
        self.silent_handled = True  # once per session; a late ack still counts
        self.refresh(now)
        target = self.choose(now)
        if target is None or target == active:
            log(f"no other backend can run; leaving {active} running")
            self.all_exhausted_told = True  # not that message: this one
            self.typed(silent_message(active))
            return "running"
        self.change(phase="stopping", target=target, phase_since=now)
        self.stopping(now)
        return "running" if self.state["phase"] == "running" else "busy"

    def acked(self, path=None):
        """Whether PATH acks this generation: exactly its number, nothing
        else (an empty or half-written file is no ack)."""
        try:
            with open(path or self.ack_file) as f:
                gen = f.read().strip()
        except FileNotFoundError:
            return False
        return gen == str(self.state["generation"])

    def handing_off(self, now):
        # The timeout runs from the handoff's start, typed or not.
        if not self.handoff_typed and self.typable(now):
            self.handoff_typed = self.typed(handoff_message(self.state["active"], self.state["target"],
                                                            self.config.handoff_timeout,
                                                            self.reason(self.state["active"])))
        why = ("it acked" if self.acked() else "it ended" if not self.sessions.alive()
               else "the handoff timed out" if now - self.state["phase_since"] >= self.config.handoff_timeout
               else None)
        if why is None:
            return
        log(f"stopping the {self.state['active']} session: {why}")
        self.change(phase="stopping", phase_since=now)
        self.stopping(now)

    def stop(self):
        """Stops the session and its processes; whether none is left.
        StopFailed after STOP_FAILURES_MAX failures in a row."""
        if self.sessions.stop():
            self.stop_failures = 0
            return True
        self.stop_failures += 1
        if self.stop_failures >= STOP_FAILURES_MAX:
            log(f"ERROR: the coordinator session's processes survived {self.stop_failures} stops in a row; "
                f"exiting, so that the container's restart takes them down")
            raise StopFailed()
        log("a coordinator session or its processes are still running; retrying")
        return False

    def stopping(self, now):
        if not self.stop():
            return
        # The target, unless it's no longer available (the old session
        # crashed long enough for that, say): then whatever is now.
        self.refresh(now)
        target = self.state["target"]
        self.start(target if self.available.get(target) and target not in self.gave_up else self.pick(now), now)

    def start(self, name, now):
        """Starts NAME's session, once nothing else runs (the caller made sure)."""
        backend = self.config.backend(name)
        prev = self.state["active"]
        reason = self.reason(prev)
        gen = self.state["generation"] + 1
        argv, env = backend.argv(self.environ), backend.session_env(self.environ)
        self.change(phase="starting", target=name, generation=gen, phase_since=now)
        with contextlib.suppress(FileNotFoundError):
            os.remove(self.start_ack_file)
        self.start_acked = self.silent_handled = False
        self.start_observed = None
        if backend.proved_by_reading:
            # What the probe shows now, which no longer counts as proof.
            with contextlib.suppress(Exception):
                reading = self.read_probe(backend.probe, now, self.environ)
                self.start_observed = reading.observed_at if reading is not None else None
        try:
            activate_skill(self.skill_dir, name)
        except OSError as e:
            log(f"warning: can't point the coordinator skill at {name}'s notes: {e}")
        try:
            # What a session left where the backend reads its config and
            # plugins: if it can't all go, the start fails.
            clean(backend.clean, backend.keep, backend.relink, self.environ, backend.archive,
                  os.path.join(self.archive_root, name),
                  f"{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime(now))}-gen{gen}")
            self.sessions.start(argv, dict(env, **{GENERATION_VAR: str(gen), BACKEND_VAR: name}))
        except Exception as e:
            # Stopped (in case it half started) and started again next tick:
            # after START_FAILURES_MAX in a row, on another backend.
            failures = self.start_failures[name] = self.start_failures.get(name, 0) + 1
            if failures >= START_FAILURES_MAX:
                log(f"ERROR: the {name} session failed to start {failures} times in a row ({e!r}); "
                    f"skipping {name} for {FAILED_HOLD // 60} minutes")
                self.state["exhausted"][name] = {"since": now, "until": now + FAILED_HOLD, "source": "failed"}
                self.start_failures[name] = 0
                self.gave_up.add(name)
            self.state.update(phase="stopping", target=name)
            self.dirty = True
            raise
        self.start_failures[name] = 0
        log(f"started the {name} session (generation {gen})")
        switched = prev is not None and prev != name
        self.state.update(phase="running", active=name, target=None, started_at=now, phase_since=now,
                          **({"last_switch": now} if switched else {}))
        self.dirty = True
        self.handoff_typed = False
        self.on_started(prev if switched else None, name, reason, now)

    def can_type(self, now):
        return self.state is not None and self.recovered and self.state["phase"] == "running"


class TmuxSessions:
    """The backend session in tmux, and every process it started."""

    def __init__(self, cwd):
        self.cwd = cwd

    def alive(self):
        return session_alive()

    def start(self, argv, env):
        args = ["new-session", "-d", "-s", SESSION, "-x", "200", "-y", "50", "-c", self.cwd]
        for k, v in env.items():
            args += ["-e", f"{k}={v}"]
        r = tmux(*args, shlex.join(argv))
        if r.returncode != 0:
            raise RuntimeError(f"tmux new-session failed: {r.stderr.strip()}")

    @staticmethod
    def processes():
        """The processes of any backend session: those with GENERATION_VAR."""
        marker, me, out = GENERATION_VAR.encode() + b"=", os.getpid(), []
        for pid in os.listdir("/proc"):
            if not pid.isdigit() or int(pid) == me:
                continue
            try:
                with open(f"/proc/{pid}/environ", "rb") as f:
                    env = f.read()
            except OSError:
                continue
            if any(v.startswith(marker) for v in env.split(b"\0")):
                out.append(int(pid))
        return out

    def stop(self):
        """Stops the session and its processes; whether none is left. The
        whole tmux server: a session the session started itself would run
        on it, without the generation in its environment. Also when the
        coordinator session already ended: another may outlive it."""
        with contextlib.suppress(subprocess.TimeoutExpired):
            tmux("kill-server")  # fails when there is no server, which is fine
        pids = self.processes()
        for pid in pids:
            with contextlib.suppress(OSError):
                os.kill(pid, signal.SIGTERM)
        deadline = time.monotonic() + SESSION_STOP_TIMEOUT
        while pids and time.monotonic() < deadline:
            time.sleep(0.2)
            pids = self.processes()
        for pid in pids:
            log(f"process {pid} of the old session didn't stop within {SESSION_STOP_TIMEOUT}s; killing it")
            with contextlib.suppress(OSError):
                os.kill(pid, signal.SIGKILL)
        if pids:
            time.sleep(0.5)
        return not self.alive() and not self.processes()




def valid_news(kinds, run):
    return (isinstance(kinds, list) and len(kinds) > 0
            and all(isinstance(k, str) and k in KNOWN_KINDS for k in kinds)
            and isinstance(run, str) and RUN_ID_RE.fullmatch(run) is not None)


def news_message(kinds, run):
    """The message for news of KINDS in RUN, both checked; None for news
    whose NEWS line didn't check out."""
    if kinds is None:
        return (f"bot-poll found news: run `bot-poll --summary`, read the newest run's "
                f"files in {RUNS_DIR} and handle it.")
    return (f"bot-poll found news ({', '.join(kinds)}) in run {run}: run `bot-poll --summary`, "
            f"read {RUNS_DIR}/{run}/*.txt and handle it, then `{PROG} ack {run}`.")


def restart_message(runs):
    """For news saved before a restart and never acked: RUNS, checked."""
    return (f"bot-poll news from before this restart may not have been handled: runs "
            f"{' '.join(runs)}. Read their files in {RUNS_DIR}, handle what's left, then "
            f"`{PROG} ack` each run.")


def brief_message(hour):
    return (f"Daily reminder from the entrypoint: it is past {hour:02d}:00 UTC. Post the "
            f"morning brief (see \"Morning brief\" in the coordinator skill) if you haven't today.")


def state_failing_message():
    return (f"The entrypoint can't read or save its news state (in {STATE_DIR}, and "
            f"bot-poll's state.json), so it has stopped polling until it can; it keeps "
            f"retrying, and its errors are in the container's log. Check that the disk isn't "
            f"full and both are writable.")


def switch_failing_message():
    return (f"The entrypoint can't save its backend state ({BACKEND_STATE_FILE}) or switch "
            f"backends, so it may not hand off before this backend's usage runs out; it keeps "
            f"retrying, and its errors are in the container's log. Check that the disk isn't full "
            f"and the file is writable, and watch the usage yourself meanwhile.")


def failing_message(failures, status):
    return (f"The entrypoint's bot-poll has failed {failures} times in a row (last exit status "
            f"{status}; its stderr is in {FAILED_STDERR}), so news isn't reaching you. It keeps "
            f"retrying. If a bot-poll you started holds its state dir, stop it: never start "
            f"bot-poll yourself.")


def parse_news_line(line):
    """(kinds, run) of a NEWS line, (None, None) for one that doesn't check
    out, or None if it isn't one."""
    m = NEWS_RE.fullmatch(line)
    if not m:
        return None
    kinds = [k.strip() for k in m.group(1).split(",")]
    run = os.path.basename(m.group(2))
    if valid_news(kinds, run):
        return kinds, run
    log(f"warning: unexpected NEWS line {line!r}")
    return None, None


def parse_poll_output(status, stdout):
    """What a finished poll reported: ("news", (kinds, run)), with both None
    if its NEWS line didn't check out; ("quiet", None); or ("failed", reason)."""
    if status != 0:
        return "failed", f"exit status {status}"
    lines = [line.strip() for line in stdout.splitlines() if line.strip()]
    for line in reversed(lines):
        news = parse_news_line(line)
        if news is not None:
            return "news", news
        if line.startswith(NO_NEWS_PREFIX):
            return "quiet", None
    return "failed", "neither NEWS nor 'No news' in its output"


def last_reported(path):
    """The (kinds, run) of bot-poll's last NEWS report, checked, from its
    state at PATH; None if there is none (or none that checks out). Raises
    OSError or ValueError if it can't be read: there may be one."""
    try:
        with open(path) as f:
            state = json.load(f)
    except FileNotFoundError:
        return None
    last = state.get("last_news") if isinstance(state, dict) else None
    line = last.get("line") if isinstance(last, dict) else None
    news = parse_news_line(line) if isinstance(line, str) else None
    return news if news and news[0] is not None else None


def brief_due(now, hour, last_day):
    """The UTC day (YYYY-MM-DD) to record if the brief reminder is due at
    NOW (seconds), given the day it was last typed on; else None."""
    t = datetime.datetime.fromtimestamp(now, datetime.timezone.utc)
    today = t.date().isoformat()
    return today if t.hour >= hour and last_day != today else None


class Pending:
    """The news runs, [{"kinds": [...], "run": ID, "acked": bool}], in a
    JSON file that the loop adds to and `ack` marks, under a lock."""

    def __init__(self, path):
        self.path = path

    @contextlib.contextmanager
    def _locked(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path + ".lock", "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield

    def _read(self):
        try:
            with open(self.path) as f:
                entries = json.load(f)
            if not isinstance(entries, list):
                raise ValueError("not a list")
        except FileNotFoundError:
            return []
        except ValueError as e:
            # Moved aside, so that a bad file can't stop every start.
            bad = f"{self.path}.bad-{int(time.time())}"
            log(f"warning: {self.path} is unreadable ({e}); moved it to {bad}")
            os.replace(self.path, bad)
            return []
        # Checked again: only what passes is ever typed.
        return [{"kinds": e["kinds"], "run": e["run"], "acked": e.get("acked") is True}
                for e in entries if isinstance(e, dict) and valid_news(e.get("kinds"), e.get("run"))]

    def _write(self, entries):
        excess = len(entries) - PENDING_MAX
        if excess > 0:
            acked = [i for i, e in enumerate(entries) if e["acked"]][:excess]
            entries = [e for i, e in enumerate(entries) if i not in acked][-PENDING_MAX:]
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(entries, f)
        os.replace(tmp, self.path)

    def entries(self):
        """The unacked runs, oldest first."""
        with self._locked():
            return [e for e in self._read() if not e["acked"]]

    def create(self):
        """Creates the file, empty, if there is none."""
        with self._locked():
            if not os.path.exists(self.path):
                self._write([])

    def add(self, kinds, run, *, acked=False):
        """Records RUN, unless it is known (acked or not); whether it was new."""
        with self._locked():
            entries = self._read()
            if any(e["run"] == run for e in entries):
                return False
            self._write(entries + [{"kinds": kinds, "run": run, "acked": acked}])
            return True

    def ack(self, runs):
        """Marks RUNS handled; the ones that weren't pending."""
        with self._locked():
            entries = self._read()
            pending = {e["run"] for e in entries if not e["acked"]}
            for e in entries:
                if e["run"] in runs:
                    e["acked"] = True
            self._write(entries)
            return sorted(set(runs) - pending)


class Loop:
    """One container's loop; the clock and the outside world are passed in,
    so tests can drive it. step() is one tick; it returns False once the
    session ended by itself. type_message returns whether it typed. SWITCH
    starts the session and switches backends; nothing else happens until
    STARTUP_DELAY after the first start, and nothing is typed until that
    long after each start."""

    def __init__(self, now, *, spawn, type_message, switch, pending, last_reported,
                 brief_hour, brief_file):
        self.spawn = spawn
        self.type_message = type_message
        self.switch = switch
        switch.on_started = self.backend_started
        self.pending = pending
        self.last_reported = last_reported
        self.brief_hour = brief_hour
        self.brief_file = brief_file
        # Polling starts STARTUP_DELAY after the first start, typing that
        # long after each start.
        self.ready_at = None
        self.type_after = None
        self.now = now
        # The start's two steps, each retried until it succeeds: adding
        # bot-poll's last report if unknown, and retyping the unacked runs.
        self.reconciled = False
        self.replayed = False
        # News from a finished poll not saved to pending yet:
        # [kinds, run, typed].
        self.unsaved = []
        self.errors = {}
        # Ticks in a row in which the loop's own state failed, and when
        # the session was last told.
        self.state_failures = 0
        self.state_alerted = None
        # The same for switching backends.
        self.switch_failures = 0
        self.switch_alerted = None
        # The day of the last brief reminder, should its file fail.
        self.brief_day = None
        self.polls = 0
        self.poll = None
        self.poll_started = 0
        self.next_poll_at = 0
        self.failures = 0
        # Messages not typed yet, retried every tick: [kind, text].
        self.outbox = []

    def backend_started(self, prev, name, reason, now):
        """A new session: what was meant for the old one goes to it."""
        if self.ready_at is None:
            self.ready_at = now + STARTUP_DELAY
        self.type_after = now + STARTUP_DELAY
        # The unacked news is typed again (replay), and news not saved yet
        # is typed with its save; an old session's start note is stale;
        # other messages wait for the new session.
        self.outbox = [m for m in self.outbox if m[0] not in ("news", "start")]
        for item in self.unsaved:
            item[2] = False
        self.replayed = False
        # A line half typed into the old session isn't finished in this one.
        if hasattr(self.type_message, "without_enter"):
            self.type_message.without_enter = None
        timeout = self.switch.config.start_ack_timeout
        if prev is not None:
            self.say(takeover_message(prev, name, reason, timeout), "start")
        else:
            self.say(started_message(name, timeout), "start")

    def step(self, now):
        self.now = now
        try:
            if self.switch.tick(now) == "ended":
                return False
        except Exception as e:  # noqa: BLE001 - logged, and retried next tick
            if self.errors.get("switch") != repr(e):
                log(f"error in switching backends (retrying every tick): {e!r}")
            self.errors["switch"] = repr(e)
        else:
            self.errors.pop("switch", None)
        if self.ready_at is None or now < self.ready_at:
            return True
        # Each part on its own: one failing doesn't stop the others, or the
        # container (which would take the session's workers with it).
        state_failed = False
        for check in (self.reconcile, self.replay, self.save_news, self.check_poll,
                      self.check_brief, self.flush):
            try:
                check(now)
                self.errors.pop(check.__name__, None)
            except Exception as e:  # noqa: BLE001 - logged, and retried next tick
                # Logged once while it keeps failing the same way.
                if self.errors.get(check.__name__) != repr(e):
                    log(f"error in {check.__name__} (retrying every tick): {e!r}")
                self.errors[check.__name__] = repr(e)
                state_failed |= check in (self.reconcile, self.replay, self.save_news)
        self.check_state(now, state_failed)
        self.check_switch(now)
        return True

    def check_switch(self, now):
        """Tells the session when switching backends keeps failing (its
        state can't be saved, say): it wouldn't be handed off in time."""
        if "switch" not in self.errors and self.switch.save_error is None:
            self.switch_failures, self.switch_alerted = 0, None
            return
        self.switch_failures += 1
        if self.switch_failures >= ALERT_AFTER_FAILURES and (
                self.switch_alerted is None or now - self.switch_alerted >= STATE_ALERT_REPEAT):
            log("switching backends keeps failing; telling the session")
            self.switch_alerted = now
            self.say(switch_failing_message())

    def check_state(self, now, failed):
        """Tells the session when the loop's own state keeps failing, which
        stops polling: otherwise that would look like a quiet day."""
        if not failed:
            self.state_failures, self.state_alerted = 0, None
            return
        self.state_failures += 1
        if self.state_failures < ALERT_AFTER_FAILURES:
            return
        if self.state_alerted is None or now - self.state_alerted >= STATE_ALERT_REPEAT:
            log("the loop's state keeps failing; telling the session")
            self.state_alerted = now
            self.say(state_failing_message())

    def say(self, message, kind="other"):
        self.outbox.append([kind, message])
        self.flush()

    def flush(self, now=None):
        now = self.now if now is None else now
        # Not into a session being handed off, nor one still starting.
        if not self.switch.can_type(now) or self.type_after is None or now < self.type_after:
            return
        while self.outbox:
            try:
                if not self.type_message(self.outbox[0][1]):
                    return
            except ValueError as e:
                log(f"error: dropping a message: {e}")
            self.outbox.pop(0)

    def reconcile(self, now):
        """Adds bot-poll's last NEWS report if pending doesn't know it:
        bot-poll records news as reported before it exits, and a restart
        can come before the loop saw that exit. Unknown news counts as not
        handled, also on a first start, when it may have been: a stale wake
        is cheap, lost news isn't."""
        if self.reconciled:
            return
        self.pending.create()
        news = self.last_reported()
        if news is not None and self.pending.add(*news):
            log(f"bot-poll reported run {news[1]} without the loop seeing it; added it")
            # A replay that already ran didn't have it.
            if self.replayed:
                self.say(news_message(*news), "news")
        self.reconciled = True

    def replay(self, now):
        """Retypes the runs not acked before the restart."""
        if self.replayed:
            return
        leftover = [e["run"] for e in self.pending.entries()]
        if leftover:
            log(f"{len(leftover)} news run(s) from before the restart are unacked; telling the session")
            self.say(restart_message(leftover), "news")
        self.replayed = True

    def save_news(self, now):
        """Types the news of finished polls, then saves it to pending; what
        can't be saved yet stays here, to be tried again next tick. Typing
        doesn't wait for saving, which only matters across restarts."""
        for item in self.unsaved:
            kinds, run, typed = item
            if not typed:
                self.say(news_message(kinds, run), "news")
                item[2] = True
        while self.unsaved:
            kinds, run, _ = self.unsaved[0]
            if run is not None:
                self.pending.add(kinds, run)
            self.unsaved.pop(0)

    def check_poll(self, now):
        # No new poll before its predecessors' news is safe: its report
        # replaces bot-poll's last one, which reconciling recovers from.
        if not self.reconciled or self.unsaved:
            return
        if self.poll is not None:
            status = self.poll.poll()
            if status is None:
                return
            poll, self.poll = self.poll, None
            self.finished(now, status, poll.stdout())
        if now >= self.next_poll_at and not self.unsaved:
            args = POLL_CMD + (FIRST_POLL_ARGS if self.polls == 0 else [])
            self.polls += 1
            self.poll_started = now
            try:
                self.poll = self.spawn(args)
            except OSError as e:
                log(f"cannot start bot-poll: {e}")
                self.failed(now, now, "none, it didn't start")

    def finished(self, now, status, stdout):
        outcome, detail = parse_poll_output(status, stdout)
        if outcome == "failed":
            log(f"bot-poll failed ({detail}); its stderr is in {FAILED_STDERR}")
            with contextlib.suppress(OSError):
                shutil.copyfile(POLL_STDERR, FAILED_STDERR)
            self.failed(now, self.poll_started, status)
            return
        self.failures = 0
        self.next_poll_at = now
        if outcome == "quiet":
            log("bot-poll found no news; restarting it")
            return
        kinds, run = detail
        log(f"bot-poll found news ({', '.join(kinds or ['?'])}); telling the session")
        self.unsaved.append([*detail, False])
        self.save_news(now)

    def failed(self, now, started, status):
        # A poll that ran for long before failing starts a new streak.
        if now - started > BACKOFF_MAX:
            self.failures = 0
        self.failures += 1
        delay = min(BACKOFF_MAX, BACKOFF_MIN * 2 ** (self.failures - 1))
        self.next_poll_at = now + delay
        log(f"restarting bot-poll in {delay}s (failure {self.failures} in a row)")
        if self.failures == ALERT_AFTER_FAILURES:
            self.say(failing_message(self.failures, status))

    def check_brief(self, now):
        last = self.brief_day
        if last is None:
            try:
                with open(self.brief_file) as f:
                    last = f.read().strip()
            except FileNotFoundError:
                pass
            except OSError as e:
                log(f"warning: can't read {self.brief_file}: {e}")
        day = brief_due(now, self.brief_hour, last)
        if day is None:
            return
        log("reminding the session of the morning brief")
        # Recorded first: a crash in between skips one reminder rather than
        # repeating it on every start. Should the file fail, it is still
        # once a day while this loop runs.
        self.brief_day = day
        try:
            os.makedirs(os.path.dirname(self.brief_file), exist_ok=True)
            with open(self.brief_file, "w") as f:
                f.write(day + "\n")
        except OSError as e:
            log(f"warning: can't record the brief reminder in {self.brief_file}: {e}")
        self.say(brief_message(self.brief_hour))

    def stop(self):
        if self.poll is not None:
            self.poll.stop()


class Poll:
    """A running bot-poll: its stdout to a temporary file (it prints a line
    or two), its stderr to POLL_STDERR, for the session to read."""

    def __init__(self, args):
        os.makedirs(STATE_DIR, exist_ok=True)
        self.out = tempfile.TemporaryFile(mode="w+")
        with open(POLL_STDERR, "w") as err:
            self.proc = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=self.out, stderr=err)

    def poll(self):
        return self.proc.poll()

    def stdout(self):
        self.out.seek(0)
        text = self.out.read()
        self.out.close()
        return text

    def stop(self):
        self.proc.terminate()
        try:
            self.proc.wait(POLL_STOP_TIMEOUT)
        except subprocess.TimeoutExpired:
            log(f"bot-poll didn't stop within {POLL_STOP_TIMEOUT}s; killing it")
            self.proc.kill()
            self.proc.wait()
        self.out.close()


def tmux(*args):
    return subprocess.run(["tmux", *args], stdin=subprocess.DEVNULL, capture_output=True,
                          text=True, timeout=SUBPROCESS_TIMEOUT)


def session_alive():
    try:
        return tmux("has-session", "-t", SESSION).returncode == 0
    except subprocess.TimeoutExpired:
        log("warning: tmux has-session timed out; taking the session as alive")
        return True


class Typist:
    """Types a line and Enter into the session. Called again with the same
    line after only its Enter failed, it retries just the Enter."""

    def __init__(self):
        self.without_enter = None

    def __call__(self, text):
        """Whether TEXT and Enter went in."""
        # One line: a newline would send what precedes it on its own.
        if not text.isprintable():
            raise ValueError(f"refusing to type a message with control characters: {text!r}")
        try:
            if self.without_enter != text:
                self.without_enter = None
                # A timeout after tmux typed the text types it again on the
                # retry; rare enough to accept.
                if tmux("send-keys", "-t", SESSION, "-l", text).returncode != 0:
                    return self.failed()
                self.without_enter = text
                time.sleep(1)
            if tmux("send-keys", "-t", SESSION, "Enter").returncode != 0:
                return self.failed()
        except subprocess.TimeoutExpired:
            return self.failed()
        self.without_enter = None
        return True

    @staticmethod
    def failed():
        log("warning: couldn't type into the session; retrying next tick")
        return False


def brief_hour_from_env():
    raw = os.environ.get("BOT_BRIEF_HOUR_UTC", "").strip()
    if not raw:
        return DEFAULT_BRIEF_HOUR_UTC
    if not raw.isdigit() or not 0 <= int(raw) <= 23:
        sys.exit(f"{PROG}: BOT_BRIEF_HOUR_UTC must be an hour from 0 to 23, not {raw!r}")
    return int(raw)


def run():
    brief_hour = brief_hour_from_env()
    try:
        config = load_config()
    except ConfigError as e:
        sys.exit(f"{PROG}: {e}")
    # Only the backend sessions carry these: never the loop and its polls.
    for var in (GENERATION_VAR, BACKEND_VAR):
        os.environ.pop(var, None)
    os.makedirs(STATE_DIR, exist_ok=True)
    lock = open(LOOP_LOCK, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        sys.exit(f"{PROG}: another loop is running (it holds {LOOP_LOCK})")
    typist = Typist()
    sessions = TmuxSessions(os.getcwd())
    switch = Switch(config, sessions=sessions, environ=dict(os.environ), type_message=typist,
                    state_file=BACKEND_STATE_FILE, marks_file=MARKS_FILE, ack_file=HANDOFF_ACK_FILE,
                    start_ack_file=START_ACK_FILE, skill_dir=SKILL_DIR)
    loop = Loop(time.time(), spawn=Poll, type_message=typist, switch=switch,
                pending=Pending(PENDING_FILE), last_reported=lambda: last_reported(POLL_STATE_FILE),
                brief_hour=brief_hour, brief_file=BRIEF_FILE)
    # Stop cleanly on SIGTERM (systemctl stop, forwarded by the container's
    # init, which also reaps orphans: see RunInit in the .container file).
    stopping = False

    def stop(*_):
        nonlocal stopping
        stopping = True
        raise SystemExit(0)
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        while loop.step(time.time()):
            time.sleep(TICK)
        log("the coordinator session ended")
    finally:
        loop.stop()
        if stopping:
            log("stopping")
            sessions.stop()
            tmux("kill-server")


def status(out=sys.stdout):
    """The loop's view of the backends, from its state file."""
    config = load_config()
    state = read_json(BACKEND_STATE_FILE, {})
    state = state if isinstance(state, dict) else {}
    view = state.get("view") if isinstance(state.get("view"), dict) else {}
    active = state.get("active")
    print(f"active: {active or 'none yet'} ({state.get('phase') or 'not started'}"
          f"{', handing off to ' + str(state.get('target')) if state.get('phase') == 'handoff' else ''})", file=out)
    later = False
    nxt = None
    for b in config.backends:
        v = view.get(b.name) if isinstance(view.get(b.name), dict) else {}
        used = v.get("used_percent")
        reading = ("no reading" if used is None else
                   f"{used:.1f}% used (observed {v.get('observed_at')}, resets {v.get('resets_at') or '?'})")
        avail = "available" if v.get("available") else f"unavailable: {v.get('why') or 'not probed yet'}"
        if v.get("probe_error"):
            reading += f" (probe: {v['probe_error']})"
        print(f"{b.name}: {avail}; {reading}; leaves at {b.leave_at:g}%, returns below {b.return_below:g}%", file=out)
        if later and nxt is None and v.get("available"):
            nxt = b.name
        later = later or b.name == active
    if nxt is None:
        # Only a backend after the active one counts as a next one: a
        # preferred one that's available is switched back to by itself.
        print("next: none (this is the last available backend)", file=out)
    else:
        print(f"next: {nxt}", file=out)


def ack(path):
    """Acks for this session's generation; run outside a session (by the
    operator), for the running one's. Written whole or not at all."""
    gen = os.environ.get(GENERATION_VAR, "")
    if not gen.isdigit():
        state = read_json(BACKEND_STATE_FILE, {})
        gen = str(state.get("generation")) if isinstance(state, dict) and isinstance(state.get("generation"), int) else ""
    if not gen.isdigit():
        sys.exit(f"{PROG}: no session to ack for (no {GENERATION_VAR}, and no generation in {BACKEND_STATE_FILE})")
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w") as f:
        f.write(gen + "\n")
    os.replace(tmp, path)


def main(argv):
    match argv:
        case ["run"]:
            run()
        case ["ack", *runs] if runs:
            for r in Pending(PENDING_FILE).ack(runs):
                log(f"warning: {r} wasn't pending")
        case ["pending"]:
            for e in Pending(PENDING_FILE).entries():
                print(f"{e['run']} ({', '.join(e['kinds'])})")
        case ["status"]:
            status()
        case ["handed-off"]:
            ack(HANDOFF_ACK_FILE)
            log("the loop stops this session within a tick or so")
        case ["started"]:
            ack(START_ACK_FILE)
            log("thanks: the loop now knows this backend works")
        case ["check"]:
            try:
                config = load_config()
            except ConfigError as e:
                sys.exit(f"{PROG}: {e}")
            ready = [b.name for b in config.backends if not b.missing(os.environ)]
            for b in config.backends:
                print(f"{b.name}: {b.missing(os.environ) or 'can start'}")
            if not ready:
                sys.exit(f"{PROG}: no backend in {config_path()} can start")
        case ["render-args"]:
            try:
                config = load_config()
            except ConfigError as e:
                sys.exit(f"{PROG}: {e}")
            for b in config.backends:
                print(f"--backend={b.name}={b.notes or ''}")
        case _:
            sys.exit(USAGE)


if __name__ == "__main__":
    main(sys.argv[1:])
