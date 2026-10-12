# bot-coordinator

The bot's long-lived coordinator: an interactive Claude Code session running
homegit's `coordinator` skill, in a podman container managed by systemd. It
polls for news (`bot-poll`: `bot-notify`, `bot-pr inbox`, `bot-watch`),
keeps the Workstream board moving, dispatches worker and reviewer subagents,
and builds and tests only on devspaces. botd (`../`) is separate: it only
answers @mentions.

## What's in the container

- Image `localhost/bot-coordinator`: the bot image (`../agent.Containerfile`)
  plus `entrypoint.sh` and `coordinator-loop.py`.
- Home `/home/agent` is the named volume `bot-coordinator-home`: clones under
  `~/src/github/jmarrero-bot` (homegit included), Claude's sessions, caches
  and bot-devspace's keys survive restarts.
- On each start the entrypoint pulls homegit (`jmarrero-forge/homegit`),
  links its `bin/` into `~/.local/bin`, renders its skills, `AGENTS.md` and
  `builder` subagent for this operator (see below) and installs those as
  `~/.agents/skills`, `~/.claude/skills`, `~/.claude/CLAUDE.md` and
  `~/.claude/agents/builder.md`, rebuilds `bot-poll` if `crates/` changed, checks the
  operator config and `gh`, then starts `claude --dangerously-skip-permissions`
  in tmux session `coordinator`, in `~/src/github/jmarrero-bot`, and hands
  over to `bot-coordinator-loop` (see "Polling" below).
- Credentials: podman secrets `jmarrero-bot-gh-token` (`GH_TOKEN`) and
  `jmarrero-bot-claude-token` (`CLAUDE_CODE_OAUTH_TOKEN`); the operator
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
Max subscription. It runs `claude-opus-5-5` at `high` effort
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
"Polling" section of [`operator-notes.md`](operator-notes.md) into it.

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
