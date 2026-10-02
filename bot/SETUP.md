# Bot setup runbook

How jmarrero's agent bot was set up, modeled on cgwalters' harness
([cgwalters-bot/homegit](https://github.com/cgwalters-bot/homegit) and the
[cgwalters-forge](https://github.com/cgwalters-forge) org). It lists every
manual step, in order, so the setup can be checked or redone. Steps marked
**TODO** are not done yet.

| Thing | Value |
|---|---|
| Operator (the only human whose requests count) | `jmarrero` |
| Bot account | [`jmarrero-bot`](https://github.com/jmarrero-bot) |
| Bot commit identity | `Joseph Marrero <jmarrero+bot@gmail.com>` |
| Forge org | [`jmarrero-forge`](https://github.com/jmarrero-forge) |
| Board | [Workstream](https://github.com/orgs/jmarrero-forge/projects/1) |
| Tracker | [`jmarrero-forge/tracker`](https://github.com/jmarrero-forge/tracker) |
| Devspace repo | [`bootc-dev/jmarrero-devspace-sandbox`](https://github.com/bootc-dev/jmarrero-devspace-sandbox) |
| Bot machine | `trashcan`, booting `ghcr.io/jmarrero/bootc-macpro61tc:latest` from this repo |

## 1. The bot machine

`trashcan` runs only LLM agents and the bot; no manual commits come from it.

1. Boot the image built from this repo's `Containerfile` (CI publishes it on
   every push to `main`): `sudo bootc switch ghcr.io/jmarrero/bootc-macpro61tc:latest`.
2. Set the global git identity to the bot's, so every commit made here is
   attributed to it:

       git config --global user.name "Joseph Marrero"
       git config --global user.email "jmarrero+bot@gmail.com"

3. Keep user services running without a login session:
   `sudo loginctl enable-linger $USER`.
4. Tailscale (for devspaces) is in the image. After the image that adds it is
   booted, log in once. **TODO**:

       sudo bootc upgrade && systemctl reboot
       sudo tailscale up      # prints a URL; log in to your own tailnet

## 2. GitHub accounts and tokens

1. Create the bot account `jmarrero-bot` (a separate GitHub user). Give it a
   profile README saying it is an agent account operated by jmarrero.
2. Add and verify `jmarrero+bot@gmail.com` as an email on jmarrero-bot, so
   its commits are attributed to the bot account, and enable two-factor
   authentication on it (tokens keep working; 2FA protects the web login).
3. As jmarrero-bot, create a **classic** personal access token (fine-grained
   tokens can't read notifications) with exactly these scopes:
   `repo`, `workflow`, `gist`, `notifications`, `project`, `read:org`.
   Leave `delete_repo`, `admin:org` and everything else unchecked: the bot owns
   the forge org, so those would let a bad run delete repos or change the org.
   Changing a classic token's scopes later keeps its value.
4. (Not used.) botd's optional org-wide access (`[auth].org`) needs a
   classic `read:org` token from a member of that org as
   `BOTD_MEMBERSHIP_TOKEN`. It's off, since only listed users can trigger the
   bot, and the old token was revoked on 2026-10-01.
5. Store both on the host only:

       mkdir -p ~/.config/bootc-bot
       install -m 600 bot/env.example ~/.config/bootc-bot/env
       # set BOTD_GITHUB_TOKEN (the bot's token)

6. Give the bot container `gh` and git push access through a podman secret
   (never in the image or a `hosts.yml`):

       set -a; . ~/.config/bootc-bot/env; set +a
       printf %s "$BOTD_GITHUB_TOKEN" | podman secret create --replace jmarrero-bot-gh-token -

   A container started with
   `--secret jmarrero-bot-gh-token,type=env,target=GH_TOKEN` is logged in as
   jmarrero-bot, and git uses the token through gh's credential helper.
   Redo this whenever the token is rotated.

## 3. Forge org, tracker and board

1. As jmarrero, create the org `jmarrero-forge` (the free plan is enough;
   everything in it is public) and add jmarrero-bot as an **owner**.
2. Run the setup script as the bot. It's idempotent, so rerun it after
   adding a fork to `FORKS`:

       set -a; . ~/.config/bootc-bot/env; set +a
       python3 bot/setup-forge.py

   It creates:
   - `jmarrero-forge/tracker`, with labels `question`, `review`, `chore`,
     `blocked`
   - `jmarrero-forge/.github`, the org profile ("not for upstream review")
   - forks of `ostreedev/ostree`, `coreos/rpm-ostree` and `bootc-dev/bootc`,
     with Actions disabled
   - the Workstream board, public and linked to the tracker, with the fields
     homegit's tools expect: Status (Todo, In Progress, Draft, Needs human,
     In Review, Done), Priority (P0–P2), Workflow (branch, analysis, pr,
     manual), Org (one per upstream org, plus other), and text fields Why,
     Branch and Gist

## 4. Devspace runners

Builds and tests run on ephemeral RHEL 10 runners from bootc-dev's `rhel10`
runner group, reachable over a tailnet; see
[bootc-dev/cgwalters-devspace-sandbox](https://github.com/bootc-dev/cgwalters-devspace-sandbox).

1. As jmarrero (a bootc-dev admin), create the **public, empty** repo
   `bootc-dev/jmarrero-devspace-sandbox`. A repo can't be forked into the org
   that owns it, so it's a copy with history instead.
2. Add jmarrero-bot to it as a collaborator with **Write**, which is enough
   to dispatch runs and push.
3. Push Colin's history and the adaptation (done; the clone is
   `~/development/github/bootc-dev/jmarrero-devspace-sandbox`, where
   `upstream` is Colin's repo, so `git fetch upstream && git merge
   upstream/main` picks up his fixes). The adaptation renames the host prefix
   to `jmarrero-devspace-`, the tag to `tag:jmarrero-devspace`, and makes
   `just init` install no dotfiles.
4. Runner access: bootc-dev → Settings → Actions → Runner groups → `rhel10`
   is set to **all repositories** (with public repositories allowed), so
   nothing was needed.
5. The tailnet is `jmarrero-bot.github` (`sudo tailscale up` on trashcan
   logged in as jmarrero-bot via GitHub), with MagicDNS on. It holds only
   trashcan and the devspaces. **TODO** (logged in to Tailscale as
   jmarrero-bot):
   - Access controls: replace the default allow-all policy with
     [`tailscale-policy.hujson`](tailscale-policy.hujson). It lets
     `tag:bot-host` reach `tag:jmarrero-devspace` on TCP 22 and nothing else,
     and its tests reject any later edit that lets a devspace reach the host.
     Job containers share trashcan's network, so whatever trashcan can reach,
     they can too. To copy it from the machine with your browser:
     `ssh trashcan cat tailscale-policy.hujson | wl-copy`.
   - Tag trashcan: `sudo tailscale up --advertise-tags=tag:bot-host` (done).
   - Key expiry is deliberately left on, so the machine gets looked at every
     few months. trashcan's key expires on **2027-03-31**; after that only
     devspace access breaks (botd, GitHub and the models don't use the
     tailnet). Renew it before then, or afterwards, with
     `sudo tailscale up --force-reauth --advertise-tags=tag:bot-host` and the
     printed login URL, logged in as jmarrero-bot.
   - Settings → Trust credentials → OpenID Connect (done): issuer GitHub Actions
     (`https://token.actions.githubusercontent.com`), subject
     `repo:bootc-dev@202312630/jmarrero-devspace-sandbox@1400847798:ref:refs/heads/main`,
     scope Keys → Auth Keys: Write, tag `tag:jmarrero-devspace`. The repo uses
     GitHub's immutable subjects, which include the org and repo ids (see
     `gh api repos/bootc-dev/jmarrero-devspace-sandbox/actions/oidc/customization/sub`),
     so a repo re-created under the same name can't use the credential. Only
     runs from `main` are accepted, so a pushed branch with a modified
     workflow can't get a key.
   - Its Client ID and Audience are the devspace repo's Actions variables
     `TS_OAUTH_CLIENT_ID` and `TS_AUDIENCE` (done; they aren't secrets).
   - Tested on 2026-10-02 with a 30-minute 4-core devspace: it joined the
     tailnet in about 40s as a tagged device, trashcan and the bot container
     (with `--userns=keep-id:uid=2000,gid=2000` so the key is readable) could
     SSH in as `runner`, and the devspace could not reach trashcan. Cancelling
     the run removes it from the tailnet.
   - The workflow installs no build toolchain (no podman or cargo; it has
     KVM, just and the agent CLIs). homegit's `bot-devspace provision`
     installs the toolchain over SSH, so use it (or an equivalent) after
     starting a devspace.

## 5. Model credentials

Each is a podman secret made for the bot, so it can be revoked without
logging you out anywhere, and none is your own CLI login. Jobs can read the
credential they're given, so revoke and recreate it if one ever leaks.

1. **Claude** (billed to the Claude Max subscription). `claude setup-token`
   prints a URL to open on any machine, then the token:

       claude setup-token
       read -s TOK; printf %s "$TOK" | podman secret create jmarrero-bot-claude-token -; set -e TOK

   (`set -e TOK` is fish; in bash use `unset TOK`.) Don't save the token to a
   file: `/var/home` is btrfs, where `shred` can't guarantee it's gone.
2. **GitHub Copilot through opencode** (billed to jmarrero's Copilot
   premium requests). Run in a real terminal; it prints a code to enter at
   `https://github.com/login/device`. Log in as **jmarrero**, the license
   holder:

       bot/copilot-login.sh

   The token only grants Copilot; revoke it under GitHub Settings →
   Applications.
3. Codex is not used. Colin uses opencode for OpenAI models, and Codex has no
   bot-specific token, only your own ChatGPT login.

## 6. botd (the @mention responder)

1. Build the job image (rebuild after changing `bot/agent.Containerfile`):

       podman build -t localhost/bootc-bot-agent -f bot/agent.Containerfile bot/

2. Configure it:

       cp bot/config.example.toml ~/.config/bootc-bot/config.toml

   Only three users can trigger it, by numeric GitHub id: jmarrero
   (`trusted_user_ids`, any repo), and cgwalters and cgwalters-bot
   (`allowed_user_ids`, only in bootc-dev, jmarrero-forge and cgwalters-forge
   repos). No org-wide access.
   Agents: `claude` and `opencode`.

3. Check, then run it as a user service:

       set -a; . ~/.config/bootc-bot/env; set +a; python3 bot/botd.py --check
       systemctl --user link $PWD/bot/bootc-bot.service
       systemctl --user enable --now bootc-bot
       journalctl --user -u bootc-bot -f

Trigger it with `@jmarrero-bot claude <request>` or
`@jmarrero-bot opencode <request>` at the start of a comment line.

## 7. Coordinating with cgwalters' harness

- cgwalters-bot opened the private repo
  [cgwalters-forge/harness-coordination](https://github.com/cgwalters-forge/harness-coordination)
  for the two bots. Its README is the bootstrap guide. jmarrero-bot can read
  it (it needs the `repo` scope for that); jmarrero joins only to take part
  directly.
- Ask questions there by opening an issue that mentions `@cgwalters-bot`. It
  answers but never acts outside that repo on jmarrero-bot's request.
- homegit's tools are still hardcoded to cgwalters. **TODO:** once its
  operator-config change lands, install them with `make install-bin` and
  `make install-crates` (never `make install`, which copies cgwalters'
  dotfiles into `~`), copy the skills, and write your own `AGENTS.md`.
- homegit has no license file yet; ask before copying from it.

## 8. Temporary access to remove

- **TODO:** jmarrero-bot has Write on `jmarrero/atomic-setup`, which builds
  this machine's OS image. Remove it once the setup works: Settings →
  Collaborators on this repo.
- See `~/security-review.md` for the full security review and its fixes.
