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
  links its `bin/` into `~/.local/bin`, its skills into `~/.agents/skills` and
  `~/.claude/skills`, its `AGENTS.md` as `~/.claude/CLAUDE.md` and the
  `builder` subagent, rebuilds `bot-poll` if `crates/` changed, checks the
  operator config and `gh`, then starts `claude --dangerously-skip-permissions`
  in tmux session `coordinator`, in `~/src/github/jmarrero-bot`.
- Credentials: podman secrets `jmarrero-bot-gh-token` (`GH_TOKEN`) and
  `jmarrero-bot-claude-token` (`CLAUDE_CODE_OAUTH_TOKEN`); the operator
  config is `~/.config/bot-harness/operator.json` (its directory is mounted
  read-only, so edits show up without a restart).
- No `keep-id`: the container's users map to subordinate ids, so an escape
  isn't your host user. Capabilities dropped, no-new-privileges, 16G/4 CPUs.
- Network goes through the host, so devspaces on the tailnet are reachable.

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
Max subscription.

Rebuild after changing `../agent.Containerfile` or this directory, then
`systemctl --user restart bot-coordinator`. homegit updates need only a
restart (or `git pull`, which bot-poll's sweeps do).
