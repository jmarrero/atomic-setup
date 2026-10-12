#!/usr/bin/env python3
"""The coordinator container's main loop, which its entrypoint ends in.

It keeps `bot-poll --exclude-lead '*'` running outside the Claude session,
restarting it after every exit, and wakes the session only when there is
something to handle, by typing a short message into its tmux session (a
message typed while the session works waits its turn):

- bot-poll's NEWS: the kinds and run it names, nothing else;
- once a day at BOT_BRIEF_HOUR_UTC: a reminder to post the morning brief;
- a poll that keeps failing.

Typed text reads as the operator's own input, so it is only ever fixed text
plus bot-poll's kind names and run ids, each checked against what bot-poll
can print. The news itself (titles, comments, anything from GitHub) stays in
the run's files and `bot-poll --summary`, which the session reads as data.

Each NEWS is saved to PENDING before it is typed, and stays pending until
the session acks its run, so news a restart interrupted is typed again on
the next start. On start, bot-poll's last NEWS report (in its state.json)
is added too if PENDING doesn't know it: bot-poll records news as reported
before it exits, and a restart can come before the loop saw that exit.

It is a stand-in for homegit upstream's bot-supervisor and bot-poll-loop,
until our fork has them. Python's standard library only.

    bot-coordinator-loop run         run the loop (the entrypoint does)
    bot-coordinator-loop ack RUN...  mark news runs handled
    bot-coordinator-loop pending     print the unacked news runs
"""
import contextlib
import datetime
import fcntl
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time

PROG = "bot-coordinator-loop"
USAGE = f"usage: {PROG} run | ack RUN... | pending"
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
    session is gone. type_message returns whether it typed."""

    def __init__(self, now, *, spawn, type_message, session_alive, pending, last_reported,
                 brief_hour, brief_file):
        self.spawn = spawn
        self.type_message = type_message
        self.session_alive = session_alive
        self.pending = pending
        self.last_reported = last_reported
        self.brief_hour = brief_hour
        self.brief_file = brief_file
        self.ready_at = now + STARTUP_DELAY
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
        # The day of the last brief reminder, should its file fail.
        self.brief_day = None
        self.polls = 0
        self.poll = None
        self.poll_started = 0
        self.next_poll_at = 0
        self.failures = 0
        # Messages not typed yet, retried every tick.
        self.outbox = []

    def step(self, now):
        if not self.session_alive():
            return False
        if now < self.ready_at:
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
        return True

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

    def say(self, message):
        self.outbox.append(message)
        self.flush()

    def flush(self, now=None):
        while self.outbox:
            try:
                if not self.type_message(self.outbox[0]):
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
                self.say(news_message(*news))
        self.reconciled = True

    def replay(self, now):
        """Retypes the runs not acked before the restart."""
        if self.replayed:
            return
        leftover = [e["run"] for e in self.pending.entries()]
        if leftover:
            log(f"{len(leftover)} news run(s) from before the restart are unacked; telling the session")
            self.say(restart_message(leftover))
        self.replayed = True

    def save_news(self, now):
        """Types the news of finished polls, then saves it to pending; what
        can't be saved yet stays here, to be tried again next tick. Typing
        doesn't wait for saving, which only matters across restarts."""
        for item in self.unsaved:
            kinds, run, typed = item
            if not typed:
                self.say(news_message(kinds, run))
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
    os.makedirs(STATE_DIR, exist_ok=True)
    lock = open(LOOP_LOCK, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        sys.exit(f"{PROG}: another loop is running (it holds {LOOP_LOCK})")
    loop = Loop(time.time(), spawn=Poll, type_message=Typist(),
                session_alive=session_alive, pending=Pending(PENDING_FILE),
                last_reported=lambda: last_reported(POLL_STATE_FILE),
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
            tmux("kill-server")


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
        case _:
            sys.exit(USAGE)


if __name__ == "__main__":
    main(sys.argv[1:])
