# workboard-hub

The hub of jmarrero's work tracker, [jmarrero-bot/work-tracker-poc](https://github.com/jmarrero-bot/work-tracker-poc): one board of his external work (GitHub, Fedora Forge, and the bot's board, fork PRs and heartbeat), reached from anywhere on the tailnet. The GNOME companion on the work laptop pulls it and adds the internal work, which never comes here. How the hub works is in that repository's [docs/hub.md](https://github.com/jmarrero-bot/work-tracker-poc/blob/main/docs/hub.md).

This directory is only the wiring: the quadlet, its secrets and `tailscale serve`. The hub's code and image live in work-tracker-poc, whose CI publishes `ghcr.io/jmarrero-bot/work-tracker-poc` on release tags. The quadlet pins that image by digest, and a new release is a pull request here moving the pin ([docs/release.md](https://github.com/jmarrero-bot/work-tracker-poc/blob/main/docs/release.md)), so the bot never deploys to this machine without you.

## What's in the container

- The image: `workboard serve --hub`, as an unprivileged user, on a distroless base with no shell.
- Its data, in the named volume `workboard-hub` at `/var/lib/workboard`: the database, each connection's copy of its token, and the companion token.
- Network: the hub listens on `127.0.0.1:8421` inside the container. `PublishPort=127.0.0.1:8421:8421` puts it on this machine's loopback, with pasta told to forward to the container's loopback (`--host-lo-to-ns-lo`; by default it forwards to the container's own address, where nothing listens), and `tailscale serve` puts that on the tailnet over HTTPS. Nothing listens on another interface.
- Credentials: podman secrets, as environment variables that `workboard connect --token-env` reads: a read-only GitHub token of the hub's own (step 1).
- No capabilities, no-new-privileges, a read-only root, 1G of memory. A health check (an authenticated `workboard connections`) every minute; the container is restarted when it fails.

## Set up

1. **The secrets.** The hub gets a GitHub token of its own that can only read, never the bot's `jmarrero-bot-gh-token`: each connection keeps a copy of its token in the hub's volume, so a compromised hub must not hold a token that can write. Its connections need:

   - `read:project`, for the bot's Workstream board (GraphQL `projectV2`, needed even though the board is public);
   - nothing else: the bot's fork PRs (`search/issues`), its heartbeat comment in jmarrero-forge/tracker, the board items' issues and PRs, the GitHub connection's searches, and `GET /user` are all in public repositories. `read:org` isn't needed: nothing reads org membership or teams. Board items in private repositories (such as jmarrero-forge/bot-ops) stay unreadable, and the hub counts them as "items this token can't read"; `repo` would read them, but can also write everywhere.

   Signed in to GitHub as jmarrero-bot, open <https://github.com/settings/tokens/new?scopes=read:project&description=workboard-hub> (Settings → Developer settings → Personal access tokens → Tokens (classic) → Generate new token (classic)). Keep the note `workboard-hub`, pick an expiration (and note when to renew it), check only `read:project`, and generate it. On this machine, check its scopes and make it a podman secret:

       read -s TOK
       printf 'Authorization: Bearer %s\n' "$TOK" | curl -sSI -H @- https://api.github.com/user | grep -i '^x-oauth-scopes'   # x-oauth-scopes: read:project
       printf %s "$TOK" | podman secret create --replace workboard-hub-gh-token -; unset TOK

   For Fedora Forge, make a secret from a Fedora token the same way, as `workboard-hub-fedora-token`, and add `Secret=workboard-hub-fedora-token,type=env,target=FEDORA_TOKEN` to the drop-in of step 2.

   Each connection keeps its own copy of its token in the volume. After replacing a secret, restart the hub and give the connection the new one (step 7's `verify`).

2. **This machine's tailnet name**, for `--allowed-host`: the hub refuses a request under any name it wasn't given. Find it with `tailscale status --json | jq -r '.Self.DNSName | rtrimstr(".")'` (such as `bot.tail1234.ts.net`) and put it in a drop-in, whose `Exec=` replaces the quadlet's (this machine's settings go there, so the checkout stays clean):

       mkdir -p ~/.config/containers/systemd/workboard-hub.container.d
       printf '[Container]\nExec=serve --hub --allowed-host %s\n' bot.tail1234.ts.net \
         > ~/.config/containers/systemd/workboard-hub.container.d/host.conf

3. **Start it:**

       ln -s $PWD/bot/workboard-hub/workboard-hub.container ~/.config/containers/systemd/
       systemctl --user daemon-reload && systemctl --user start workboard-hub

4. **Let jmarrero's devices in.** The bot's tailnet policy, [../tailscale-policy.hujson](../tailscale-policy.hujson), lets `group:operator` (jmarrero, as `jmarrero@github`) reach this machine on TCP 443 and nothing else, and keeps devspaces out. In the bot tailnet's admin console, logged in as the bot:

   - paste the updated policy under Access controls;
   - Users → Invite users: invite jmarrero as a **Member**, and he accepts, logging in with GitHub. If he logs in as another identity, change `jmarrero@github` in the policy's group and tests;
   - DNS: turn on MagicDNS and **HTTPS Certificates**. `tailscale serve --https` needs them for this machine's certificate. That certificate puts this machine's tailnet name (such as `bot.tail1234.ts.net`) in the public Certificate Transparency logs.

   On each of his devices, add the bot's tailnet as another account (`tailscale login`, or the client's "Add account"); `tailscale switch` moves a device between his tailnets, since it is in one at a time. Whether Tailscale is allowed on the work laptop is still open ([jmarrero-forge/tracker#8](https://github.com/jmarrero-forge/tracker/issues/8)).

5. **Put it on the tailnet**, in HTTPS mode only:

       sudo tailscale serve --bg --https=443 http://127.0.0.1:8421

   Never `--http`, `--tcp` or `--tls-terminated-tcp`, and never `tailscale funnel`, which would put the hub on the internet; the hub refuses every request Funnel brings in. Serve nothing else under this machine's name, on any port or path: the hub's session cookie would be sent to it. `tailscale serve status` shows what's served.

6. **Check that the name gets through** `tailscale serve`, from one of jmarrero's devices on the tailnet:

       curl -sS https://bot.tail1234.ts.net/api/snapshot

   `` {"error":"Open the pairing URL printed by `workboard serve`"} `` means the hub took the name and wants a login. `{"error":"Unexpected host"}` means the name isn't the drop-in's `--allowed-host`. A timeout means the policy or the invitation of step 4 isn't in place; a certificate error, that HTTPS Certificates are off.

7. **Connect its sources**, always as external, and pair a browser. The CLI runs in the container:

       alias wb='podman exec workboard-hub workboard'
       wb connect bot --url https://github.com/ --external --token-env GH_TOKEN --board orgs/jmarrero-forge/1 \
         --tracker jmarrero-forge/tracker --heartbeat 1 --bot-login jmarrero-bot --forge-org jmarrero-forge
       wb connect github --url https://github.com/ --external --token-env GH_TOKEN --watch-user jmarrero
       wb connections                          # IDs and sync state; `wb verify ID --token-env GH_TOKEN` after a rotation
       podman logs workboard-hub | tail -n 3   # the pairing URL, new on every start; keep it private
       wb companion-token                      # for `workboard hub set` on the laptop

   `wb connections` should show the bot connection synced. If it says the sync "answered with errors about the bot's board", GitHub refused part of the board's GraphQL query, and the token needs more than `read:project` after all.

The image's package is public, so pulling it needs no login. If that ever changes, log in with a token that only has `read:packages`, into the auth file that survives a reboot (a plain `podman login` writes to `$XDG_RUNTIME_DIR`, which doesn't, and the next pull would fail):

    podman login --authfile ~/.config/containers/auth.json ghcr.io

## Updating

Merge the pull request that moves `Image=`, then in this checkout:

    git pull
    systemctl --user daemon-reload && systemctl --user restart workboard-hub

Upgrade the companions on the laptop first when the release notes say the hub's export changed.

## Once GitHub App login lands

GitHub App login ([work-tracker-poc#34](https://github.com/jmarrero-bot/work-tracker-poc/pull/34)) replaces browser pairing. Once a release has it, after creating the App as its `docs/github-app.md` says:

1. Make the App's client secret a podman secret, add `Secret=workboard-hub-github-app-secret,type=env,target=WORKBOARD_GITHUB_APP_SECRET` to the drop-in, restart, and store it in the hub's data: `wb github-app set-secret --token-env WORKBOARD_GITHUB_APP_SECRET`.
2. Add the App's flags to the drop-in's `Exec=` line:

       Exec=serve --hub --allowed-host bot.tail1234.ts.net \
         --github-app-client-id Iv23li... \
         --github-app-callback https://bot.tail1234.ts.net/auth/github/callback \
         --github-user JMARRERO_NUMERIC_ID

   Until the App is set up, a hub with #34 prints its pairing URL only with `--pairing` on that line.
