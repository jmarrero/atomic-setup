#!/bin/bash
# Prepare the bot's home from homegit, then run Claude Code in tmux until that
# session ends. The home directory is a persistent volume: clones, Claude's
# sessions, caches and bot-devspace's keys survive restarts.
#   podman exec -it bot-coordinator tmux attach -t coordinator
set -euo pipefail

: "${BOT_LOGIN:?}" "${HOMEGIT_URL:?}" "${GH_TOKEN:?GH_TOKEN secret missing}"
: "${CLAUDE_CODE_OAUTH_TOKEN:?CLAUDE_CODE_OAUTH_TOKEN secret missing}"
clones=$HOME/src/github/$BOT_LOGIN
homegit=$clones/homegit
log() { echo "bot-coordinator: $*" >&2; }

mkdir -p "$clones" "$HOME/.local/bin" "$HOME/.agents" "$HOME/.claude/skills" "$HOME/.claude/agents"

# homegit lives in the volume, where bot-poll's sweeps `git pull` it.
if [ ! -d "$homegit/.git" ]; then
    log "cloning $HOMEGIT_URL"
    git clone --quiet "$HOMEGIT_URL" "$homegit"
else
    git -C "$homegit" pull --quiet --ff-only || log "warning: homegit pull failed; using what's there"
fi

# The tools and skills, as docs/bootstrap.md says: bin/ as symlinks, the
# skills by hand, never homegit's dotfiles (cgwalters' .bashrc, .gitconfig).
(cd "$homegit" && ./install-bin.sh)
ln -sfn "$homegit/dotfiles/.agents/skills" "$HOME/.agents/skills"
for skill in "$homegit"/dotfiles/.agents/skills/*/; do
    ln -sfn "$skill" "$HOME/.claude/skills/$(basename "$skill")"
done
ln -sfn "$homegit/dotfiles/.config/AGENTS.md" "$HOME/.claude/CLAUDE.md"
ln -sfn "$homegit/dotfiles/.claude/agents/builder.md" "$HOME/.claude/agents/builder.md"
# homegit's settings, plus accepting --dangerously-skip-permissions' warning
# screen, which would otherwise wait for someone to attach and confirm it.
python3 - "$homegit/dotfiles/.claude/settings.json" "$HOME/.claude/settings.json" <<'EOF'
import json, sys
settings = json.load(open(sys.argv[1]))
settings["skipDangerousModePermissionPrompt"] = True
json.dump(settings, open(sys.argv[2], "w"), indent=2)
EOF

# bot-poll (Rust); rebuilt only when its sources change.
rev=$(git -C "$homegit" log -1 --format=%H -- crates/)
if [ "$(cat "$HOME/.cargo/bot-poll.rev" 2>/dev/null)" != "$rev" ]; then
    log "building bot-poll at ${rev:0:12}"
    (cd "$homegit" && for c in crates/*/; do cargo install --quiet --locked --path "$c"; done)
    echo "$rev" > "$HOME/.cargo/bot-poll.rev"
fi

bot-operator --json > /dev/null   # fail early on a bad operator config
gh auth status >/dev/null 2>&1 || { log "gh is not logged in"; exit 1; }

# Skip Claude Code's first-run screens; trust the clones directory.
if [ ! -s "$HOME/.claude.json" ]; then
    python3 - "$clones" > "$HOME/.claude.json" <<'EOF'
import json, sys
print(json.dumps({"hasCompletedOnboarding": True,
                  "projects": {sys.argv[1]: {"hasTrustDialogAccepted": True}}}))
EOF
fi

# Interactive, so the operator can attach and talk to it. Permission prompts
# are skipped (it runs unattended, in this container); COORDINATOR_PROMPT,
# if set, is the first message, e.g. "Load the coordinator skill and run".
cmd=(claude --dangerously-skip-permissions)
[ -n "${COORDINATOR_PROMPT:-}" ] && cmd+=("$COORDINATOR_PROMPT")
cd "$clones"
tmux new-session -d -s coordinator -x 200 -y 50 "$(printf '%q ' "${cmd[@]}")"
log "started; attach with: podman exec -it bot-coordinator tmux attach -t coordinator"
# As PID 1, stop cleanly on SIGTERM (systemctl stop) instead of being killed.
trap 'log "stopping"; tmux kill-server 2>/dev/null; exit 0' TERM INT
while tmux has-session -t coordinator 2>/dev/null; do sleep 30 & wait $!; done
log "the coordinator session ended"
