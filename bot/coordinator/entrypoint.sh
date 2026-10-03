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
# As PID 1, stop cleanly on SIGTERM (systemctl stop) instead of being killed.
trap 'log "stopping"; tmux kill-server 2>/dev/null; exit 0' TERM INT
# Watchdog: the session only hears of news while bot-poll runs in its
# background, and it sometimes stops it (to do a sweep by hand, say) and
# doesn't start it again. If no bot-poll has run for BOT_POLL_WATCHDOG_SECS,
# type a reminder into the session (a message typed while it works waits
# its turn), at most once per 2x that.
watchdog=${BOT_POLL_WATCHDOG_SECS:-900}
missing=0 last_nudge=0
while tmux has-session -t coordinator 2>/dev/null; do
    sleep 30 & wait $!
    if pgrep -f 'bot-poll --exclude-lead' >/dev/null; then
        missing=0
        continue
    fi
    missing=$((missing + 30))
    now=$(date +%s)
    if [ "$missing" -ge "$watchdog" ] && [ $((now - last_nudge)) -ge $((2 * watchdog)) ]; then
        log "bot-poll hasn't run for ${missing}s; reminding the session"
        tmux send-keys -t coordinator -l "Watchdog: bot-poll has not been running for $((missing / 60)) minutes, so news isn't reaching you. Run bot-poll --once and handle what it reports, then start bot-poll --exclude-lead '*' again with the Bash tool's run_in_background, and keep it running."
        sleep 1
        tmux send-keys -t coordinator Enter
        last_nudge=$now
    fi
done
log "the coordinator session ended"
