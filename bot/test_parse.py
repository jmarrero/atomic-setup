import unittest

from botd import parse_command

BOT = "jmarrero-bot"
COMMANDS = {"claude", "opencode", "help"}


def parse(body):
    return parse_command(body, BOT, COMMANDS)


class ParseCommandTest(unittest.TestCase):
    def test_agent_commands(self):
        self.assertEqual(parse("@jmarrero-bot opencode can you summarize the PR?"),
                         ("opencode", "can you summarize the PR?"))
        self.assertEqual(parse("thanks\n  @jmarrero-bot claude explain this\nbye"),
                         ("claude", "explain this\nbye"))
        self.assertEqual(parse("@JMARRERO-BOT Help"), ("help", ""))

    def test_other_mentions_are_the_coordinators(self):
        # Requests that aren't botd commands get no "Unknown agent" reply.
        for body in ("@jmarrero-bot open code can you summarize the PR?",
                     "@jmarrero-bot can you fix the build?",
                     "@jmarrero-bot add a P2 board item titled \"routing test\"",
                     "@jmarrero-bot claudes idea",
                     "see what @jmarrero-bot claude said",
                     "> @jmarrero-bot claude quoted"):
            self.assertIsNone(parse(body), body)

    def test_a_command_on_a_later_line(self):
        self.assertEqual(parse("@jmarrero-bot please look at this\n@jmarrero-bot claude why?"),
                         ("claude", "why?"))


if __name__ == "__main__":
    unittest.main()
