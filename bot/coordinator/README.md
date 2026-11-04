# bot-coordinator

The bot's interactive session: one plain Claude Code session, in a podman
container managed by systemd, that jmarrero attaches to and talks with. It
starts nothing on its own. Since cutover
([jmarrero-forge/tracker#42](https://github.com/jmarrero-forge/tracker/issues/42),
[#44](https://github.com/jmarrero-forge/tracker/issues/44)) the hosted
harness in [jmarrero-forge/tracker](https://github.com/jmarrero-forge/tracker)
does the polling, the board, dispatch and pull request follow-up, as
cgwalters-forge/tracker's does for cgwalters; this session is the
counterpart of the one at his workstation, for what has no hosted path:
devspace and KVM work, reviews that need devspace testing, and upstream
promotion by hand
([tracker#45](https://github.com/jmarrero-forge/tracker/issues/45)). The
container keeps its pre-cutover name, so its unit and home volume stay.
botd (`../`) is separate: it only answers @mentions.

## What's in the container

- Image `localhost/bot-coordinator`: the bot image (`../agent.Containerfile`)
  plus `entrypoint.sh`, `render-skills.py`, `session.md` and
  `operator-goals.md`.
- Home `/home/agent` is the named volume `bot-coordinator-home`: clones under
  `~/src/github/jmarrero-bot` (homegit included), Claude's sessions, caches
  and bot-devspace's keys survive restarts.
- On each start the entrypoint pulls homegit (`jmarrero-forge/homegit`,
  frozen) and links only the four tools the session still uses into
  `~/.local/bin` (`bot-devspace`, `bot-git`, `bot-operator`,
  `bot-review-guide`), removing the other homegit links, the loop's
  `bot-poll` build and homegit's builder subagent left by earlier starts. It renders the session's instructions and skills
  (below), checks the operator config and `gh`, and starts
  `claude --dangerously-skip-permissions` in tmux session `coordinator`, in
  `~/src/github/jmarrero-bot`, with no first message. When that session
  ends, so does the container.
- Credentials: podman secrets `jmarrero-bot-gh-token` (`GH_TOKEN`) and
  `jmarrero-bot-claude-token` (`CLAUDE_CODE_OAUTH_TOKEN`), plus, when its
  drop-in is set up, `jmarrero-bot-hackmd-token` (`HACKMD_TOKEN`, see
  [../SETUP.md](../SETUP.md#2-github-accounts-and-tokens); its rules are in
  [`session.md`](session.md)); the operator config is
  `~/.config/bot-harness/operator.json` (its directory is mounted
  read-only, so edits show up without a restart).
- No `keep-id`: the container's users map to subordinate ids, so an escape
  isn't your host user. Capabilities dropped, no-new-privileges, 16G/4 CPUs.
- Network goes through the host, so devspaces on the tailnet are reachable.

## Instructions and skills

homegit's text is written for cgwalters' setup and spells out his logins,
forge org, tracker, board, identities and paths; the model sometimes copies
those literally. `render-skills.py` makes copies with this operator's values
from `bot-operator --json`, keeping only what really is his (his
repositories, issues and gists, the coordination channel); if a homegit
change moves a section it replaces, it stops with an error, and
`bot/test_render.py` checks the result.

For the session it keeps only the skills that name no loop duty
(`devspace-work`, `commit-review`, `ast-explore`, `codegraph`), and writes
its `CLAUDE.md` from homegit's `AGENTS.md` up to its "Commit attribution"
section, followed by [`session.md`](session.md): attribution, identity,
what the session does now that the pass runs the board, and jmarrero's
standing rules. That is where to change what the session is told; rebuild
the image and restart the container after editing it.

## Use

    podman build -t localhost/bot-coordinator bot/coordinator/
    ln -s $PWD/bot/coordinator/bot-coordinator.container ~/.config/containers/systemd/
    systemctl --user daemon-reload && systemctl --user start bot-coordinator

    podman exec -it bot-coordinator tmux attach -t coordinator   # Ctrl-b d to detach

It starts at boot and waits for you. If the Claude session ends (`/exit`,
or by accident), systemd starts a fresh one after 60 seconds; stop it for
good with `systemctl --user stop bot-coordinator`. Its usage counts against
the Claude Max subscription. It runs `claude-opus-5-5` at `high` effort
(`COORDINATOR_MODEL`, `COORDINATOR_EFFORT` in the `.container` file).

Rebuild after changing `../agent.Containerfile` or this directory, then
`systemctl --user restart bot-coordinator`.
