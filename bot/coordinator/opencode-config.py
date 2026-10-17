#!/usr/bin/env python3
"""Write opencode.json, the opencode coordinator backend's config. The
image installs it as /etc/opencode/opencode.json, opencode's system-managed
config, which it loads last: its provider pin always wins. Its builder
rules can still be outranked by rules a session writes into its own config
directories, which the loop empties before each start (see README.md). It
applies to bot-opencode's runs too.

    opencode-config.py OUT

The config is generated because of the builder agent's bash rules, which
stand in for Claude Code's builder hook (homegit's
dotfiles/.claude/agents/builder.md): opencode has no hooks, only
permission patterns. opencode 1.18 matches each command of a command line
(as its shell parser splits it) against every pattern with its
Wildcard.match (* is anything, ? one character, a trailing " *" may also
match nothing), and the last matching rule wins. So there are only deny
rules after the "*" allow: an allow after them would let through any
denied command whose text happens to contain it.

The rules look for the command words where a command can start: at the
start, after a space (behind env, command, timeout, xargs, nice, VAR=1),
after a slash (a path), and anywhere after a shell (sh, bash, dash, zsh,
ksh, as a word or a path) given any option (its -c, however it is
spelled), or after eval. They don't look inside other quoted text, so `rg
'git push'` and `bot-devspace ssh NAME '... git diff'` stay allowed. It is a guard against
mistakes, not a sandbox: see README.md, "The builder under opencode", for
what it doesn't refuse. bot/test_coordinator_loop.py checks it against
opencode's matcher.
"""
import json
import os
import sys

# Where a command word can start: at the start, after a space or tab
# (behind env, timeout, VAR=1...) or after a slash (a path).
STARTS = ["", "* ", "*\t", "*/"]
# Between git or gh and their subcommands: up to three characters (? is
# any one), so up to three spaces or tabs, in any mix.
GAPS = ["?", "??", "???"]
# After a word: the end (a trailing " *" also matches nothing), a tab, or a
# redirect or & right after it (git push>/tmp/log).
ENDS = [" *", "\t*", ">*", "<*", "&*"]

SPACES = [" ", "\t"]

# The hook's list.
GIT_WRITES = ["commit", "commit-tree", "push", "pull", "am", "cherry-pick", "merge", "rebase", "revert",
              "update-ref"]
GH_GROUPS = ["issue", "gist", "release", "project", "label", "secret", "variable", "alias"]
GH_WRITES = {
    "pr": ["create", "merge", "close", "reopen", "edit", "comment", "review", "ready", "lock", "unlock",
           "update-branch", "revert"],
    "repo": ["create", "delete", "edit", "fork", "rename", "archive", "unarchive", "sync", "set-default",
             "deploy-key", "autolink"],
    "workflow": ["run", "enable", "disable"],
    "run": ["rerun", "cancel", "delete"],
    "cache": ["delete"],
}
GH_API_WRITES = ["-X", "-f", "-F", "--method", "--field", "--raw-field", "--input"]
BOT_WRITES = ["board", "feedback", "git", "land", "notify", "pr", "retro", "review-guide", "runs", "tmt-number",
              "watch", "work"]
# Words that, anywhere after a shell given an option or after eval, are refused.
WRAPPED = ["git ", "git\t", "gh ", "gh\t", "bot-", "dco-signoff"]
SHELLS = ["sh", "bash", "dash", "zsh", "ksh"]

BUILDER_PROMPT = (
    "You do mechanical build and CI work for the bot account. Read ~/.claude/agents/builder.md and follow "
    "its instructions (the text after its front matter). Here opencode's permissions, not a hook, refuse "
    "commits, pushes and GitHub or board writes: don't work around them. Fetching is fine, including a "
    "pull request's head (`git fetch origin pull/N/head:prN`). Leave your changes uncommitted and report the "
    "diff.")


def builder_rules():
    deny = []
    for start in STARTS:
        for verb in GIT_WRITES:
            for end in ENDS:
                deny += [f"{start}git{gap}{verb}{end}" for gap in GAPS]
                # Behind options (git -C DIR push, git<TAB>-c k=v commit).
                deny += [f"{start}git?-*{space}{verb}{end}" for space in SPACES]
        # The group or command right after gh, or after its options (gh -R
        # o/r issue create): not anywhere, which would refuse reads such as
        # gh run list --branch release.
        heads = [f"{start}gh{gap}" for gap in GAPS] + [f"{start}gh{gap}-* " for gap in GAPS]
        for head in heads:
            deny += [f"{head}{group}{end}" for group in GH_GROUPS for end in ENDS]
            for command, subs in GH_WRITES.items():
                deny += [f"{head}{command}*{space}{sub}{end}"
                         for sub in subs for space in SPACES for end in ENDS]
        deny += [f"{start}gh{gap}api*{space}{flag}*" for gap in GAPS for flag in GH_API_WRITES for space in SPACES]
        # A shell (by name or path) given any option (-c, -lc, --norc -c,
        # -o pipefail -c) and one of the words anywhere after it; eval,
        # likewise.
        deny += [f"{start}{shell}{gap}-*{word}*" for shell in SHELLS for gap in GAPS for word in WRAPPED]
        deny += [f"{start}eval?*{word}*" for word in WRAPPED]
    deny += [f"*bot-{tool}*" for tool in BOT_WRITES] + ["*dco-signoff*"]
    rules = {"*": "allow"}
    rules.update((pattern, "deny") for pattern in deny)
    return rules


def config():
    return {
        "$schema": "https://opencode.ai/config.json",
        # Only Copilot: without its login opencode would otherwise fall back
        # to opencode's own free model.
        "enabled_providers": ["github-copilot"],
        # No update prompt and no question tool, whose dialogs would take the
        # next typed message as their answer; no sharing.
        "autoupdate": False,
        "share": "disabled",
        "permission": {"question": "deny"},
        # Workers may start their own reviewer.
        "subagent_depth": 2,
        "agent": {
            "second-opinion": {
                "mode": "subagent",
                # Set in the quadlet; opencode fills in {env:...}.
                "model": "{env:BOT_OPENCODE_REVIEW_MODEL}",
                "description": "A reviewer on a different model family from the coordinator, for the second "
                               "review of every result.",
            },
            "builder": {
                "mode": "subagent",
                "description": "Mechanical build work: loop until a branch compiles, fix lint warnings, get a "
                               "named failing test passing when the fix is local, classify CI failures, bisect "
                               "a build failure. Leaves its changes uncommitted for the caller. Never for "
                               "reviews, design or security questions.",
                "prompt": BUILDER_PROMPT,
                "permission": {"task": "deny", "bash": builder_rules()},
            },
        },
    }


def render():
    return json.dumps(config(), indent=1) + "\n"


def main():
    if len(sys.argv) != 2:
        sys.exit(f"usage: {os.path.basename(sys.argv[0])} OUT")
    with open(sys.argv[1], "w") as f:
        f.write(render())


if __name__ == "__main__":
    main()
