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
cp /bot/context/thread.md THREAD.md
# Public clone, no credentials. The PR head may come from a fork: it is untrusted
# code, but this container holds nothing that can write to GitHub. The thread
# doesn't need the clone (botd fetched it), so a repository the anonymous clone
# can't reach (a private one) only means answering without its code.
export GIT_TERMINAL_PROMPT=0
if git clone --quiet --filter=blob:none "https://github.com/${BOT_REPO}.git" repo >&2; then
    cd repo
    if [ "$BOT_IS_PR" = "1" ]; then
        git fetch --quiet origin "pull/${BOT_NUMBER}/head:bot-pr" >&2
        git checkout --quiet bot-pr >&2
    fi
else
    echo "bot-run: can't clone ${BOT_REPO} anonymously; continuing without its code" >&2
    { printf '%s\n\n' "> Note from botd: ${BOT_REPO} could not be cloned (it may be private), so its code isn't available here; answer from this thread."
      cat /bot/context/thread.md; } > THREAD.md
fi

exec "$@"
