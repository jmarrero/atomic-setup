# bot-coordinator

The bot's long-lived coordinator: an interactive Claude Code session running
homegit's `coordinator` skill, in a podman container managed by systemd. It
polls for news (`bot-poll`: `bot-notify`, `bot-pr inbox`, `bot-watch`),
keeps the Workstream board moving, dispatches worker and reviewer subagents,
and builds and tests only on devspaces. botd (`../`) is separate: it only
answers @mentions.

## What's in the container

- Image `localhost/bot-coordinator`: the bot image (`../agent.Containerfile`)
  plus `entrypoint.sh`.
- Home `/home/agent` is the named volume `bot-coordinator-home`: clones under
  `~/src/github/jmarrero-bot` (homegit included), Claude's sessions, caches
  and bot-devspace's keys survive restarts.
- On each start the entrypoint pulls homegit (`jmarrero-forge/homegit`),
  links its `bin/` into `~/.local/bin`, renders its skills, `AGENTS.md` and
  `builder` subagent for this operator (see below) and installs those as
  `~/.agents/skills`, `~/.claude/skills`, `~/.claude/CLAUDE.md` and
  `~/.claude/agents/builder.md`, rebuilds `bot-poll` if `crates/` changed, checks the
  operator config and `gh`, then starts `claude --dangerously-skip-permissions`
  in tmux session `coordinator`, in `~/src/github/jmarrero-bot`.
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

The coordinator only hears of news while `bot-poll` runs in its background,
and it sometimes stops it and doesn't start it again. The entrypoint's
watchdog types a reminder into the session when no `bot-poll` has run for
15 minutes (`BOT_POLL_WATCHDOG_SECS`), at most once per 30.

Rebuild after changing `../agent.Containerfile` or this directory, then
`systemctl --user restart bot-coordinator`. homegit updates need only a
restart (or `git pull`, which bot-poll's sweeps do).
