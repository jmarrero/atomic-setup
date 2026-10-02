import os
import tempfile
import unittest

import botd


class JobFileTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.env = dict(os.environ)

    def tearDown(self):
        botd.remove_job_dir(self.dir, self.env)

    def test_reads_a_regular_file(self):
        with open(os.path.join(self.dir, "reply.md"), "w") as f:
            f.write("  the answer\n")
        self.assertEqual(botd.read_job_file(os.path.join(self.dir, "reply.md"), self.env), "the answer")

    def test_missing_file_is_empty(self):
        self.assertEqual(botd.read_job_file(os.path.join(self.dir, "reply.md"), self.env), "")

    def test_refuses_a_symlink_to_a_host_file(self):
        secret = os.path.join(self.dir, "secret")
        with open(secret, "w") as f:
            f.write("TOKEN")
        os.symlink(secret, os.path.join(self.dir, "reply.md"))
        with self.assertLogs("botd", "WARNING"):
            self.assertEqual(botd.read_job_file(os.path.join(self.dir, "reply.md"), self.env), "")

    def test_refuses_a_directory_and_oversized_files(self):
        os.mkdir(os.path.join(self.dir, "reply.md"))
        with self.assertLogs("botd", "WARNING"):
            self.assertEqual(botd.read_job_file(os.path.join(self.dir, "reply.md"), self.env), "")
        with open(os.path.join(self.dir, "big"), "w") as f:
            f.write("x" * 100)
        with self.assertLogs("botd", "WARNING"):
            self.assertEqual(botd.read_job_file(os.path.join(self.dir, "big"), self.env, limit=10), "")

    def test_remove_job_dir(self):
        os.makedirs(os.path.join(self.dir, "a", "b"))
        botd.remove_job_dir(self.dir, self.env)
        self.assertFalse(os.path.exists(self.dir))


if __name__ == "__main__":
    unittest.main()
