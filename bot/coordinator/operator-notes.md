## Operator's notes

These come from jmarrero (`~/.config/bot-harness/notes.md`) and add to the
rest of this skill.

### After a restart

A session that ends takes its workers with it: subagents and `bot-opencode`
runs die mid-task, while their items stay In Progress on the board. So at
the start of every session, right after the first `bot-poll --once`, run
`bot-board list --status "In Progress"` (and check the board for items
without a priority too). For each item, look at its PR, branch and Why to
see how far it got, then resume it with a new worker (or `bot-opencode`)
briefed with that state, or set it to Needs human with the reason. Say
which in your start-up summary. Never leave an item In Progress without a
worker on it.

### Second opinions and well-defined work with opencode

`bot-opencode` runs one task with opencode on a GitHub Copilot model (a
different model family from yours; usage counts against jmarrero's Copilot
premium requests). Always start it with the Bash tool's
`run_in_background`, so its exit wakes you, and give it `--name` (e.g. the
item or PR) and `--dir` (a worktree of its own). Run at most two at once.

- **Reviews.** Every forge PR also gets a review from opencode, besides the
  reviewer subagent: brief it like a reviewer, starting with "Read
  ~/.agents/skills/coordinator/reviewer-preamble.md and follow it", plus the
  PR URL and head SHA, and have it post its verdict as a review comment on
  the PR (it has the bot's `gh` login). Weigh both verdicts; when they
  disagree, say so on the PR and in the item's Why, and treat the stricter
  one as standing until resolved.
- **Well-defined work.** An item is well-defined when its issue says what to
  change and how to check it, with no design choices left: dependency
  bumps, mechanical refactors and renames, a test for a described case, a
  clear compiler, clippy or CI fix, docs that follow a stated change. Send
  those to opencode instead of a Claude worker: brief it starting with
  "Read ~/.agents/skills/coordinator/worker-preamble.md and follow it",
  plus the item URL, the repository and the worktree. Everything else
  (design, ambiguous asks, debugging an unknown cause, anything touching
  sign-off, credentials or the sandbox) stays with Claude workers.
- Its result gets the same review as any worker's: a reviewer subagent, and
  you before it reaches jmarrero. If it fails or wanders off, hand the item
  to a Claude worker and note it in the Why field.
- The default model is `BOT_OPENCODE_MODEL`; `--model` overrides it
  (`opencode models github-copilot` lists them).
