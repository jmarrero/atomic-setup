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
render-skills "$homegit" "$rendered" "$goals" "$notes"
ln -sfn "$rendered/skills" "$HOME/.agents/skills"
rm -f "$HOME"/.claude/skills/*
for skill in "$rendered"/skills/*/; do
    ln -sfn "$skill" "$HOME/.claude/skills/$(basename "$skill")"
done
ln -sfn "$rendered/AGENTS.md" "$HOME/.claude/CLAUDE.md"
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

# Interactive, so the operator can attach and talk to it. Permission prompts
# are skipped (it runs unattended, in this container); COORDINATOR_PROMPT,
# if set, is the first message, e.g. "Load the coordinator skill and run".
cmd=(claude --dangerously-skip-permissions)
# Its model and effort (COORDINATOR_MODEL, COORDINATOR_EFFORT); unset means
# Claude Code's defaults. Subagents inherit the model unless they set one.
[ -n "${COORDINATOR_MODEL:-}" ] && cmd+=(--model "$COORDINATOR_MODEL")
[ -n "${COORDINATOR_EFFORT:-}" ] && cmd+=(--effort "$COORDINATOR_EFFORT")
[ -n "${COORDINATOR_PROMPT:-}" ] && cmd+=("$COORDINATOR_PROMPT")
cd "$clones"
tmux new-session -d -s coordinator -x 200 -y 50 "$(printf '%q ' "${cmd[@]}")"
log "started; attach with: podman exec -it bot-coordinator tmux attach -t coordinator"
# The poll runs here, outside the session, which wakes only when there is
# something to handle: bot-coordinator-loop keeps bot-poll running, types its
# news (kinds and run only) and a daily brief reminder into the session, and
# stops it on SIGTERM (forwarded by the container's init, RunInit=true). A
# poll run as the session's background task would wake it every 2 hours,
# when Claude Code's Bash tool kills it.
exec bot-coordinator-loop run
