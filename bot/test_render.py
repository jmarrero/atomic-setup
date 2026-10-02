"""Tests of coordinator/render-skills.py against the homegit checkout, if there is one."""
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
RENDER = os.path.join(HERE, "coordinator", "render-skills.py")
GOALS = os.path.join(HERE, "coordinator", "operator-goals.md")
HOMEGIT = os.environ.get("HOMEGIT", os.path.expanduser("~/development/github/jmarrero-forge/homegit"))
CONFIG = json.load(open(os.path.join(HERE, "operator.json")))


@unittest.skipUnless(os.path.isdir(os.path.join(HOMEGIT, "dotfiles/.agents/skills")),
                     f"no homegit checkout at {HOMEGIT}")
class RenderTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        # bot-operator --json, with the values operator.json leaves to defaults.
        cfg = dict(CONFIG, tracker_repo=f"{CONFIG['forge_org']}/tracker")
        cfg["board"] = dict(owner_type="orgs", owner=CONFIG["forge_org"], number=1, **CONFIG.get("board", {}))
        stub = os.path.join(cls.tmp, "bot-operator")
        with open(stub, "w") as f:
            f.write(f"#!/bin/sh\ncat <<'EOF'\n{json.dumps(cfg)}\nEOF\n")
        os.chmod(stub, stat.S_IRWXU)
        cls.out = os.path.join(cls.tmp, "rendered")
        env = dict(os.environ, PATH=f"{cls.tmp}:{os.environ['PATH']}")
        cls.proc = subprocess.run([sys.executable, RENDER, HOMEGIT, cls.out, GOALS],
                                  env=env, capture_output=True, text=True)

    def read(self, rel):
        return open(os.path.join(self.out, rel)).read()

    def test_renders(self):
        self.assertEqual(self.proc.returncode, 0, self.proc.stderr)

    def test_identities_and_places_are_ours(self):
        text = "\n".join(self.read(os.path.join(root, f))
                         for root, _, files in os.walk(self.out) for f in files)
        for wrong in ("walters@verbum.org", "walters+llm@verbum.org", "Colin Walters",
                      "orgs/cgwalters-forge/projects/1", "cgwalters-forge/tracker#176",
                      "~/src/github/cgwalters-bot", "github.com/cgwalters/#llms",
                      "bootc-dev/cgwalters-devspace-sandbox"):
            self.assertNotIn(wrong, text)
        # Everything still naming cgwalters is one of his own things, kept on purpose.
        allowed = re.compile(r"cgwalters-forge/(harness-coordination|tracker#\d+|review|workflow-compiler"
                             r"|agentic-job|cgwalters-devspace-sandbox#\d+)|cgwalters-forge\.github\.io"
                             r"|cgwalters-bot/(praxis-credential-broker|debug-bootc|ostree-missing-refs)"
                             r"|gist\.github\.com/cgwalters-bot|users/cgwalters-bot/projects/2"
                             r"|cgwalters' harness|cgwalters-bot (and|or) cgwalters|`cgwalters-bot` (and|or) `cgwalters`"
                             r"|(@|`)?cgwalters-bot`?( questions| in that)|for cgwalters-bot|cgwalters\s+for jmarrero-bot|cgwalters-bot and\s*$")
        for line in text.splitlines():
            rest = allowed.sub("", line)
            self.assertNotRegex(rest, r"(?i)cgwalters", line)

    def test_goals_are_the_operators(self):
        coordinator = self.read("skills/coordinator/SKILL.md")
        self.assertIn("The operator is jmarrero.", coordinator)
        for skill in ("skills/coordinator/SKILL.md", "skills/workstream/SKILL.md",
                      "skills/backlog-planning/SKILL.md"):
            self.assertNotRegex(self.read(skill), r"(?i)composefs stability|toward stable", skill)

    def test_peers_are_cgwalters_harness(self):
        self.assertIn("coordination questions from cgwalters-bot and cgwalters", self.read("skills/bot-notify/SKILL.md"))
        self.assertIn("the channel with cgwalters' harness", self.read("skills/coordinator/worker-preamble.md"))

    def test_paths_point_at_the_rendered_skills(self):
        self.assertIn("~/.agents/skills/coordinator/", self.read("skills/coordinator/SKILL.md"))
        self.assertNotIn("homegit/dotfiles/.agents/skills", self.read("skills/coordinator/SKILL.md"))


if __name__ == "__main__":
    unittest.main()
