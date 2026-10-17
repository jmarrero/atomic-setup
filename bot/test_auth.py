import os
import tempfile
import unittest
from unittest import mock

import botd

CFG = {
    "github": {"allowed_owners": ["bootc-dev"]},
    "auth": {"trusted_user_ids": [1], "allowed_user_ids": [2]},
    "runner": {},
    "agents": {"claude": {"command": ["claude", "{prompt}"]}},
}


class AuthTest(unittest.TestCase):
    def bot(self, cfg=CFG):
        with mock.patch.dict(os.environ, {"BOTD_GITHUB_TOKEN": "x"}, clear=False):
            os.environ.pop("BOTD_MEMBERSHIP_TOKEN", None)
            return botd.Bot(cfg, os.path.join(tempfile.mkdtemp(), "state.json"))

    def test_only_listed_ids_without_org(self):
        b = self.bot()
        self.assertIsNone(b.member_token)
        with mock.patch.object(botd, "gh", side_effect=AssertionError("no API call")):
            self.assertTrue(b.is_authorized("operator", 1))
            self.assertTrue(b.is_authorized("allowed", 2))
            self.assertFalse(b.is_authorized("someone", 3))

    def test_allowed_ids_only_in_allowed_owners(self):
        b = self.bot()
        b.bot_login = "bot"
        dispatched = []
        b.dispatch = dispatched.append
        comment = {"id": 10, "user": {"login": "allowed", "id": 2}, "body": "@bot claude hi",
                   "created_at": botd.iso(botd.datetime.now(botd.timezone.utc)),
                   "html_url": "u"}
        issue = dict(comment, id=11, body="", title="t")

        def fake_gh(method, path, token, *a, **k):
            return 200, {}, issue

        for owner, expected in (("bootc-dev", 1), ("elsewhere", 0)):
            dispatched.clear()
            b.processed.clear()
            n = {"repository": {"full_name": f"{owner}/r", "owner": {"login": owner}},
                 "subject": {"type": "Issue", "url": f"https://api.github.com/repos/{owner}/r/issues/5"}}
            with mock.patch.object(botd, "gh", fake_gh), \
                 mock.patch.object(botd, "gh_list", return_value=[comment]):
                b.handle_thread(n)
            self.assertEqual(len(dispatched), expected, owner)


if __name__ == "__main__":
    unittest.main()
