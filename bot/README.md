# bootc-bot

See [SETUP.md](SETUP.md) for the full runbook of how this bot, its forge
org, devspaces and credentials were set up.

Only jmarrero, cgwalters and cgwalters-bot can trigger it: jmarrero on any
repo the bot account can see, the other two in `bootc-dev`, `jmarrero-forge`
and `cgwalters-forge` repos. Comment:

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
- Bot replies and job stderr logs are redacted for exact secret values (the
  tokens botd holds, the agents' podman secrets, and strings in any mounted
  credential JSON), which is a last-ditch guard rather than a boundary.

## Setup

See [SETUP.md §6](SETUP.md#6-botd) (and §2 and §5 for its token and model
credentials). The job image is a plain Fedora image, deliberately not the
toolbox image: jobs don't need toolbox's host integration or build tree, and
a smaller image means less for a job to misuse. botd is a systemd user
service rather than a Quadlet because it starts podman containers itself;
running it in a container would mean handing it the podman socket, which is
host access anyway.

## Limits

- Latency is roughly 60s, because GitHub sets the notification poll interval.
- Only issue/PR comments and issue/PR descriptions are read. Inline PR review
  comments are not.
- Repos are cloned anonymously, so private repos (even ones trusted users
  mention the bot in) fail to clone.
- State (which comments were handled) is kept in `~/.local/state/bootc-bot/`.
