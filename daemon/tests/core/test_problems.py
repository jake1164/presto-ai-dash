"""The problems buffer: the daemon's recent warnings and errors.

The buffer lives in memory only and is lost on restart. It holds the last 50
records, de-duplicated, and never a file's contents or a secret.
"""

import logging
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from presto_ai_dash.problems import MAX_PROBLEMS, ProblemBuffer, clean_message


class CleanMessageTests(unittest.TestCase):
    """A message is shown short, without paths and without credentials."""

    def test_a_plain_message_is_kept(self):
        self.assertEqual(clean_message("collection failed"), "collection failed")

    def test_newlines_become_spaces(self):
        self.assertEqual(clean_message("first line\nsecond line"), "first line second line")

    def test_a_path_is_replaced(self):
        message = clean_message("cannot read C:\\Users\\someone\\.claude.json")
        self.assertNotIn("someone", message)
        self.assertIn("<path>", message)

    def test_a_bearer_token_is_replaced(self):
        message = clean_message("rejected Authorization: Bearer sekrit-value")
        self.assertNotIn("sekrit-value", message)
        self.assertIn("<redacted>", message)

    def test_a_token_parameter_is_replaced(self):
        message = clean_message("failed with api_key=abcd1234")
        self.assertNotIn("abcd1234", message)

    def test_a_long_message_is_cut(self):
        self.assertLessEqual(len(clean_message("x" * 500)), 200)


class ProblemBufferTests(unittest.TestCase):
    """The buffer keeps the last warnings and errors, newest first."""

    def setUp(self):
        self.buffer = ProblemBuffer()
        self.logger = logging.getLogger("presto_ai_dash.test")
        self.logger.propagate = False
        self.logger.setLevel(logging.DEBUG)
        self.logger.addHandler(self.buffer)
        self.addCleanup(self.logger.removeHandler, self.buffer)

    def test_a_warning_is_kept(self):
        self.logger.warning("something went wrong")
        problems = self.buffer.problems()
        self.assertEqual(len(problems), 1)
        self.assertEqual(problems[0]["message"], "something went wrong")
        self.assertEqual(problems[0]["level"], "WARNING")
        self.assertEqual(problems[0]["count"], 1)

    def test_an_error_is_kept(self):
        self.logger.error("it broke")
        self.assertEqual(self.buffer.problems()[0]["level"], "ERROR")

    def test_information_is_not_kept(self):
        self.logger.info("just so you know")
        self.assertEqual(self.buffer.problems(), [])

    def test_the_newest_problem_comes_first(self):
        self.logger.warning("first")
        self.logger.warning("second")
        self.assertEqual([problem["message"] for problem in self.buffer.problems()],
                         ["second", "first"])

    def test_the_same_problem_is_counted_once(self):
        self.logger.warning("the same thing")
        self.logger.warning("the same thing")
        self.logger.warning("the same thing")
        problems = self.buffer.problems()
        self.assertEqual(len(problems), 1)
        self.assertEqual(problems[0]["count"], 3)

    def test_different_problems_are_not_merged(self):
        self.logger.warning("one thing")
        self.logger.warning("another thing")
        self.assertEqual(len(self.buffer.problems()), 2)

    def test_at_most_fifty_problems_are_kept(self):
        for index in range(MAX_PROBLEMS + 10):
            self.logger.warning("problem %d" % index)
        problems = self.buffer.problems()
        self.assertEqual(len(problems), MAX_PROBLEMS)
        self.assertEqual(problems[0]["message"], "problem %d" % (MAX_PROBLEMS + 9))

    def test_an_exception_contributes_only_its_type(self):
        try:
            raise RuntimeError("secret detail inside the traceback")
        except RuntimeError:
            self.logger.exception("collection failed")
        message = self.buffer.problems()[0]["message"]
        self.assertIn("RuntimeError", message)
        self.assertNotIn("secret detail", message)

    def test_the_buffer_is_empty_after_a_restart(self):
        self.logger.warning("gone after a restart")
        self.assertEqual(ProblemBuffer().problems(), [])


if __name__ == "__main__":
    unittest.main()


class SecretShapesTests(unittest.TestCase):
    """Credential-shaped text never reaches the Problems list."""

    def test_these_shapes_are_redacted(self):
        from presto_ai_dash.problems import clean_message
        secrets = ["hunter2", "sk-ant-api03-AAAAAAAAAAAAAAAA", "abcd1234efgh", "abc.def.ghi"]
        for message in (
                "fetch failed: password=hunter2",
                "bad config secret: hunter2",
                "headers {'x-api-key': 'sk-ant-api03-AAAAAAAAAAAAAAAA'}",
                "cookie: session=abcd1234efgh",
                "Authorization: Bearer abc.def.ghi rejected",
                "leaked sk-ant-api03-AAAAAAAAAAAAAAAA here",
                "digest " + "a" * 40):
            shown = clean_message(message)
            for secret in secrets + ["a" * 40]:
                self.assertNotIn(secret, shown, message)

    def test_ordinary_text_survives(self):
        from presto_ai_dash.problems import clean_message
        self.assertEqual(clean_message("Ollama is not reachable: refused"),
                         "Ollama is not reachable: refused")

    def test_an_authorization_header_is_redacted_whatever_its_scheme(self):
        from presto_ai_dash.problems import clean_message
        for message, secret in (
                ("Authorization: Basic dXNlcjpwYXNz", "dXNlcjpwYXNz"),
                ("authorization=Bearer abcdef", "abcdef"),
                ("AUTHORIZATION: Token s3cr3t", "s3cr3t"),
                ("headers {'Authorization': 'Basic Zm9vOmJhcg=='}", "Zm9vOmJhcg"),
                ('{"authorization": "Digest xyz"}', "xyz")):
            with self.subTest(message=message):
                shown = clean_message(message)
                self.assertNotIn(secret, shown)
                self.assertIn("<redacted>", shown)

    def test_short_password_style_values_are_redacted(self):
        from presto_ai_dash.problems import clean_message
        for message, secret in (("login failed pass=abc", "abc"),
                                ("pwd: hunter", "hunter"),
                                ("passphrase=correct", "correct"),
                                ("PASSWORD = 'x1'", "x1")):
            with self.subTest(message=message):
                self.assertNotIn(secret, clean_message(message))

    def test_words_that_only_look_like_credentials_survive(self):
        from presto_ai_dash.problems import clean_message
        for message in ("authorization failed for the source",
                        "checks passed: 3 sources", "bypass: not needed",
                        "the password manager is not involved"):
            with self.subTest(message=message):
                self.assertEqual(clean_message(message), message)

    def test_the_pattern_holds_no_control_characters(self):
        from presto_ai_dash import problems
        self.assertEqual([char for char in problems._SECRET.pattern if ord(char) < 32], [])
