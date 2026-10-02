import os
import tempfile
import unittest
from unittest import mock

import botd

CFG = {
    "github": {"allowed_owners": ["bootc-dev"], "poll_min_seconds": 30},
    "auth": {"trusted_user_ids": [1]},
    "runner": {},
    "agents": {},
}


def thread(id, updated):
    return {"id": id, "updated_at": updated, "repository": {}, "subject": {}}


class PollTest(unittest.TestCase):
    def setUp(self):
        with mock.patch.dict(os.environ, {"BOTD_GITHUB_TOKEN": "x"}):
            self.bot = botd.Bot(CFG, os.path.join(tempfile.mkdtemp(), "state.json"))
        self.calls = []
        self.notifications = []
        self.handled = []
        self.bot.handle_thread = lambda n: self.handled.append((n["id"], n["updated_at"]))

    def gh(self, method, path, token, *a, **k):
        self.calls.append((method, path))
        return 200, {"X-Poll-Interval": "60"}, self.notifications

    def poll(self):
        with mock.patch.object(botd, "gh", self.gh):
            return self.bot.poll_once()

    def test_reads_read_and_unread_and_never_marks_read(self):
        self.notifications = [thread("1", "t1")]
        self.assertEqual(self.poll(), 60)
        method, path = self.calls[0]
        self.assertEqual(method, "GET")
        self.assertIn("all=true", path)
        self.assertIn("since=", path)
        self.assertEqual([c for c in self.calls if c[0] != "GET"], [])

    def test_a_thread_is_handled_again_only_when_it_changes(self):
        self.notifications = [thread("1", "t1"), thread("2", "t1")]
        self.poll()
        self.poll()
        self.notifications = [thread("1", "t2"), thread("2", "t1")]
        self.poll()
        self.assertEqual(self.handled, [("1", "t1"), ("2", "t1"), ("1", "t2")])

    def test_a_failed_thread_is_retried(self):
        attempts = []

        def flaky(n):
            attempts.append(n["id"])
            if len(attempts) == 1:
                raise RuntimeError("transient")
        self.bot.handle_thread = flaky
        self.notifications = [thread("1", "t1")]
        with self.assertLogs("botd", "ERROR"):
            self.poll()
        self.poll()
        self.poll()
        self.assertEqual(attempts, ["1", "1"])

    def test_threads_out_of_the_window_are_forgotten(self):
        self.notifications = [thread("1", "t1")]
        self.poll()
        self.notifications = []
        self.poll()
        self.assertEqual(self.bot.seen_threads, {})


if __name__ == "__main__":
    unittest.main()
