#!/bin/bash
# Prepare the bot's home from homegit, then hand over to bot-coordinator-loop,
# which runs the coordinator session in tmux on the first available backend
# (backends.toml) until it ends. The home directory is a persistent volume:
# clones, the sessions, caches and bot-devspace's keys survive restarts.
#   podman exec -it bot-coordinator tmux attach -t coordinator
set -euo pipefail

: "${BOT_LOGIN:?}" "${HOMEGIT_URL:?}" "${GH_TOKEN:?GH_TOKEN secret missing}"
# The backends (each one's secret is checked there): fail early on a bad
# config, or when none can start.
bot-coordinator-loop check
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

# The tools, as docs/bootstrap.md says: bin/ as symlinks, never homegit's
# dotfiles (cgwalters' .bashrc, .gitconfig).
(cd "$homegit" && ./install-bin.sh)
# The skills, AGENTS.md and the builder subagent, rendered for this operator
# (render-skills: homegit's text spells out cgwalters' names and goals, which
# the model sometimes copies). Rendered again on every start, after the pull.
goals=$HOME/.config/bot-harness/goals.md
test -s "$goals" || goals=/usr/local/share/bot-coordinator/operator-goals.md
notes=$HOME/.config/bot-harness/notes.md
test -s "$notes" || notes=/usr/local/share/bot-coordinator/operator-notes.md
rendered=$HOME/.local/share/bot-skills
# Plus a variant of the coordinator skill per backend, with its notes.
backend_args=$(bot-coordinator-loop render-args)
mapfile -t backend_args <<<"$backend_args"
render-skills "$homegit" "$rendered" "$goals" "$notes" "${backend_args[@]}"
ln -sfn "$rendered/skills" "$HOME/.agents/skills"
rm -f "$HOME"/.claude/skills/*
for skill in "$rendered"/skills/*/; do
    ln -sfn "$skill" "$HOME/.claude/skills/$(basename "$skill")"
done
ln -sfn "$rendered/AGENTS.md" "$HOME/.claude/CLAUDE.md"
mkdir -p "$HOME/.config/opencode"
ln -sfn "$rendered/AGENTS.md" "$HOME/.config/opencode/AGENTS.md"
ln -sfn "$rendered/agents/builder.md" "$HOME/.claude/agents/builder.md"
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

# The session is the loop's to start, on the first available backend, in
# tmux session `coordinator` in the clones directory: interactive, so the
# operator can attach and talk to it, with permission prompts skipped (it
# runs unattended, in this container), and COORDINATOR_PROMPT as its first
# message. The loop also keeps bot-poll running outside the session, which
# wakes only when there is something to handle: it types bot-poll's news
# (kinds and run only), a daily brief reminder and handoffs into the
# session, and stops it on SIGTERM (forwarded by the container's init,
# RunInit=true). A poll run as the session's background task would wake it
# every 2 hours, when Claude Code's Bash tool kills it.
cd "$clones"
log "starting; attach with: podman exec -it bot-coordinator tmux attach -t coordinator"
exec bot-coordinator-loop run
