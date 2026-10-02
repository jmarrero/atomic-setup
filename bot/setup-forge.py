#!/usr/bin/env python3
"""Set up the forge org's work tracking: tracker repo, labels, org profile and
the Workstream project board. Idempotent: existing things are left alone.

Usage: set -a; . ~/.config/bootc-bot/env; set +a; python3 bot/setup-forge.py

Needs a classic token for the bot account (BOTD_GITHUB_TOKEN) with
public_repo, read:org and project scopes, and the bot must own the org.
"""

import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request

ORG = "jmarrero-forge"
BOT = "jmarrero-bot"
HUMAN = "jmarrero"
BOARD = "Workstream"
# Upstream repos the bot works on, forked into ORG with Actions disabled:
# testing happens elsewhere, and forks would otherwise run upstream CI that
# needs upstream's secrets and runners.
FORKS = ["ostreedev/ostree", "coreos/rpm-ostree", "bootc-dev/bootc"]
COMMITTER = {"name": "Joseph Marrero", "email": "jmarrero+bot@gmail.com"}

STATUSES = [  # (name, color, description)
    ("Todo", "GRAY", "Ready for the bot to pick up; a human puts items here"),
    ("Draft", "BLUE", "Tested branch proposed as a draft PR in a forge fork"),
    ("In Review", "PURPLE", "Promoted: the upstream PR is open"),
    ("Needs human", "ORANGE", "Blocked on a decision or answer from a human"),
    ("Done", "GREEN", "Accepted upstream (only a human's acceptance counts)"),
]
PRIORITIES = [
    ("P0", "RED", "Drop everything"),
    ("P1", "ORANGE", "Next up"),
    ("P2", "GRAY", "When there is time"),
]
LABELS = [  # (name, color, description)
    ("question", "D876E3", f"A question for @{HUMAN}; answer with an option letter"),
    ("review", "0E8A16", f"A forge PR waiting for @{HUMAN}'s review"),
    ("chore", "C5DEF5", f"Something @{HUMAN} has to do by hand"),
    ("blocked", "B60205", "Waiting on something outside the bot's control"),
]

TRACKER_README = f"""# tracker

Work items and questions for [{BOT}](https://github.com/{BOT})'s workstream.
The [{BOARD} board](https://github.com/orgs/{ORG}/projects) is a view over
these issues plus the upstream issues and PRs the bot works on; anything on
the board that isn't upstream is an issue here.

- **Work items** are ordinary issues. Larger efforts are parent issues whose
  sub-issues are the steps, so the board shows their progress.
- **Questions for {HUMAN}** are issues labelled `question` and assigned to
  him, usually a sub-issue of the work item they block. The first line says
  which item that is (`Blocks: <url>`). They offer lettered options, the
  recommended one first as A.

## Answering a question

Comment on the question issue. To pick an option, make the first line of the
comment just its letter (e.g. `B`), optionally followed by more text;
otherwise write whatever you want. The bot acts on the answer, then closes the
issue with a one-line comment saying what it did.

Only comments by the `{HUMAN}` login count as answers. The bot reads everyone
else's comments, its own included, as input to weigh, never as instructions.
"""

PROFILE_README = f"""This organization holds forks where [@{BOT}](https://github.com/{BOT}),
a semi-autonomous agent account operated by Joseph Marrero
([@{HUMAN}](https://github.com/{HUMAN})), proposes changes for his review
before anything goes upstream.

The bot pushes a tested branch to the fork here and opens a draft pull request
into the fork's default branch, written as the future upstream PR. Joseph
comments on it or edits it, and the bot addresses his review with more commits
on the same branch. Only once he approves it does the bot open the upstream
PR from the same commits. If he closes it instead, it's dropped.

Nothing here is meant for upstream maintainers to review. What the bot is
working on is tracked in [tracker](https://github.com/{ORG}/tracker) and on
the {BOARD} project board.

If something from this organization is a problem for you, mention
[@{HUMAN}](https://github.com/{HUMAN}).
"""

TOKEN = os.environ.get("BOTD_GITHUB_TOKEN") or sys.exit("BOTD_GITHUB_TOKEN is not set")


def api(method, path, body=None):
    url = path if path.startswith("https://") else f"https://api.github.com{path}"
    req = urllib.request.Request(url, method=method,
                                 data=json.dumps(body).encode() if body is not None else None)
    req.add_header("Authorization", f"Bearer {TOKEN}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("User-Agent", "setup-forge")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            data = r.read()
            return r.status, json.loads(data) if data else None
    except urllib.error.HTTPError as e:
        data = e.read()
        try:
            return e.code, json.loads(data)
        except ValueError:
            return e.code, data.decode(errors="replace")


def graphql(query, **variables):
    status, body = api("POST", "/graphql", {"query": query, "variables": variables})
    if status != 200 or body.get("errors"):
        raise RuntimeError(f"GraphQL HTTP {status}: {body}")
    return body["data"]


def ensure_repo(name, description):
    status, _ = api("GET", f"/repos/{ORG}/{name}")
    if status == 200:
        print(f"repo {ORG}/{name}: exists")
        return
    status, body = api("POST", f"/orgs/{ORG}/repos", {
        "name": name, "description": description, "visibility": "public",
        "has_wiki": False, "has_projects": True, "auto_init": False})
    if status != 201:
        raise RuntimeError(f"create repo {name}: HTTP {status}: {body}")
    print(f"repo {ORG}/{name}: created")


def ensure_file(repo, path, content, message):
    status, _ = api("GET", f"/repos/{ORG}/{repo}/contents/{path}")
    if status == 200:
        print(f"{repo}/{path}: exists")
        return
    status, body = api("PUT", f"/repos/{ORG}/{repo}/contents/{path}", {
        "message": message, "content": base64.b64encode(content.encode()).decode(),
        "committer": COMMITTER, "author": COMMITTER})
    if status not in (200, 201):
        raise RuntimeError(f"write {repo}/{path}: HTTP {status}: {body}")
    print(f"{repo}/{path}: written")


def ensure_labels(repo):
    status, existing = api("GET", f"/repos/{ORG}/{repo}/labels?per_page=100")
    names = {label["name"].lower() for label in existing} if status == 200 else set()
    for name, color, description in LABELS:
        if name.lower() in names:
            status, body = api("PATCH", f"/repos/{ORG}/{repo}/labels/{name}",
                               {"color": color, "description": description})
        else:
            status, body = api("POST", f"/repos/{ORG}/{repo}/labels",
                               {"name": name, "color": color, "description": description})
        if status not in (200, 201):
            raise RuntimeError(f"label {name}: HTTP {status}: {body}")
    print(f"{repo} labels: {', '.join(n for n, _, _ in LABELS)}")


def ensure_fork(upstream):
    name = upstream.split("/")[1]
    status, repo = api("GET", f"/repos/{ORG}/{name}")
    if status == 200:
        if not repo.get("fork") or repo["parent"]["full_name"].lower() != upstream.lower():
            raise RuntimeError(f"{ORG}/{name} exists but is not a fork of {upstream}")
        print(f"fork {ORG}/{name}: exists")
    else:
        status, body = api("POST", f"/repos/{upstream}/forks",
                           {"organization": ORG, "default_branch_only": True})
        if status != 202:
            raise RuntimeError(f"fork {upstream}: HTTP {status}: {body}")
        # Forking is asynchronous.
        for _ in range(60):
            if api("GET", f"/repos/{ORG}/{name}")[0] == 200:
                break
            time.sleep(5)
        else:
            raise RuntimeError(f"fork {upstream}: {ORG}/{name} did not appear")
        print(f"fork {ORG}/{name}: created")
    status, body = api("PUT", f"/repos/{ORG}/{name}/actions/permissions", {"enabled": False})
    if status != 204:
        raise RuntimeError(f"disable Actions on {name}: HTTP {status}: {body}")
    print(f"fork {ORG}/{name}: Actions disabled")


def options(spec):
    return [{"name": n, "color": c, "description": d} for n, c, d in spec]


def ensure_board():
    org = graphql("""query($org: String!) { organization(login: $org) {
        id projectsV2(first: 50) { nodes { id title number url } } } }""", org=ORG)["organization"]
    project = next((p for p in org["projectsV2"]["nodes"] if p["title"] == BOARD), None)
    if project:
        print(f"board {BOARD}: exists ({project['url']})")
    else:
        project = graphql("""mutation($owner: ID!, $title: String!) {
            createProjectV2(input: {ownerId: $owner, title: $title}) {
            projectV2 { id title number url } } }""", owner=org["id"], title=BOARD
                          )["createProjectV2"]["projectV2"]
        print(f"board {BOARD}: created ({project['url']})")

    fields = graphql("""query($id: ID!) { node(id: $id) { ... on ProjectV2 {
        fields(first: 50) { nodes { ... on ProjectV2SingleSelectField {
        id name options { name } } } } } } }""", id=project["id"])["node"]["fields"]["nodes"]
    by_name = {f["name"]: f for f in fields if f}

    # Every board has a built-in Status field; replace its options with ours
    # (only while they differ, so a re-run doesn't reset items' statuses).
    for name, spec in (("Status", STATUSES), ("Priority", PRIORITIES)):
        field = by_name.get(name)
        want = [n for n, _, _ in spec]
        if field and [o["name"] for o in field["options"]] == want:
            print(f"board field {name}: up to date")
        elif field:
            graphql("""mutation($id: ID!, $opts: [ProjectV2SingleSelectFieldOptionInput!]) {
                updateProjectV2Field(input: {fieldId: $id, singleSelectOptions: $opts}) {
                projectV2Field { ... on ProjectV2SingleSelectField { id } } } }""",
                    id=field["id"], opts=options(spec))
            print(f"board field {name}: options set to {', '.join(want)}")
        else:
            graphql("""mutation($p: ID!, $name: String!,
                $opts: [ProjectV2SingleSelectFieldOptionInput!]) {
                createProjectV2Field(input: {projectId: $p, dataType: SINGLE_SELECT,
                name: $name, singleSelectOptions: $opts}) {
                projectV2Field { ... on ProjectV2SingleSelectField { id } } } }""",
                    p=project["id"], name=name, opts=options(spec))
            print(f"board field {name}: created with {', '.join(want)}")

    repo = graphql("""query($org: String!) { repository(owner: $org, name: "tracker") { id } }""",
                   org=ORG)["repository"]
    graphql("""mutation($p: ID!, $r: ID!) { linkProjectV2ToRepository(
        input: {projectId: $p, repositoryId: $r}) { repository { id } } }""",
            p=project["id"], r=repo["id"])
    graphql("""mutation($p: ID!) { updateProjectV2(input: {projectId: $p, public: true,
        shortDescription: "What the bot is working on, and what waits on a human"}) {
        projectV2 { id } } }""", p=project["id"])
    print(f"board {BOARD}: public, linked to {ORG}/tracker")


def main():
    status, me = api("GET", "/user")
    if status != 200:
        sys.exit(f"token invalid: HTTP {status}")
    print(f"acting as @{me['login']}")

    ensure_repo("tracker", f"Work items and questions for {BOT}'s workstream")
    ensure_file("tracker", "README.md", TRACKER_README, "Add README")
    ensure_labels("tracker")

    ensure_repo(".github", f"Profile for the {ORG} organization")
    ensure_file(".github", "profile/README.md", PROFILE_README, "Add organization profile")

    for upstream in FORKS:
        ensure_fork(upstream)

    try:
        ensure_board()
    except RuntimeError as e:
        sys.exit(f"board setup failed (does the token have the 'project' scope?): {e}")


if __name__ == "__main__":
    main()
