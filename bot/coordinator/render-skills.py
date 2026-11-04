#!/usr/bin/env python3
"""Render homegit's skills and AGENTS.md for this operator.

homegit's skills are written for cgwalters' setup: they spell out his logins,
forge org, tracker, board, identities, paths and goals, and say to "read them
as the operator config's values" under another config. The model sometimes
copies them literally instead (it signed a comment with his Generated-by
link), so the coordinator gets copies with this operator's values filled in.
homegit stays as cgwalters writes it, which keeps the fork easy to update.

  render-skills.py HOMEGIT DEST GOALS [--skills NAME,...] [--session NOTES]

writes DEST/skills/<name>/... (from dotfiles/.agents/skills), DEST/AGENTS.md
and DEST/agents/builder.md, with values from `bot-operator --json`. GOALS is
a Markdown file replacing cgwalters' goals and priority rules. --skills
keeps only the named skills. --session also writes DEST/CLAUDE.md, the
interactive session's instructions: AGENTS.md up to its "Commit
attribution" section, whose rules and the rest name homegit's loop tools,
then NOTES as written. What still names cgwalters afterwards (links to his
own repositories, issues and the coordination channel, kept on purpose) is
listed on stderr.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys

# What really is cgwalters' and must stay so: his repositories, issues, gists,
# board, and the coordination channel between the two harnesses. His review
# app's repository stays too, except where the skills name it as the app the
# operator uses or as an example of the forge's repositories (substitutions()
# points those, and its URL, at ours).
KEEP = [
    r"cgwalters-forge/harness-coordination",
    r"cgwalters-forge/tracker#(?!176\b)\d+",
    r"cgwalters-forge/cgwalters-devspace-sandbox#\d+",
    r"(?<!review app \()(?<!such as\n)(?<!such as )cgwalters-forge/review\b",
    r"cgwalters-forge/(?:workflow-compiler|agentic-job)\b",
    r"cgwalters-bot/(?:praxis-credential-broker|debug-bootc-to-disk-virtiofsd|ostree-missing-refs)\b",
    r"gist\.github\.com/cgwalters-bot/[0-9a-f]+",
    r"users/cgwalters-bot/projects/2",
    r"github\.com/cgwalters/#llms",
    r"cgwalters' harness",
    # The coordination bullet names both harnesses' peers on purpose.
    r"- \*\*Coordination questions\*\*.*?(?=\n- \*\*)",
]


# homegit names jmarrero's harness as cgwalters' peer; seen from jmarrero's,
# the peer is cgwalters'. (The coordination bullet, kept above, names both.)
PEERS = [
    (r"`jmarrero-bot` (and|or) `jmarrero`", r"`cgwalters-bot` \1 `cgwalters`"),
    (r"jmarrero-bot and jmarrero\b", "cgwalters-bot and cgwalters"),
    (r"jmarrero's harness", "cgwalters' harness"),
]
PEER_KEEP = r"`cgwalters-bot` (?:and|or) `cgwalters`|cgwalters-bot and cgwalters\b|cgwalters' harness"


def sections(goals, cfg):
    """(file, regex, replacement) for cgwalters' goals and his lists."""
    bot, forge = cfg["bot"]["login"], cfg["forge_org"]
    own_repos = f"homegit ({cfg['bot']['homegit_repo']}), {bot}/{bot}, {forge}/tracker, {forge}/.github"
    return [
        ("skills/coordinator/SKILL.md",
         r"## What we're working toward\n.*?(?=\nThe interactive coordinator session is itself temporary)",
         "## What we're working toward\n\n" + goals.strip() + "\n"),
        ("skills/workstream/SKILL.md",
         r"The \[Composefs Stable\]\(https://github\.com/users/cgwalters-bot/projects/2\) board .*?\n\n",
         ""),
        ("skills/workstream/SKILL.md",
         r"The \*\*Priority\*\* field ranks work by cgwalters' standing rule:.*?(?=\n- \*\*P2\*\*)",
         "The **Priority** field ranks work by the operator's goals (see \"What we're working\n"
         "toward\" in the coordinator skill):\n\n"
         "- **P0** is what the operator marks P0, and nothing else.\n"
         "- **P1** is the operator's direct requests, their own stuck PRs, the bot's own\n"
         "  infrastructure, and burning down the backlog in the repositories the forge\n"
         "  forks (bootc, ostree, rpm-ostree)."),
        ("skills/coordinator/SKILL.md",
         r"Composefs stability comes\n  first: fill free worker slots with P0 \(composefs-stable\) items before\n  any P1 own-infra or backlog item",
         "P0 comes\n  first: fill free worker slots with P0 items before\n  any P1 item"),
        ("skills/backlog-planning/SKILL.md",
         r"- \*\*P0\*\*: work that moves composefs toward stable,.*?(?=\n- \*\*P2\*\*)",
         "- **P0**: never assign it: P0 is only what the operator marks P0 (see\n"
         "  \"What we're working toward\" in the coordinator skill).\n"
         "- **P1**: explicit requests to the bot from the operator and their own\n"
         "  open PRs blocked on failing CI, merge conflicts, or unanswered review;\n"
         "  the bot's own open PRs in that state; concrete asks from the operator\n"
         "  (\"we should\", \"needs a test\", a bug they confirmed); and the bot's\n"
         "  own infrastructure.\n"
         "- **P2** also covers review requests where a pre-review, reproduction or\n"
         "  bisect would help, and backlog in the repositories the forge forks."),
        ("skills/coordinator/worker-preamble.md",
         r"\(homegit, cgwalters-bot/cgwalters-bot, [^)]*\)",
         "(" + own_repos + ")"),
    ]


def review_repo(url):
    """The repository a GitHub Pages URL is published from, else the URL."""
    m = re.fullmatch(r"https://([A-Za-z0-9-]+)\.github\.io/([A-Za-z0-9._-]+)/?", url)
    return f"{m[1]}/{m[2]}" if m else url


def substitutions(cfg):
    op, bot = cfg["operator"], cfg["bot"]
    skills = "~/.agents/skills/"
    app = cfg["review_app_url"]
    return [
        # The review app the operator uses: ours, by URL and by repository.
        (r"https://cgwalters-forge\.github\.io/review/?", app),
        (r"review app \(cgwalters-forge/review\)", f"review app ({review_repo(app)})"),
        # The workstream skill's example of the forge's own repositories:
        # the review app isn't one here, the tracker is.
        (r"(?:(?<=such as )|(?<=such as\n))cgwalters-forge/review\b", cfg["tracker_repo"]),
        # cgwalters' heartbeat issue: ours.
        (r"cgwalters-forge/tracker#176\b", f"{cfg['tracker_repo']}#{cfg['heartbeat_issue']}"),
        # Paths to the skills in the homegit checkout: the rendered copies.
        (r"~/src/github/cgwalters-bot/homegit/dotfiles/\.agents/skills/", skills),
        (r"(?<![\w/.~])dotfiles/\.agents/skills/", skills),
        # The bot's clones (homegit's checkout among them), before the rule
        # for the homegit repository below would turn the path into the
        # repository's name.
        (r"~/src/github/cgwalters-bot\b", f"~/src/github/{bot['login']}"),
        # Identities, most specific first.
        (r"Colin Walters <walters\+llm@verbum\.org>", f"{bot['git_name']} <{bot['git_email']}>"),
        (r"Colin Walters <walters@verbum\.org>", f"{op['name']} <{op['email']}>"),
        (r"walters\+llm@verbum\.org", bot["git_email"]),
        (r"walters@verbum\.org", op["email"]),
        (r"Colin Walters", op["name"]),
        # Places.
        (r"bootc-dev/cgwalters-devspace-sandbox", cfg["devspace"]["repo"]),
        (r"cgwalters-devspace-", cfg["devspace"]["host_prefix"]),
        (r"cgwalters-bot/homegit", bot["homegit_repo"]),
        (r"orgs/cgwalters-forge/projects/1", f"{cfg['board']['owner_type']}/{cfg['board']['owner']}/projects/{cfg['board']['number']}"),
        (r"cgwalters-forge", cfg["forge_org"]),
        (r"cgwalters-bot", bot["login"]),
        # The operator, by login and possessive.
        (r"\bcgwalters'(?!s)", f"{op['login']}'s"),
        (r"\bcgwalters\b", op["login"]),
    ]


def render(text, subs, keep_re):
    kept = []

    def stash(m):
        kept.append(m.group(0))
        return f"\x00{len(kept) - 1}\x00"

    text = keep_re.sub(stash, text)
    for pattern, value in PEERS:
        text = re.sub(pattern, value, text)
    text = re.sub(PEER_KEEP, stash, text)
    for pattern, value in subs:
        text = re.sub(pattern, lambda _m, v=value: v, text)
    return re.sub(r"\x00(\d+)\x00", lambda m: kept[int(m.group(1))], text)


# Where the session's CLAUDE.md stops taking homegit's AGENTS.md: from here
# on it names the loop's tools (bot-pr, bot-land, the workstream skill).
SESSION_CUT = "\n## Commit attribution\n"


def skill_list(value):
    names = value.split(",")
    if not all(re.fullmatch(r"[a-z][a-z0-9-]{0,63}", n) for n in names):
        raise argparse.ArgumentTypeError(f"expected NAME,..., not {value!r}")
    return names


def main():
    parser = argparse.ArgumentParser(prog="render-skills")
    parser.add_argument("homegit")
    parser.add_argument("dest")
    parser.add_argument("goals")
    parser.add_argument("--skills", type=skill_list)
    parser.add_argument("--session", metavar="NOTES")
    args = parser.parse_args()
    homegit, dest, goals_file = args.homegit, args.dest, args.goals
    # Read first, so that a missing file fails before anything is written.
    session = open(args.session).read() if args.session else None
    cfg = json.loads(subprocess.run(["bot-operator", "--json"], check=True,
                                    capture_output=True, text=True).stdout)
    goals = open(goals_file).read()
    subs = substitutions(cfg)
    keep_re = re.compile("|".join(f"(?:{k})" for k in KEEP), re.S)

    tmp = dest + ".new"
    shutil.rmtree(tmp, ignore_errors=True)
    shutil.copytree(os.path.join(homegit, "dotfiles/.agents/skills"), os.path.join(tmp, "skills"))
    os.makedirs(os.path.join(tmp, "agents"))
    shutil.copy(os.path.join(homegit, "dotfiles/.config/AGENTS.md"), os.path.join(tmp, "AGENTS.md"))
    shutil.copy(os.path.join(homegit, "dotfiles/.claude/agents/builder.md"),
                os.path.join(tmp, "agents/builder.md"))

    for rel, pattern, replacement in sections(goals, cfg):
        path = os.path.join(tmp, rel)
        text = open(path).read()
        new, n = re.subn(pattern, lambda _m: replacement, text, flags=re.S)
        if n != 1:
            # homegit changed: say so rather than render cgwalters' goals as ours.
            sys.exit(f"render-skills: expected one match of {pattern[:60]!r} in {rel}, found {n}")
        open(path, "w").write(new)

    leftovers = []
    for root, _, files in os.walk(tmp):
        for name in files:
            if not name.endswith(".md"):
                continue
            path = os.path.join(root, name)
            text = render(open(path).read(), subs, keep_re)
            open(path, "w").write(text)
            for i, line in enumerate(text.splitlines(), 1):
                if re.search(r"(?i)cgwalters|colin|verbum", line):
                    leftovers.append(f"{os.path.relpath(path, tmp)}:{i}: {line.strip()[:120]}")

    if args.skills is not None:
        skills_dir = os.path.join(tmp, "skills")
        missing = sorted(set(args.skills) - set(os.listdir(skills_dir)))
        if missing:
            sys.exit(f"render-skills: homegit has no skill {', '.join(missing)}")
        for name in os.listdir(skills_dir):
            if name not in args.skills:
                shutil.rmtree(os.path.join(skills_dir, name))
        leftovers = [line for line in leftovers
                     if not line.startswith("skills/") or line.split("/")[1] in args.skills]
    if session is not None:
        agents = open(os.path.join(tmp, "AGENTS.md")).read()
        if agents.count(SESSION_CUT) != 1:
            sys.exit(f"render-skills: expected one {SESSION_CUT.strip()!r} section in AGENTS.md")
        # The notes after rendering, so the operator's text is taken as written.
        with open(os.path.join(tmp, "CLAUDE.md"), "w") as f:
            f.write(agents[:agents.index(SESSION_CUT)].rstrip() + "\n\n" + session.strip() + "\n")
    shutil.rmtree(dest, ignore_errors=True)
    os.rename(tmp, dest)
    print(f"render-skills: rendered for {cfg['operator']['login']}/{cfg['bot']['login']}; "
          f"{len(leftovers)} lines still name cgwalters on purpose:", file=sys.stderr)
    for line in leftovers:
        print("  " + line, file=sys.stderr)


if __name__ == "__main__":
    main()
