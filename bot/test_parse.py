import unittest

from botd import defuse_mentions, parse_command

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


class DefuseMentionsTest(unittest.TestCase):
    def test_the_bot_is_never_mentioned(self):
        out = defuse_mentions("Usage: `@jmarrero-bot <agent>`; ask @JMARRERO-BOT claude", BOT)
        self.assertNotRegex(out, r"(?i)@jmarrero-bot")
        self.assertEqual(out.replace("\u200b", ""), "Usage: `@jmarrero-bot <agent>`; ask @JMARRERO-BOT claude")
        # and a defused reply is no command for botd itself
        self.assertIsNone(parse(defuse_mentions("@jmarrero-bot claude hi", BOT)))

    def test_other_logins_are_left_alone(self):
        for text in ("@jmarrero please look", "@jmarrero-bot2 hi", "@jmarrero-bots"):
            self.assertEqual(defuse_mentions(text, BOT), text)


if __name__ == "__main__":
    unittest.main()
