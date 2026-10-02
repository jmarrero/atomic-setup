# bootc-bot

See [SETUP.md](SETUP.md) for the full runbook of how this bot, its forge
org, devspaces and credentials were set up.

Only jmarrero, cgwalters and cgwalters-bot can trigger it: jmarrero on any
repo the bot account can see, the other two in `bootc-dev` repos. Comment:

    @<bot-account> claude investigate why bootc is not installing the container

and the requested agent runs on this machine in a disposable container, then
the bot posts its answer as a reply. `@<bot-account> help` lists agents.

## How it works

```
GitHub @mention ──► bot account's notifications
                          │  polled by botd (no inbound ports/tunnels)
                          ▼
botd.py (systemd user service on the host)
  • "@bot <agent> <request>" must start a line (quotes ignored)
  • commenter is in trusted_user_ids (any repo), or
    repo owner is in allowed_owners AND commenter is in allowed_user_ids
    (or, if [auth].org is set, an org/team member, checked via API, fail closed)
  • per-user rate limit, queue limit, max request age
                          │
                          ▼
podman run --rm bootc-bot-agent   (uid 2000, no caps, no-new-privileges,
  bot-run.sh: public clone,        memory/cpu/pids limits, timeout)
  checkout PR head, run agent
                          │ stdout or /bot/out/reply.md
                          ▼
botd redacts known secrets and posts the reply with the bot token
```

## Security model

- **Who can trigger it:** only the users listed by numeric id, never by
  login, because a login can be renamed and then registered by someone else.
  `[auth].trusted_user_ids` can use the bot on any repo, and
  `[auth].allowed_user_ids` only in `allowed_owners` repos. Everyone else is
  ignored without a reply.
- **Optional org access:** setting `[auth].org` (or `team`) also allows every
  member, checked on every request with `BOTD_MEMBERSHIP_TOKEN` (your token,
  since seeing private members requires membership). Redirects aren't
  followed, so a misconfigured token can't silently fall back to public
  membership. It's off in the example.
- **What the agent can do:** the job container gets no GitHub credentials,
  only its agent's model credential (a podman secret). The bot token, which can
  post comments, never enters it.
- **Prompt injection is the remaining risk:** an allowed user can ask about an issue
  written by anyone, and that text reaches the agent. The prompt tells the
  agent to treat it as data, but assume that sometimes won't hold. A job can
  read its model credential, so use tokens made for the bot (revocable without
  logging you out), never your own CLI logins. Consider:
  - Restricting network egress from job containers (for example, a
    `--network` with a proxy that only allows api.anthropic.com, api.openai.com,
    github.com and package mirrors).
  - Keeping API keys out of the container entirely, with a host-side proxy
    that injects them (`ANTHROPIC_BASE_URL=http://proxy`).
- Bot replies and job stderr logs are redacted for exact secret values (including
  strings in mounted credential JSON), which is a last-ditch
  guard rather than a boundary.

## Setup

1. **Bot account:** create a GitHub machine user (e.g. `bootc-bot`). Give it a
   classic PAT with `notifications` and `public_repo`.
2. **Membership token** (only if you set `[auth].org`): create a classic PAT
   on *your* account with `read:org` only.
3. **Image:** `podman build -t localhost/bootc-bot-agent -f bot/agent.Containerfile bot/`.
   This is a plain Fedora image, deliberately not the toolbox image: jobs
   don't need toolbox's host integration or the rpm-ostree/cosa build tree,
   and a smaller image means less for a job to misuse.
4. **Config:**

       mkdir -p ~/.config/bootc-bot
       cp bot/config.example.toml ~/.config/bootc-bot/config.toml
       install -m 600 bot/env.example ~/.config/bootc-bot/env        # fill in tokens

   The example enables `claude`, using a long-lived token from
   `claude setup-token` (billed to your Claude subscription) stored as a podman
   secret, so jobs never see your own `~/.claude` login:

       claude setup-token     # prints a URL to open on any machine, then the token
       read -s TOK; printf %s "$TOK" | podman secret create jmarrero-bot-claude-token -; unset TOK

   Each agent gets only the secrets in its `[agents.<name>.secrets]`, as env
   vars; your CLI settings, plugins and history are not mounted. botd reads the
   secret values only to redact them. The commands run noninteractively and
   rely on the disposable container for isolation. Trigger one with
   `@<bot-account> claude <request>`.

   The example also enables `opencode` with GitHub Copilot models. Store its
   login with `bot/copilot-login.sh` (a device code you enter on any browser,
   as the account holding the Copilot license) and trigger it with
   `@<bot-account> opencode <request>`.

5. **Check:** `set -a; . ~/.config/bootc-bot/env; set +a; python3 bot/botd.py --check`
6. **Run always-on** (on the host, not in a toolbox). botd is a plain systemd
   user service rather than a Quadlet: it has to launch podman containers
   itself, so running it in a container would mean handing it the podman
   socket, which is host access anyway.

       systemctl --user link $PWD/bot/bootc-bot.service
       systemctl --user enable --now bootc-bot
       sudo loginctl enable-linger $USER    # keep running without a login session
       journalctl --user -u bootc-bot -f

## Limits

- Latency is roughly 60s, because GitHub sets the notification poll interval.
- Only issue/PR comments and issue/PR descriptions are read. Inline PR review
  comments are not.
- Repos are cloned anonymously, so private repos (even ones trusted users
  mention the bot in) fail to clone.
- State (which comments were handled) is kept in `~/.local/state/bootc-bot/`.
