#!/bin/bash
# Runs inside the disposable job container as the unprivileged "agent" user.
# botd mounts:
#   /bot/context (ro): this script, prompt.md, thread.md (issue/PR + comments)
#   /bot/out     (rw): reply.md may be written here; otherwise stdout is used
# Usage: bot-run.sh <agent argv...>
set -euo pipefail

: "${BOT_REPO:?}" "${BOT_NUMBER:?}" "${BOT_IS_PR:?}"

mkdir -p "$HOME/work"
cd "$HOME/work"
# Public clone, no credentials. The PR head may come from a fork: it is untrusted
# code, but this container holds nothing that can write to GitHub.
git clone --quiet --filter=blob:none "https://github.com/${BOT_REPO}.git" repo >&2
cd repo
if [ "$BOT_IS_PR" = "1" ]; then
    git fetch --quiet origin "pull/${BOT_NUMBER}/head:bot-pr" >&2
    git checkout --quiet bot-pr >&2
fi
cp /bot/context/thread.md "$HOME/work/THREAD.md"

exec "$@"
