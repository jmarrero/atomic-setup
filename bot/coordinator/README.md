# bot-coordinator

The bot's long-lived coordinator: an interactive Claude Code session running
homegit's `coordinator` skill, in a podman container managed by systemd.
When Claude's weekly usage runs out, an opencode session takes over until
it resets (see "Backends" below). It
polls for news (`bot-poll`: `bot-notify`, `bot-pr inbox`, `bot-watch`),
keeps the Workstream board moving, dispatches worker and reviewer subagents,
and builds and tests only on devspaces. botd (`../`) is separate: it only
answers @mentions.

## What's in the container

- Image `localhost/bot-coordinator`: the bot image (`../agent.Containerfile`)
  plus `entrypoint.sh`, `coordinator-loop.py` and the default
  `backends.toml`.
- Home `/home/agent` is the named volume `bot-coordinator-home`: clones under
  `~/src/github/jmarrero-bot` (homegit included), Claude's sessions, caches
  and bot-devspace's keys survive restarts.
- On each start the entrypoint pulls homegit (`jmarrero-forge/homegit`),
  links its `bin/` into `~/.local/bin`, renders its skills, `AGENTS.md` and
  `builder` subagent for this operator (see below) and installs those as
  `~/.agents/skills`, `~/.claude/skills`, `~/.claude/CLAUDE.md` and
  `~/.claude/agents/builder.md`, rebuilds `bot-poll` if `crates/` changed, checks the
  operator config, `gh` and the backends, then hands over to
  `bot-coordinator-loop`, which starts the first available backend
  (`claude --dangerously-skip-permissions` by default) in tmux session
  `coordinator`, in `~/src/github/jmarrero-bot`, and polls (see "Polling"
  and "Backends" below).
- Credentials: podman secrets `jmarrero-bot-gh-token` (`GH_TOKEN`),
  `jmarrero-bot-claude-token` (`CLAUDE_CODE_OAUTH_TOKEN`) and
  `jmarrero-bot-copilot-auth` (`OPENCODE_AUTH_CONTENT`); the operator
  config is `~/.config/bot-harness/operator.json` (its directory is mounted
  read-only, so edits show up without a restart).
- No `keep-id`: the container's users map to subordinate ids, so an escape
  isn't your host user. Capabilities dropped, no-new-privileges, 16G/4 CPUs.
- Network goes through the host, so devspaces on the tailnet are reachable.

## Rendered skills and goals

homegit's skills are written for cgwalters' setup and spell out his logins,
forge org, tracker, board, identities, paths and goals; the model sometimes
copies those literally (it once signed a comment with his `Generated-by`
link). `render-skills.py` makes copies with this operator's values from
`bot-operator --json`, keeping only what really is his (his repositories,
issues and gists, his review app, the coordination channel), turns his side
of the coordination channel around, and replaces his goals and priority
rules with `~/.config/bot-harness/goals.md` (default:
[`operator-goals.md`](operator-goals.md)). Edit that file and restart the
coordinator to change the bot's priorities. homegit itself stays as he
writes it, so the fork is easy to update. If a homegit change moves a
section the renderer replaces, it stops with an error rather than render
his goals as yours; `bot/test_render.py` checks the result.

## opencode for reviews and well-defined work

The coordinator also has `bot-opencode`, which runs one task with opencode
on a GitHub Copilot model (`BOT_OPENCODE_MODEL`, with the
`jmarrero-bot-copilot-auth` secret; usage counts against the license
holder's premium requests). The operator's notes
(`~/.config/bot-harness/notes.md`, default
[`operator-notes.md`](operator-notes.md), appended to the rendered
coordinator skill) say when: a second, different-model review of every
forge PR, and items well-defined enough to need no design choices. Edit the
notes and restart the coordinator to change that. Runs are logged in
`~/.local/state/bot-opencode/` in the container.

## Backends

The session runs on one of an ordered list of backends, from
`~/.config/bot-harness/backends.toml` (default:
[`backends.toml`](backends.toml), which documents the format): Claude Code
(`COORDINATOR_MODEL`, `COORDINATOR_EFFORT`), then opencode in its TUI with
`--auto` (`BOT_OPENCODE_MODEL`, the Copilot login). Each has a launch
command, the secret it needs, notes for the skill, and a usage probe:

- Claude: the 7-day percent that `bot-heartbeat statusline` saves in
  `~/.cache/bot-heartbeat/rate-limits.json`, with its reset time. It only
  updates while Claude runs.
- opencode: Copilot's premium requests, from
  `https://api.github.com/copilot_internal/user` with the Copilot login
  (for a GitHub Enterprise Cloud login, `api.SUBDOMAIN.ghe.com`; a
  self-hosted Enterprise Server login has no known endpoint, so its token
  is sent nowhere and there is no reading, which `status` says). That
  endpoint is undocumented (it's what VS Code's quota display reads). A
  refused login (401) keeps opencode out for an hour at a time, until a
  reading works again; any other failure is no reading, and opencode
  counts as available.

The loop leaves a backend whose reading reaches `leave_at` (95%), even a
stale reading, since usage only grows until its window resets, and comes
back when the window resets or a fresh reading is below `return_below`
(50%), but not sooner than 30 minutes after the last switch. A backend
without its secret is skipped; with every backend used up, it stays where
it is and tells the session to dispatch nothing new. To add a provider
(Grok, Cursor), add a `[[backend]]` with its command, secret, notes and a
probe: `file`, or `command` (anything printing `{"used_percent",
"resets_at", "observed_at"}`). The loop reads the file when it starts, so
restart the container after editing it.

To override a probe, write `~/.config/bot-harness/backend-marks.toml` on
the host (the container mounts that directory read-only, so the session
can't steer itself); the loop reads it every tick:

    [claude]
    state = "exhausted"            # out until until (optional), or until removed
    until = 2026-10-20T00:00:00Z

    [opencode]
    state = "available"            # clears what the loop recorded before at
    at = 2026-10-13T12:00:00Z      # required: editing other entries doesn't move it

`podman exec bot-coordinator bot-coordinator-loop status` shows the result.

A handoff types a fixed message into the session: stop dispatching, record
each running worker's state on its board item (with its devspace or agent
run, which outlive it), then run `bot-coordinator-loop handed-off`. After
the ack, or 15 minutes (`handoff_timeout_minutes`), it stops the tmux server
(which serves only this session) and every process that carries its
`BOT_COORDINATOR_GENERATION`, then starts the next backend with the same
prompt, and types the news the old one didn't ack and a takeover note. A
new backend starts only once neither the session nor any such process is
left, and a restart of the loop stops whatever is left first, so a crash
at any step ends with one backend running. A stop that fails 6 times in a
row exits the loop, so the container's restart takes everything down; a
start that fails 3 times in a row holds that backend for an hour and tries
the next (with none left, the loop exits and the container restarts).

Each backend's `clean` paths are emptied before it starts (opencode's
config, cache and data directories; see below), and a failure to do that
is a failed start.

A session can also start and not work: a refused model, a dialog, no
quota. So the first thing `COORDINATOR_PROMPT` and every start note
(`Started:`, `Takeover:`) ask is `bot-coordinator-loop started`, ahead of
the first turn's work. Claude also counts as working as soon as its status
line saves a new reading (`proved_by_reading`, for `file` probes only):
stamped by the status line, after the start, and not the one the file held
at the start. A session that does neither within 10 minutes of the startup
delay (`start_ack_timeout_minutes`), or that ends within 5 minutes of its
start, has its backend held for an hour, and the loop moves on at once; if
no other backend can run, it leaves the session running and says so. A
held backend with nothing else to run is started anyway (a coordinator
that may not work beats none); the loop exits only when no backend can
start at all. A late `started` still counts. Acks are
written whole (a temporary file, renamed) and count only for their own
generation. The state is in `~/.local/state/bot-coordinator/backend.json`;
`status` shows probe errors only as fixed text (`HTTP 503`, `timed out`),
never a server's words, which stay in the container's log.

Both backends read the same rendered skills (opencode reads
`~/.agents/skills` and `~/.claude/skills`, and the rendered `AGENTS.md`).
`render-skills --backend NAME=NOTES` also writes
`skills/coordinator/SKILL.md.NAME`, the coordinator skill with that
backend's notes ([`backend-opencode.md`](backend-opencode.md): how the
skill's Claude Code tools map onto opencode's), and the loop copies the
active one over `SKILL.md`. The operator's notes say how a session hands
off, and replace the skill's "at 100% dispatch nothing new" with handing
off.

### opencode's config, and the builder under opencode

opencode's config is [`opencode-config.py`](opencode-config.py)'s output,
which the image installs as `/etc/opencode/opencode.json`: root-owned, and
opencode's system-managed config. It pins opencode to the Copilot
provider, turns off the update prompt, sharing and the `question` tool,
and defines the `second-opinion` and `builder` agents; it applies to
`bot-opencode`'s runs too. To change it, edit the generator and rebuild.

What this protects against is mistakes, not a session that sets out to
change its own config: the container, its credentials and the trust rules
are the boundary, as for Claude, and the same user can edit its home, put
commands first in `PATH` or signal its own processes.

- The provider pin always wins: opencode loads the managed config last,
  also when the TUI reloads its config (SIGUSR2).
- The builder rules are merged with those of a session's own config
  directories, keeping the order their keys first appeared in, so a file
  there that lists the managed rules first and its own after them
  outranks them (on opencode 1.19.0, as the check below shows). So before
  each opencode start the loop empties `~/.opencode` and opencode's config
  and cache directories (config, agents, plugins, cached packages), and
  moves its data directory (sessions, logs, and the account state that
  could pull in a remote config) to
  `~/.local/state/bot-coordinator/archive/opencode/`, where the 5 newest
  are kept, within 2 GiB together (the oldest go first; a single entry
  bigger than that is still kept, so it isn't a hard bound). The login
  (`auth.json`) is moved out before and back after, so no archive holds
  it, and is put back even if archiving fails. The
  paths follow `XDG_CONFIG_HOME`, `XDG_CACHE_HOME` and `XDG_DATA_HOME`. It
  then links the rendered `AGENTS.md` afresh. If anything of that fails
  (`clean`, `archive`, `keep` and `relink` in `backends.toml`), so does
  the start, like any other failed start. Project config is off
  (`OPENCODE_DISABLE_PROJECT_CONFIG`).
- [`check-opencode-config.py`](check-opencode-config.py), run in the
  image after each opencode bump, checks this: planted config of each kind
  after the clean, and the provider pin after a SIGUSR2 reload; it reports
  whether the reload picks up a planted builder rule.
- That protects each start. A session that plants config itself can still
  get it read later: by a SIGUSR2 reload, or as instructions (the global
  `AGENTS.md` link's target is re-read on every turn, and an `AGENTS.md`
  next to a file it reads is attached to it, as Claude Code does with
  `CLAUDE.md`).

Claude Code's `builder` subagent has a hook that refuses commits, pushes
and GitHub or board writes; under opencode, `builder` is an opencode agent
whose bash permissions do that job. opencode matches each command of a
command line (as its shell parser splits it: `a && b`, `a | b`, `$(b)`
are separate commands) against every pattern, `*` matching anything and
`?` one character, and the last match wins. So after the `*` allow there
are only deny rules: an allow after them would let through any denied
command that merely mentions it (`git commit -m 'gh pr view'`).

The rules look for the command words where a command can start: at the
start, after a space or tab (so also behind `env`, `command`, `timeout`,
`xargs`, `nice` or `VAR=1`), after a slash (a path), with up to three
spaces or tabs between the words, a redirect or `&` right after a write
word (`git push>/tmp/log`, `gh pr merge>/tmp/log`), options between `gh`
and its words (`gh -R o/r pr merge 1`), and anywhere after a shell (`sh`,
`bash`, `dash`, `zsh`, `ksh`, as a word or a path) given an option, which
covers its `-c` however it is spelled (`bash -lc`, `bash --norc -c`,
`bash -o pipefail -c`), and after `eval`. They refuse:

- `git` followed by `commit`, `commit-tree`, `push`, `pull`, `am`,
  `cherry-pick`, `merge`, `rebase`, `revert` or `update-ref`, also behind
  options (`git -C DIR push`, `git -c k=v commit`);
- `gh issue`, `gist`, `release`, `project`, `label`, `secret`, `variable`
  and `alias`; the writing `gh pr` (`create`, `merge`, `close`, `edit`,
  `comment`, `review`, `ready` and the rest), `gh repo`, `gh workflow
  run|enable|disable`, `gh run rerun|cancel|delete` and `gh cache delete`
  commands; `gh api` with `-X`, `-f`, `-F` (also attached, `-ftitle=x`),
  `--method`, `--field`, `--raw-field` or `--input`;
- the bot's write tools (`bot-board`, `bot-git`, `bot-land`, `bot-pr`,
  `bot-watch` and the rest the hook lists) and `dco-signoff`, anywhere.

Reading stays allowed: builds and tests, `git status`, `log` (`--grep
commit` too), `diff`, `show`, `fetch` (a pull request's head too), `stash
push`, `merge-base`, `bisect`, `worktree`; `gh pr view|checks|diff|list`,
`gh run view`, `gh run list --branch release`, `gh api` reads; searching
for those words (`rg 'git push'`); checkout paths under `~/src/github`;
a shell running a script or a command without those words (`bash -lc
'cargo test'`). It refuses a few harmless
ones: options before a git subcommand that is followed later by one of
those words (`git --no-pager log --grep push`), a write word later in a
`gh pr` command (`gh pr list --json title -q merge`), all of `gh issue`
(`gh issue view` too), `git`, `gh` or `bot-` anywhere after a shell given
an option (`bash -c 'cargo build && git status'`), and any command naming
one of the bot's write tools. `bot-devspace ssh NAME '...'` isn't a
shell (`ssh` isn't `sh`), so `cargo fmt --check && git diff` there passes.

It is a guard against mistakes, not a sandbox. It doesn't refuse quoted
words (`git "push"`, `'git' push`), a word built at run time (`git
$(printf push)`, `sh -c "$CMD"`), four or more spaces or tabs between the
words, a shell run without options (`bash script.sh`), a script fed to
a shell through a pipe or a heredoc (`echo 'git push' | sh`, `bash
<<EOF`), git and gh aliases (`git -c alias.p=push p`), other interpreters
(`python3 -c`), scripts that run these, or `curl` to the API.
`bot/test_coordinator_loop.py` checks the rules against opencode's matcher,
with the reviewers' lists of what must and mustn't pass.

## Use

    podman build -t localhost/bot-coordinator bot/coordinator/
    ln -s $PWD/bot/coordinator/bot-coordinator.container ~/.config/containers/systemd/
    systemctl --user daemon-reload && systemctl --user start bot-coordinator

    podman exec -it bot-coordinator tmux attach -t coordinator   # Ctrl-b d to detach

It starts at boot and starts working on its own: `COORDINATOR_PROMPT` in the
`.container` file is its first message ("Load the coordinator skill and run
the bot."); comment it out to have it wait for you. If the Claude session
ends (`/exit`, or by accident), systemd restarts it after 60 seconds; stop it
for good with `systemctl --user stop bot-coordinator`. Attach to watch or
talk to it, and detach with Ctrl-b d. Its usage counts against the Claude
Max subscription, and on the opencode backend against the Copilot
license's premium requests. It runs `claude-opus-5-5` at `high` effort
(`COORDINATOR_MODEL`, `COORDINATOR_EFFORT` in the `.container` file).

## Polling

`bot-poll` runs outside the Claude session. It used to run as the
session's background task, which wakes the session when it exits; but
Claude Code's Bash tool kills background tasks after at most 2 hours, so
an idle coordinator woke every 2 hours just to restart it, re-reading its
whole long session each time.

Instead, the entrypoint ends in `bot-coordinator-loop run`
([`coordinator-loop.py`](coordinator-loop.py); a lock keeps it to one
loop), which keeps
`bot-poll --exclude-lead '*'` running (the first one with `--no-wait`, so
it sweeps at once) and restarts it after each exit. A crashed poll is
restarted with backoff (30s, doubling to 15 minutes); after 4 failures in a
row the session is told once. The poll's stderr is in
`~/.local/state/bot-coordinator/bot-poll.stderr` (a failed one's is kept
as `bot-poll.stderr.failed`), the loop's own log in the container's
journal (`podman logs bot-coordinator`). An error in one tick is logged
and the loop goes on, rather than restart the container and the session's
workers with it. The `.container` file sets `RunInit=true`, so an init is
PID 1: it reaps orphaned processes (`git gc --auto` from the sweeps' pulls,
ssh masters, the session's `&` jobs) and forwards SIGTERM to the loop,
which then stops the poll (killing it after 10 seconds) and tmux.

It wakes the session only when there is something to handle, by typing a
line into its tmux session, which waits its turn if the session is busy
(a line tmux couldn't type is retried on the next tick):

- on bot-poll's `NEWS (KINDS) ...`: "bot-poll found news (KINDS) in run
  RUN: run `bot-poll --summary`, read RUN's files and handle it, then
  `bot-coordinator-loop ack RUN`". The news is saved to
  `~/.local/state/bot-coordinator/pending.json` first, and whatever isn't
  acked is typed again after a restart (`bot-coordinator-loop pending`
  lists it). bot-poll records news as reported before it exits, so on
  start the loop also adds bot-poll's last NEWS report (from its
  `state.json`) as unacked if `pending.json` doesn't know it. On a first
  start that may repeat news already handled, which is cheaper than
  losing some. Until reading bot-poll's state, the start and saving each
  news run succeed, they are retried every tick and no new poll starts,
  but news is typed without waiting for its save. If the loop's state
  keeps failing (a full disk, a directory it can't write), the session is
  told so, again every 3 hours while it lasts, rather than see what looks
  like a quiet day. An unreadable `pending.json` is moved aside as
  `pending.json.bad-TIME`;
- once a day at `BOT_BRIEF_HOUR_UTC` (07 by default, set in the `.container`
  file), a reminder to post the morning brief.

Text typed there reads as the operator's own, so it is only ever fixed text
plus bot-poll's kind names and run ids, each checked against the names and
format bot-poll uses; a NEWS line that doesn't check out still wakes the
session, without its kinds. Titles, comments and anything else from GitHub
stay in the run's files and `bot-poll --summary`, which the session reads
as data. `COORDINATOR_PROMPT` and the operator's notes tell the session
never to start `bot-poll` itself: a second poll can't take the first's
lock. If you keep your own `~/.config/bot-harness/notes.md`, copy the
"Polling", "Backends and handoffs" and "Capacity" sections of
[`operator-notes.md`](operator-notes.md) into it.

If you're attached and typing when a line comes in, it is appended to what
you typed, and Enter sends both; the old watchdog had the same problem,
but more rarely. Detach (Ctrl-b d) when you're not talking to the session.

The session still publishes its heartbeat (`bot-heartbeat publish`) when it
wakes, so during long quiet periods the review app's ops view may show the
coordinator as stale. That stays so until our homegit fork takes upstream's
heartbeat and its `bot-supervisor`/`bot-poll-loop`, which do this loop's job
and would replace it.

Rebuild after changing `../agent.Containerfile` or this directory, then
`systemctl --user restart bot-coordinator`. homegit updates need only a
restart (or `git pull`, which bot-poll's sweeps do).
