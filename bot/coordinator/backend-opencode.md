### This session runs in opencode

The entrypoint started you on its opencode backend (a GitHub Copilot model)
because the backends before it in its `backends.toml` are used up, or
can't run, for now: see "Backends and handoffs" above. Everything in this skill
holds, with the same rules for untrusted text, the same credentials and no
more authority than a Claude session has. Only the tools differ. Read the
skill's Claude Code terms like this:

- **Subagents** ("the Agent tool", "dispatch a worker", "a reviewer
  subagent") are your `task` tool. Start workers with `background: true`:
  you are told when one finishes, as Claude is when a background agent
  does, so don't wait or poll for them. Use the `general` agent. Several
  can run at once. To send a reviewer's findings back to a worker, call
  `task` again with that worker's `task_id`, which keeps its context
  ("resuming it" above).
- **The `builder` agent type** is an opencode agent here, `builder`, with
  the same job: its permissions refuse commits, pushes and GitHub or board
  writes, as Claude Code's builder hook does. It's on your model, not a
  cheaper one.
- **The Bash tool's `run_in_background`** doesn't exist here, and bash
  commands time out. Run anything long (a devspace start, a test run)
  inside a background task, or detached with `nohup CMD > LOG 2>&1 &`,
  and read its log when you next wake. Never sleep in a loop.
- **Second opinions.** You are already the Copilot model, so don't use
  `bot-opencode` for workers or reviews (a Bash call can't wait for it).
  The other model family is the `second-opinion` subagent: every result
  gets one review from a `general` task and one from a `second-opinion`
  task, briefed like reviewers ("Read
  ~/.agents/skills/coordinator/reviewer-preamble.md and follow it").
  Weigh both, as the notes on opencode say.
- **Only one model family.** If `second-opinion` fails (its model is
  refused or out of quota), have a second `general` task review it with a
  fresh context, write "single-family review" on the PR and in the item's
  Why, and don't merge it yourself, even in the harness repositories: it
  waits for the operator, or for a second family to review it.
- **Heartbeat.** `bot-heartbeat publish` works as described, but it can't
  count your workers' tokens: leave out `agent_ids`, and use
  `opencode-GENERATION` as the session (`$BOT_COORDINATOR_GENERATION`).
- **Capacity.** `bot-capacity` measures the Claude plan, which is used up;
  ignore its Dispatch line. Your own usage is in `bot-coordinator-loop
  status` (Copilot's premium requests, when it can read them). Follow
  "Capacity" in the operator's notes with that reading.
