# Bot setup runbook

How jmarrero's agent bot is set up, modeled on cgwalters' harness
([cgwalters-bot/homegit](https://github.com/cgwalters-bot/homegit) and the
[cgwalters-forge](https://github.com/cgwalters-forge) org). It lists every
step, in order, so the setup can be checked or redone by someone else; see
[Setting this up for someone else](#setting-this-up-for-someone-else) first.

## The pieces

| Piece | What it does | Where |
|---|---|---|
| Bot account | A separate GitHub user that does all the bot's work | `jmarrero-bot` |
| Forge org | Forks where the bot opens draft PRs for the operator's review; the tracker; the Workstream board | `jmarrero-forge` |
| botd | Answers `@jmarrero-bot <agent> <request>` mentions by running the agent in a disposable, credential-free container | `bot/botd.py`, systemd user service |
| Coordinator | Long-lived Claude Code session running homegit's `coordinator` skill: polls, moves the board, dispatches workers | `bot/coordinator/`, Quadlet |
| homegit | cgwalters' harness (tools and skills), configured by an operator config, run unmodified from a fork | `jmarrero-forge/homegit` |
| Devspaces | Ephemeral RHEL 10 runners (bootc-dev's `rhel10` group) where all builds and tests run, reached over a tailnet | `bootc-dev/jmarrero-devspace-sandbox` |

| Name | Value |
|---|---|
| Operator (the only human whose requests count) | `jmarrero`, signing off as `Joseph Marrero Corchado <jmarrero@redhat.com>` |
| Bot account and commit identity | [`jmarrero-bot`](https://github.com/jmarrero-bot), `Joseph Marrero <jmarrero+bot@gmail.com>` |
| Forge org, board, tracker | [`jmarrero-forge`](https://github.com/jmarrero-forge), [Workstream](https://github.com/orgs/jmarrero-forge/projects/1), [`jmarrero-forge/tracker`](https://github.com/jmarrero-forge/tracker) |
| Devspace repo, tailnet | [`bootc-dev/jmarrero-devspace-sandbox`](https://github.com/bootc-dev/jmarrero-devspace-sandbox), `jmarrero-bot.github` |
| Bot machine | `trashcan`, booting `ghcr.io/jmarrero/bootc-macpro61tc:latest` from this repo |

## Status

Working (2026-10-02): botd with the `claude` and `opencode` agents; the forge
org, tracker, board and forks; homegit's tools under the operator config
(read-only checks pass); devspaces over the tailnet; the coordinator
container, started but not yet told to run.

Nothing is left to set up. The bot's Write access to `jmarrero/atomic-setup`,
used while setting this up, was removed on 2026-10-02 ([§9](#9-security-notes)).

## Setting this up for someone else

**You need:** a GitHub account (the operator); a second GitHub account for
the bot; a Claude subscription (Pro or Max) for `claude setup-token`;
optionally a GitHub Copilot license; a Linux machine with podman, python3,
git and Tailscale that runs only the bot; and, for devspaces, a bootc-dev
admin to give your devspace repo the `rhel10` runner group. This repo's
`Containerfile` is jmarrero's own machine image (a 2013 Mac Pro); you don't
need it, only the `bot/` directory.

**Change these names** (each file names them once or a few times):

| What | Files |
|---|---|
| Operator login, name and sign-off email; bot login and commit identity | `bot/operator.json`, `bot/agent.Containerfile` (git identity), `bot/setup-forge.py` (`ORG`, `BOT`, `HUMAN`, `COMMITTER`, profile texts) |
| Who may trigger botd (numeric ids: `curl https://api.github.com/users/LOGIN \| jq .id`) and in which orgs | `bot/config.example.toml` (`[auth]`, `allowed_owners`) |
| Forge org and repos to fork | `bot/setup-forge.py` (`ORG`, `FORKS`), `bot/operator.json` |
| Podman secret names (`jmarrero-bot-*`) | `bot/config.example.toml`, `bot/copilot-login.sh`, `bot/coordinator/bot-coordinator.container` |
| Bot login and homegit fork URL in the coordinator | `bot/coordinator/Containerfile` (`BOT_LOGIN`, `HOMEGIT_URL`) |
| Devspace repo, host prefix and Tailscale tag | `bot/operator.json` (`devspace`), `bot/tailscale-policy.hujson`, and the devspace repo's own workflow ([§4](#4-devspaces)) |
| Path of this checkout | `bot/bootc-bot.service` (`ExecStart`) |

## 1. The bot machine

The machine runs only LLM agents and the bot; no manual commits come from it.

1. jmarrero's machine boots the image built from this repo's `Containerfile`
   (CI publishes it on every push to `main`), which includes Tailscale:
   `sudo bootc switch ghcr.io/jmarrero/bootc-macpro61tc:latest`. Elsewhere,
   install podman, python3, git and Tailscale.
2. Set the global git identity to the bot's, so every commit made here is
   attributed to it:

       git config --global user.name "Joseph Marrero"
       git config --global user.email "jmarrero+bot@gmail.com"

3. Keep user services running without a login session:
   `sudo loginctl enable-linger $USER`.

## 2. GitHub accounts and tokens

1. Create the bot account (a separate GitHub user). `setup-forge.py`
   ([§3](#3-forge-org-tracker-and-board)) gives it a profile README saying it is
   an agent account and who operates it.
2. Add and verify the bot's commit email on the bot account, so its commits
   are attributed to it, and enable two-factor authentication (tokens keep
   working; 2FA protects the web login).
3. As the bot, create a **classic** personal access token (fine-grained
   tokens can't read notifications) with exactly these scopes: `repo`,
   `workflow`, `gist`, `notifications`, `project`, `read:org`. Leave
   `delete_repo`, `admin:org` and everything else unchecked: the bot owns the
   forge org, so those would let a bad run delete repos or change the org.
   Changing a classic token's scopes later keeps its value.
4. Store it on the host only:

       mkdir -p ~/.config/bootc-bot
       install -m 600 bot/env.example ~/.config/bootc-bot/env
       # set BOTD_GITHUB_TOKEN to the bot's token

5. Make it a podman secret, for the containers that act as the bot (never in
   an image or a `hosts.yml`):

       set -a; . ~/.config/bootc-bot/env; set +a
       printf %s "$BOTD_GITHUB_TOKEN" | podman secret create --replace jmarrero-bot-gh-token -

   A container started with
   `--secret jmarrero-bot-gh-token,type=env,target=GH_TOKEN` is logged in as
   the bot, and git pushes over https through gh's credential helper. Redo
   this whenever the token is rotated.

botd can also allow a whole org (`[auth].org`); that needs a classic
`read:org` token from a member of that org as `BOTD_MEMBERSHIP_TOKEN`. It's
off here: only listed users can trigger the bot.

## 3. Forge org, tracker and board

1. As the operator, create the forge org (the free plan is enough;
   everything in it is public except `bot-ops`) and add the bot as an
   **owner**.
2. Run the setup script as the bot. It's idempotent: rerun it after changing
   `FORKS`.

       set -a; . ~/.config/bootc-bot/env; set +a
       python3 bot/setup-forge.py

   It creates, as homegit's
   [docs/bootstrap.md](https://github.com/jmarrero-forge/homegit/blob/main/docs/bootstrap.md)
   requires:
   - `tracker`, with labels `question`, `review`, `chore`, `blocked`, and
     issue #1 "Bot heartbeat", pinned and locked (`heartbeat_issue`)
   - `bot-ops`, **private**, with locked issue #1 "Bot usage" (usage
     snapshots must not be public)
   - `.github`, the org profile ("not for upstream review")
   - the bot's own `jmarrero-bot/jmarrero-bot`: its profile README, with the
     `#llms` section that `Generated-by:` lines link to, and where pings from
     others are filed (`bot.issue_repo`)
   - forks, with Actions disabled: `ostreedev/ostree`, `coreos/rpm-ostree`,
     `bootc-dev/bootc`, and homegit itself
   - the Workstream board, public and linked to the tracker, with every
     field in homegit's docs/bootstrap.md table (the tools look them up by
     name): Status (Todo, In Progress, Draft, Needs human, In Review,
     Done), Priority (P0–P2), Workflow (branch, analysis, pr, manual), Org
     (one per upstream org, the bot, the forge org, other), Est. cost (XS
     to XL, for pacing), text fields Why, Branch, Gist, News, Lead and
     Run, and number fields Budget tokens and Actual tokens. The optional
     Theme and the unused Verdict fields are left out.

## 4. Devspaces

Builds and tests run only on ephemeral RHEL 10 runners from bootc-dev's
`rhel10` runner group, reached over a tailnet. The workflow comes from
[bootc-dev/cgwalters-devspace-sandbox](https://github.com/bootc-dev/cgwalters-devspace-sandbox).

1. As a bootc-dev admin, create a **public, empty** repo
   `bootc-dev/<you>-devspace-sandbox`. A repo can't be forked into the org
   that owns it, so it's a copy with history: clone cgwalters', rename its
   `origin` to `upstream`, add the new repo as `origin` and push `main`.
   `git fetch upstream && git merge upstream/main` picks up his fixes later.
2. Adapt it (jmarrero's two commits on top of cgwalters' history): the
   repo name in `devspace.rs` and `Cargo.toml`, the host prefix
   `cgwalters-devspace-` → `jmarrero-devspace-`, the Tailscale tag
   `tag:bootc-dev-sandbox` → `tag:jmarrero-devspace`, and `just init`
   installing no dotfiles. Run `cargo test` before pushing.
3. Add the bot as a collaborator with **Write** (enough to dispatch and
   cancel runs).
4. Runner access: bootc-dev → Settings → Actions → Runner groups → `rhel10`
   must allow the repo. It's set to all repositories, public ones included.
5. The tailnet, logged in to Tailscale as the bot (here `jmarrero-bot.github`,
   with MagicDNS on; it holds only the bot machine and the devspaces):
   - On the bot machine: `sudo tailscale up`, logging in via the printed URL.
   - Access controls → JSON editor: replace the default allow-all policy
     with [`tailscale-policy.hujson`](tailscale-policy.hujson). It lets
     `tag:bot-host` reach `tag:jmarrero-devspace` on TCP 22 and nothing else,
     and its tests reject any later edit that lets a devspace reach the
     host. Containers on the bot machine share its network, so whatever it
     can reach, they can too.
   - Tag the bot machine: `sudo tailscale up --advertise-tags=tag:bot-host`.
   - Key expiry is left on deliberately, so the machine gets looked at every
     few months; trashcan's key expires on **2027-03-31**. Only devspace
     access breaks then (botd, GitHub and the models don't use the tailnet).
     Renew with `sudo tailscale up --force-reauth --advertise-tags=tag:bot-host`
     and the printed URL, logged in as the bot.
   - Settings → Trust credentials → OpenID Connect: issuer GitHub Actions
     (`https://token.actions.githubusercontent.com`), scope Keys → Auth
     Keys: Write with tag `tag:jmarrero-devspace`, and subject
     `repo:bootc-dev@202312630/jmarrero-devspace-sandbox@1400847798:ref:refs/heads/main`.
     The repo uses GitHub's immutable subjects, which include the org and
     repo ids; get yours with
     `gh api repos/bootc-dev/<repo>/actions/oidc/customization/sub`
     (`sub_claim_prefix`) and append `:ref:refs/heads/main`. A repo
     re-created under the same name can't use the credential, and only runs
     from `main` are accepted, so a pushed branch with a modified workflow
     can't get a key.
   - Set the credential's Client ID and Audience as the devspace repo's
     Actions **variables** `TS_OAUTH_CLIENT_ID` and `TS_AUDIENCE` (they
     aren't secrets).
6. Test: dispatch `devspace.yml` with a throwaway SSH public key, 30
   minutes, 4 cores. On 2026-10-02 it joined the tailnet in about 40s as a
   tagged device; the bot machine and the bot container could SSH in as
   `runner` (in a container, the key must be readable by its user, e.g. with
   `--userns=keep-id:uid=2000,gid=2000`); the devspace could not reach the
   bot machine; cancelling the run removed it from the tailnet.
7. The workflow installs no build toolchain (no podman or cargo; it has KVM,
   just and the agent CLIs). homegit's `bot-devspace` installs it over SSH
   (`provision`) after starting a devspace.

## 5. Model credentials

Each is a podman secret made for the bot: revocable without logging you out
anywhere, and none is your own CLI login. Containers can read the credential
they're given, so revoke and recreate it if one ever leaks.

1. **Claude** (billed to the Claude subscription). `claude setup-token`
   prints a URL to open on any machine, then the token:

       claude setup-token
       read -s TOK; printf %s "$TOK" | podman secret create jmarrero-bot-claude-token -; unset TOK

   (In fish, `set -e TOK` instead of `unset TOK`.) Don't save the token to a
   file: on btrfs, `shred` can't guarantee it's gone.
2. **GitHub Copilot through opencode** (optional; billed to the license
   holder's premium requests). Run in a real terminal; it prints a code to
   enter at `https://github.com/login/device`, logged in as the account with
   the Copilot license:

       bot/copilot-login.sh

   The token only grants Copilot; revoke it under GitHub Settings →
   Applications.
3. Codex is not used: cgwalters uses opencode for OpenAI models, and Codex
   has no bot-specific token, only your own ChatGPT login.

## 6. botd

The @mention responder; see [README.md](README.md) for how it works and its
security model.

1. Build the job image (rebuild after changing `bot/agent.Containerfile`):

       podman build -t localhost/bootc-bot-agent -f bot/agent.Containerfile bot/

2. Configure it:

       cp bot/config.example.toml ~/.config/bootc-bot/config.toml

   Here only three users can trigger it, by numeric GitHub id: jmarrero
   (`trusted_user_ids`, any repo), and cgwalters and cgwalters-bot
   (`allowed_user_ids`, only in bootc-dev, jmarrero-forge and cgwalters-forge
   repos). Agents: `claude` and `opencode` (§5).
3. Check, then run it as a user service (on the host: it starts podman
   containers itself):

       set -a; . ~/.config/bootc-bot/env; set +a; python3 bot/botd.py --check
       systemctl --user link $PWD/bot/bootc-bot.service
       systemctl --user enable --now bootc-bot
       journalctl --user -u bootc-bot -f

Trigger it with `@jmarrero-bot claude <request>` or
`@jmarrero-bot opencode <request>` at the start of a comment line. Any other
mention of the bot by the operator goes to the coordinator instead, as work
on the board; the two share the bot's notifications without stepping on each
other ([README.md](README.md#botd-and-the-coordinator)). When you add or
rename a botd agent, update `BOT_NOTIFY_SKIP_COMMANDS` in
`coordinator/bot-coordinator.container` to match.

## 7. homegit

homegit's tools take an operator config, so they run unmodified from a fork
([docs/bootstrap.md](https://github.com/jmarrero-forge/homegit/blob/main/docs/bootstrap.md)).

1. `setup-forge.py` forks `cgwalters-bot/homegit` into the forge org (§3).
   The fork carries one change of ours, `4f3a6f9`: `bot-cost` and `bot-retro`
   take their defaults from the operator config too. The review app still
   names cgwalters' setup.
2. Install the operator config, [`operator.json`](operator.json):

       install -D -m 644 bot/operator.json ~/.config/bot-harness/operator.json

   `operator.name`/`email` is the `Signed-off-by` that `bot-pr promote` and
   `signoff` add, only after the operator approved that exact head;
   `bot.git_name`/`git_email` is what the bot commits as.
3. Check it (read-only) in the bot image, with a clone of the fork:

       git clone https://github.com/jmarrero-forge/homegit.git ~/development/github/jmarrero-forge/homegit
       podman run --rm --userns=keep-id:uid=2000,gid=2000 \
         --secret jmarrero-bot-gh-token,type=env,target=GH_TOKEN \
         -v ~/development/github/jmarrero-forge/homegit:/home/agent/src/github/jmarrero-bot/homegit:ro,z \
         -v ~/.config/bot-harness:/home/agent/.config/bot-harness:ro,z \
         localhost/bootc-bot-agent bash -c 'cd ~/src/github/jmarrero-bot/homegit &&
           PATH=$PWD/bin:$PATH; bot-operator --json; bot-board list;
           bot-watch --dry-run; bot-notify --dry-run'

   All passed on 2026-10-02 against the jmarrero-forge board.
4. `upstream-policy/` in the fork holds cgwalters' per-repo contribution
   policy records, which `bot-pr promote` checks before opening any upstream
   PR. We use them as they are (decided 2026-10-02: we trust cgwalters'
   review); only the operator can loosen a verdict. Checked with
   `upstream-policy check OWNER/REPO` in the coordinator container:
   `bootc-dev/bootc` is `bot-ok` (the bot writes code and text, with an AI
   trailer); `ostreedev/ostree` and `coreos/rpm-ostree` are `human-text` (the
   bot's code is fine, but the PR title and body, commit messages and
   comments must be the operator's own: rewrite them on the fork PR, then
   `/promote --human-text`). A record goes stale, and the gate refuses, when
   the project's policy files change upstream.
5. The tools keep their shared state (handled notifications, the last sweep,
   the work lease, tmt numbers) in five archived draft items on the board:
   `bot-state: notifications`, `watch`, `pr-inbox`, `lease` and
   `tmt-numbers`. Their ids are `board.state_items` in `operator.json`, so
   every machine and container finds the same items. They were created
   empty (the same as no state) with `bot-board state-put NAME '{}'`, which
   prints each id; a new setup does the same once, then adds the ids.

## 8. The coordinator

See [coordinator/README.md](coordinator/README.md). It keeps its own homegit
clone in its home volume, so the clone in §7 is only for checks. It uses
homegit's skills rendered for this operator, with the goals in
`~/.config/bot-harness/goals.md`:

    install -m 644 bot/coordinator/operator-goals.md ~/.config/bot-harness/goals.md
    install -m 644 bot/coordinator/operator-notes.md ~/.config/bot-harness/notes.md

The notes also tell it when to hand reviews and well-defined work to
opencode (`bot-opencode`, with the Copilot login from §5).

    podman build -t localhost/bot-coordinator bot/coordinator/
    mkdir -p ~/.config/containers/systemd
    ln -s $PWD/bot/coordinator/bot-coordinator.container ~/.config/containers/systemd/
    systemctl --user daemon-reload && systemctl --user start bot-coordinator
    podman exec -it bot-coordinator tmux attach -t coordinator   # Ctrl-b d detaches

It starts at boot and starts working on its own (`COORDINATOR_PROMPT`), and
systemd restarts it if the session ends; `systemctl --user stop
bot-coordinator` stops it.

## 9. Security notes

- **Credentials** live only on the bot machine: `~/.config/bootc-bot/env`
  (mode 600) and podman secrets. None is in this repo, an image, or a
  devspace.
- **botd's jobs** get no GitHub token, only their model credential, and run
  as a subordinate uid rather than your user; see
  [README.md](README.md#security-model).
- **The coordinator** holds the bot's GitHub token and runs Claude with
  permission prompts skipped, the same trade-off cgwalters' setup makes. It
  acts only on the operator's requests (homegit's `bot-notify`), and only
  the operator's approval of an exact head sends work upstream. It doesn't
  map to your host user.
- **The tailnet** lets the bot machine reach devspaces on port 22 and
  nothing else; devspaces can't reach the bot machine.
- **This repo builds the bot machine's OS image**, so whoever can push to it
  can change what the machine runs after its next `bootc upgrade` (automatic
  updates are off). The bot has no write access to it: agents on the bot
  machine can commit here, but the operator reviews and pushes those
  commits from his own machine (e.g. `git pull trashcan:<path> main` there,
  then push). Don't give the bot write access again.
- CI uses least-privilege `permissions` and actions pinned to commit SHAs;
  `.containerignore` keeps `bot/` and the rest out of the OS image.
