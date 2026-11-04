#!/bin/bash
# Prepare the bot's home, then run one plain interactive Claude Code session
# in tmux for jmarrero to attach to, until it ends. Nothing here polls or
# wakes it: since cutover (jmarrero-forge/tracker#42, #44) the tracker's
# hosted pass runs the board, dispatch and pull request follow-up, and this
# session does what has no hosted path, when asked. The home directory is a
# persistent volume: clones, the sessions, caches and bot-devspace's keys
# survive restarts.
#   podman exec -it bot-coordinator tmux attach -t coordinator
set -euo pipefail

: "${BOT_LOGIN:?}" "${HOMEGIT_URL:?}" "${GH_TOKEN:?GH_TOKEN secret missing}"
: "${CLAUDE_CODE_OAUTH_TOKEN:?CLAUDE_CODE_OAUTH_TOKEN secret missing}"
clones=$HOME/src/github/$BOT_LOGIN
homegit=$clones/homegit
share=/usr/local/share/bot-coordinator
log() { echo "bot-coordinator: $*" >&2; }

mkdir -p "$clones" "$HOME/.local/bin" "$HOME/.agents" "$HOME/.claude/skills"

# homegit is frozen (#44): only the tools below are linked, and its
# upstream-policy records read. Links to its other tools, and the loop's
# bot-poll, are removed from earlier starts, so the loop can't be run here.
if [ ! -d "$homegit/.git" ]; then
    log "cloning $HOMEGIT_URL"
    git clone --quiet "$HOMEGIT_URL" "$homegit"
else
    git -C "$homegit" pull --quiet --ff-only || log "warning: homegit pull failed; using what's there"
fi
find "$HOME/.local/bin" -maxdepth 1 -lname '*/homegit/bin/*' -delete
rm -f "$HOME/.cargo/bin/bot-poll" "$HOME/.cargo/bot-poll.rev"
for tool in bot-devspace bot-git bot-operator bot-review-guide; do
    ln -sfr "$homegit/bin/$tool" "$HOME/.local/bin/$tool"
done

# The session's instructions and the few skills it keeps, rendered for this
# operator (see README.md).
goals=$HOME/.config/bot-harness/goals.md
test -s "$goals" || goals=$share/operator-goals.md
rendered=$HOME/.local/share/bot-skills
render-skills "$homegit" "$rendered" "$goals" --session "$share/session.md" \
    --skills devspace-work,commit-review,ast-explore,codegraph
ln -sfn "$rendered/skills" "$HOME/.agents/skills"
rm -f "$HOME"/.claude/skills/*
for skill in "$rendered"/skills/*/; do
    ln -sfn "$skill" "$HOME/.claude/skills/$(basename "$skill")"
done
ln -sfn "$rendered/CLAUDE.md" "$HOME/.claude/CLAUDE.md"
# homegit's builder subagent briefs itself from the coordinator skill.
rm -f "$HOME/.claude/agents/builder.md"
# homegit's settings without the heartbeat status line, plus accepting
# --dangerously-skip-permissions' warning screen.
python3 - "$homegit/dotfiles/.claude/settings.json" "$HOME/.claude/settings.json" <<'EOF'
import json, sys
settings = json.load(open(sys.argv[1]))
settings.pop("statusLine", None)
settings["skipDangerousModePermissionPrompt"] = True
json.dump(settings, open(sys.argv[2], "w"), indent=2)
EOF

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

# No first message: the session waits for jmarrero. The container ends
# with it, and systemd starts it again; a stop ends the session too.
cd "$clones"
tmux new-session -d -s coordinator -x 200 -y 50 -c "$clones" \
    claude --dangerously-skip-permissions --model "${COORDINATOR_MODEL:?}" --effort "${COORDINATOR_EFFORT:?}"
trap 'tmux kill-server 2>/dev/null; exit 0' TERM INT
log "started; attach with: podman exec -it bot-coordinator tmux attach -t coordinator"
while tmux has-session -t coordinator 2>/dev/null; do sleep 10 & wait $!; done
log "the session ended"
