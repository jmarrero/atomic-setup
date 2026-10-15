# praxis-broker

The inference broker for the bot's agentic-job runs ([jmarrero-forge/tracker#17](https://github.com/jmarrero-forge/tracker/issues/17)): [cgwalters-bot/praxis-credential-broker](https://github.com/cgwalters-bot/praxis-credential-broker), run here as rootless Podman containers. agentic-job runs its agents in throwaway GitHub Actions runners that hold no model credential. Each run registers at the broker with its job's GitHub OIDC token, gets a run token, and sends its Claude requests through the broker, which adds jmarrero's Claude subscription token, caps the run and meters it. The broker's own docs are its [README](https://github.com/cgwalters-bot/praxis-credential-broker/blob/45fbb14c7a66328ad19e411820fbd58484767e39/README.md) and [INTERNALS.md](https://github.com/cgwalters-bot/praxis-credential-broker/blob/45fbb14c7a66328ad19e411820fbd58484767e39/INTERNALS.md) at the pinned commit; read its "Risks" first. Injecting a subscription token at a gateway is undocumented by Anthropic and may stop working.

Only the Claude pool is set up. Codex and Mistral are left out (see [Adding Codex or Mistral](#adding-codex-or-mistral)).

## What's in the pod

The pod `praxis-credential-broker` has two containers, both pinned by image digest, with no capabilities, no-new-privileges and a read-only root:

- **`praxis-credential-broker-praxis`**: the broker's gateway, upstream's [contrib/quadlet](https://github.com/cgwalters-bot/praxis-credential-broker/tree/45fbb14c7a66328ad19e411820fbd58484767e39/contrib/quadlet) unit with two changes. Its image is pinned by digest rather than following `:main` with `AutoUpdate=registry`, since whoever can push `:main` would control the process that holds the token. And it mounts two directories of this checkout: [praxis/](praxis/praxis.yaml), upstream's `praxis.yaml` with our [caps](#caps) and `/usage` origin, over the one in the image; and [policy/](policy/), the [run-token policy](#run-token-policy). Its only credential is the Claude token, a podman secret.
- **`praxis-credential-broker-tailscale`**: the pod's own tailnet node, `praxis-broker` with `tag:praxis-broker`, in Tailscale's userspace mode: no TUN device, no capabilities, its state in the volume `praxis-credential-broker-tailscale`, and a one-time auth key, a podman secret, for its first login. It hands every connection the tailnet policy lets in to the same port on the pod's loopback, where Praxis listens on 8081.

The codex proxy and provider of upstream's pod aren't here, so `/healthz`, which asks the proxy, answers 502; use the checks under [Verify](#verify) instead.

**Why published images, pinned.** The digest fixes the exact image, and the comment above each `Image=` says which commit and CI run built it (the broker's CI also tags each image with its full commit id). Building from source here instead would build the same pinned commit, so it would trust the same code. What it would remove is the trust in cgwalters-bot's CI to have built that digest from that commit (there are no provenance attestations to check), at the cost of a Rust build of Praxis on this machine and a build step in setup. Pinning closes what was actually open, the moving tag.

**Why a tailnet node in the pod.** The bot host is already on the tailnet as `tag:bot-host`, and upstream's own deployment publishes on its host's tailnet address. Here the runners that reach the broker run untrusted agents, and this machine holds the bot's GitHub token and the coordinator. With a node of its own, the policy grants the runners one port on `tag:praxis-broker` and nothing on `tag:bot-host`, and its tests check that. It also keeps the broker off the bot host's tailnet name, under which the workboard hub must be the only thing served ([../workboard-hub/README.md](../workboard-hub/README.md), step 5), and needs no change to the host's Tailscale.

## Set up

In this order, on the bot machine, from this checkout:

1. **The Claude token.** Make one for the broker alone, separate from the coordinator's `jmarrero-bot-claude-token`, so that each can be revoked without the other. `claude setup-token` prints a URL to open on any machine logged in to the subscription, then the token (`sk-ant-oat01-...`):

       claude setup-token
       read -rs TOK; printf %s "$TOK" | podman secret create praxis-credential-broker-anthropic-oauth -; unset TOK

   In fish: `read -s TOK; printf %s $TOK | podman secret create praxis-credential-broker-anthropic-oauth -; set -e TOK`. No trailing newline: Praxis sends it as it is. The broker's own `scripts/init-anthropic-token`, from a checkout at the pinned commit, does the same and also checks the token's shape.

2. **The tailnet.** In the bot tailnet's admin console, logged in as the bot:

   - paste [../tailscale-policy.hujson](../tailscale-policy.hujson) under Access controls. It adds `tag:praxis-broker` and `tag:agentic-job`, lets the runners and jmarrero's devices reach the broker on TCP 8081 and nothing else, and its tests refuse any later edit that lets a runner reach the bot host, a devspace or another port of the broker, or the bot host reach the broker;
   - Settings → Keys → Generate auth key, for the broker's node: not reusable, not ephemeral, pre-approved, tagged `tag:praxis-broker`, with the shortest expiry that covers step 3. Make it a podman secret (in fish, as in step 1):

         read -rs TOK; printf %s "$TOK" | podman secret create praxis-credential-broker-tailscale-authkey -; unset TOK

     The node logs in with it once, on its first start, and from then on with its own key in the state volume; the used auth key is worth nothing, so the secret can stay. A tagged node's key expiry is off by default.

3. **Start it:**

       ln -s $PWD/bot/praxis-broker ~/.config/containers/systemd/
       systemctl --user daemon-reload && systemctl --user start praxis-credential-broker-pod

   Link the directory, not the unit files: the units mount `./praxis` and `./policy` relative to where Quadlet finds them, which through the directory link is this checkout. Those mounts are `:z`, so on the first start Podman relabels `bot/praxis-broker/praxis/` and `bot/praxis-broker/policy/` to `container_file_t` under SELinux: a deliberate change to the host, confined to those two directories of the checkout, which stay readable and writable to you as before. `loginctl enable-linger` is already on ([../SETUP.md](../SETUP.md) §1), which keeps the pod running at boot without a login.

4. **Its address.** In the admin console's Machines, check that `praxis-broker` is there with `tag:praxis-broker` and its key expiry disabled. Its tailnet address is what the runners use:

       podman exec praxis-credential-broker-tailscale tailscale ip -4

   The runners join with `--accept-dns=false`, and agentic-job's sandbox allows the broker only as an IPv4 address and port, so they can't use its MagicDNS name. The address stays the node's as long as the node exists; to choose it, edit the machine's IPv4 address in the admin console, then restart the pod. If the node isn't there, `podman logs praxis-credential-broker-tailscale` says why (a used-up or expired auth key: make a new one and replace the secret with `--replace`).

5. **The runners' tailnet login.** Settings → Trust credentials → OpenID Connect, as for the devspaces ([../SETUP.md](../SETUP.md) §4): issuer GitHub Actions (`https://token.actions.githubusercontent.com`), scope Keys → Auth Keys: Write with tag `tag:agentic-job`, and subject

       repo:jmarrero-forge@336730605/tracker@1400840502:ref:refs/heads/main

   The tracker uses GitHub's immutable subjects (`gh api repos/jmarrero-forge/tracker/actions/oidc/customization/sub`). A job of a reusable workflow gets the caller's subject, so this admits the agent jobs of the tracker's workflows run from `main`, the ones the dispatch wrapper starts, and nothing from another branch or repository. The credential's Client ID and audience go into jmarrero-forge/actions' `tailscale/tailnets.json` entry for jmarrero-forge ([jmarrero-forge/tracker#20](https://github.com/jmarrero-forge/tracker/issues/20)); neither is a secret.

   Runner nodes must be ephemeral, so that a finished run's node leaves the tailnet with its runner. The credential can't require that; the join does. jmarrero-forge/actions' `tailscale` action (as of jmarrero-forge/actions#1) joins with tailscale/github-action v4.2.0, pinned by commit, which logs in through the OIDC credential with `preauthorized=true&ephemeral=true` and keeps tailscaled's state in memory (`--state=mem:`). Keep it that way when that action changes.

6. **Point the caller at it**, as the tracker's repository variables or the wrapper's defaults:

       INFERENCE_URL=http://ADDRESS:8081         # ADDRESS from step 4
       INFERENCE_AUDIENCE=jmarrero-praxis-broker

   The [run-token policy](#run-token-policy) admits the tracker's wrapper at the commit its `dispatch.yml` pins, so once the pod is up, runs from the tracker register.

## Verify

On this machine, Praxis is on loopback port 18080:

    curl -s http://127.0.0.1:18080/usage                  # praxis-broker-usage/v1 JSON
    curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:18080/anthropic/v1/messages/count_tokens   # 403: the Anthropic routes are served (POST only)
    curl -s -o /dev/null -w '%{http_code}\n' -X POST http://127.0.0.1:18080/v1/runs                     # 401: the policy is loaded and wants an OIDC token; 503 means no policy file
    curl -s -D - -o /dev/null -H 'Origin: https://jmarrero-forge.github.io' http://127.0.0.1:18080/usage | grep -i '^access-control-allow-origin'
    podman logs praxis-credential-broker-praxis 2>&1 | grep -i -m3 'warn\|error'   # the called_workflows warning is expected; "no run registration policy" means the file is missing

From one of jmarrero's devices on the bot's tailnet, `curl http://ADDRESS:8081/usage` should answer the same JSON; a timeout means the policy of step 2 or the node's login of step 3 isn't in place. From a runner, jmarrero-forge/actions' `tailscale` action with `check: 'true'` joins, then expects 401 from `POST /v1/runs`. After the first real run, `/usage` has `anthropic.counts` and `anthropic.unified_5h`, and the log has its `run registered` and `request usage` lines. A 401 from Anthropic in those lines means the token was refused.

## Caps

The broker caps injected Claude use at 10M weighted tokens per run (upstream: 20M) and 30M weighted tokens for all runs together in any 5 hours (upstream: 100M), where cache reads count a tenth. A request over either gets 429 before it reaches Anthropic. These come from upstream's one data point, in [praxis/praxis.yaml](praxis/praxis.yaml): 13.24M weighted tokens were 9% of cgwalters' plan's 5-hour window. If jmarrero's plan is the same size, the window cap lets runs use about a fifth of a 5-hour window and a run about 7%, leaving the rest to the coordinator, which uses the same subscription without the broker, so the broker can't count it. On a smaller plan, Anthropic's own limit binds first. `/usage`'s `anthropic.unified_5h` and `unified_7d` show what the subscription's own windows say, as of the last response. The per-run limits in the policy stay upstream's defaults: 6 hours and 4 requests in flight. Restarting the pod or reloading `praxis.yaml` resets the caps.

## Run-token policy

The policy decides which jobs may register runs, by their OIDC token's claims (upstream's INTERNALS.md, "Run tokens" and "Admitting more workflows"). It is [policy/run-token-policy.yaml](policy/run-token-policy.yaml), which the gateway reads when it starts: a change to it takes effect after `git pull` and a restart of the pod (as under [Updating](#updating)), while no run is active, since a restart forgets registered runs. Without the file, `POST /v1/runs` answers 503 and only pass-through requests are served; a policy with no entry stops the gateway at startup.

It has a deployment-specific audience, `jmarrero-praxis-broker`, which the caller's `INFERENCE_AUDIENCE` must match, so that an identity token made out to another praxis broker is no good here. Callers are pinned to the tracker by repository id (1400840502), and only `workflow_dispatch`, which needs write access to the tracker, is admitted. Which entry admits a run depends on which workflow file holds its agent job, since that file is the token's `job_workflow_ref`:

- **The tracker's wrapper in jmarrero-forge/actions**, `jmarrero-agentic-job.yml`, whose agent job joins the tailnet ([jmarrero-forge/tracker#20](https://github.com/jmarrero-forge/tracker/issues/20)): a `called_workflows` entry naming the wrapper's file, with the commit the tracker calls it at, `2a2825fa768d` (from jmarrero-forge/actions#1, on its `main`). The tracker's `dispatch.yml` names that full commit in its `uses:`, so the identity token's `job_workflow_ref` ends in it and its `job_workflow_sha` is it, which is what `sha` matches. The entry names the commit rather than a branch, so that a change to what may spend the subscription is a pull request here, which jmarrero merges. The cost is one such pull request, and a restart, each time the tracker's pin moves. With `ref: refs/heads/main` instead, whoever can push the wrapper's `main` (the bot included) would decide what admitted jobs do, with no change here.
- **agentic-job's complete reusable workflow** (`cgwalters-forge/agentic-job/.github/workflows/agentic-job.yml`) isn't admitted: nothing here calls it, and under `called_workflows` its caller chooses the runner, inputs and setup, so an entry for it would admit more than the wrapper needs.
- **An agent job written into a tracker workflow itself** would need a `workflows` entry, its exact `job_workflow_ref` with `repository_ids: [1400840502]`, since that workflow is in the run's own repository.

**Moving the wrapper's pin.** A new wrapper commit on jmarrero-forge/actions' `main` reaches runs only once the tracker's `dispatch.yml` names it, and the broker admits it only once the policy does. To move without a gap, add a second `called_workflows` entry, the same but for the new full commit id, by a pull request here, then pull and restart the pod while no run is active. Then move the tracker's `uses:` to that commit, and once it has merged, drop the old entry the same way. Check first that the commit is on the wrapper's `main`: `gh api repos/jmarrero-forge/actions/compare/SHA...main -q .status` says `ahead` or `identical`.

agentic-job's `review.yml` runs on `pull_request_target` and `issue_comment` and joins cgwalters' tailnet itself, so no entry admits it. Never add `unproven` or `any_workflow`: they let any tailnet peer, or any workflow of an admitted repository, spend.

Whatever admits a run, whoever can push a branch to the tracker and dispatch it can spend within the caps, the bot account included. The caps bound that, not the policy.

## Rotating the token

Make a new token as in step 1, then, while no run is active (a restart forgets registered runs):

    systemctl --user stop praxis-credential-broker-pod
    read -rs TOK; printf %s "$TOK" | podman secret create --replace praxis-credential-broker-anthropic-oauth -; unset TOK
    systemctl --user start praxis-credential-broker-pod

(In fish, the middle line is `read -s TOK; printf %s $TOK | podman secret create --replace praxis-credential-broker-anthropic-oauth -; set -e TOK`.)

The old token stays valid until it expires or is revoked from the Claude account; revoke it if it might have leaked. Setup tokens aren't refreshed: when one expires, runs get 401 from Anthropic in the broker's log, and this is the fix.

## Updating

Everything that runs is pinned here, so every update is a pull request, then on this machine, while no run is active:

    git pull
    systemctl --user daemon-reload && systemctl --user restart praxis-credential-broker-pod

**The broker.** Pick a commit of praxis-credential-broker's `main` whose "Container images" run succeeded, and read what changed since 45fbb14c7a66: INTERNALS.md, `praxis.yaml`, `scripts/praxis-gateway-entrypoint`, `run-token-policy.yaml` and `contrib/quadlet/`. Its images are tagged with the full commit id; get the digest without pulling with

    skopeo inspect --format '{{.Digest}}' docker://ghcr.io/cgwalters-bot/praxis-credential-broker-gateway:COMMIT

Move `Image=` and its comment, and make [praxis/praxis.yaml](praxis/praxis.yaml) upstream's file at COMMIT again, with the lines marked `jmarrero:` put back. Its header says which commit it's from; this shows what differs from that:

    curl -s https://raw.githubusercontent.com/cgwalters-bot/praxis-credential-broker/45fbb14c7a66328ad19e411820fbd58484767e39/praxis.yaml | diff - bot/praxis-broker/praxis/praxis.yaml

A newer image can need a newer `praxis.yaml` (a filter setting it no longer knows stops it at startup; a route or filter it now expects is simply missing), so the two always move in one pull request.

**Tailscale.** Move `Image=` to a newer release's digest, `skopeo inspect --format '{{.Digest}}' docker://ghcr.io/tailscale/tailscale:vX.Y.Z`. The node keeps its key and address across updates.

## Adding Codex or Mistral

Both are upstream units and secrets at the same pinned commit, plus a route that is already in `praxis.yaml`:

- **Mistral:** a key stored with upstream's `scripts/init-mistral-key`, and on `praxis-credential-broker-praxis.container` the `Secret=praxis-credential-broker-mistral-api-key,...` line from upstream's INTERNALS.md ("Quadlet").
- **Codex:** upstream's `praxis-credential-broker-proxy.container`, `praxis-credential-broker-provider-codex.container` and their two `.volume` units, with their images pinned like the gateway's (both are tagged with the same commit), the channel secret (`scripts/init-secrets`) and the device login (`scripts/native-pod.sh login`). The proxy listens on the pod's loopback port 8080 and trusts whatever reaches it, so the tailnet policy must never let anyone reach the broker's 8080; its tests check that.

Then admit the runs that use them in the policy (the entries aren't per provider) and size their caps in `praxis.yaml`.

## Security notes

- The Claude token is a podman secret that only the Praxis container mounts. Praxis puts it in its own environment, readable by the host user that runs the pod. It's never in this repository, an image or a devspace.
- Praxis logs the raw request of a malformed request, `Authorization` included. For a pass-through caller that is their own Claude credential, so treat the pod's logs like the credentials.
- `/usage` is unauthenticated for anything that reaches the port. The CORS origin only decides which web pages a browser lets read it.
- `praxis/` and `policy/` are mounted from this checkout, so whoever can change the checkout can change the routes and who spends. That is the same trust as the quadlets themselves.
