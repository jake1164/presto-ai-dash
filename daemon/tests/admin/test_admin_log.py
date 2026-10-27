"""The log on the Status tab: a bounded in-memory buffer and its read-only endpoint."""

import logging
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from presto_ai_dash.logbuffer import LogBuffer, MAX_LINES
from admin.test_management import AdminServerBase


def record(level, message, name="presto_ai_dash.test", exc_info=None):
    return logging.LogRecord(name, level, __file__, 1, message, None, exc_info)


class LogBufferTests(unittest.TestCase):
    def setUp(self):
        self.clock = [1000.0]
        self.buffer = LogBuffer(max_lines=50, clock=lambda: self.clock[0])

    def add(self, level, message, **kwargs):
        self.buffer.emit(record(level, message, **kwargs))
        self.clock[0] += 1

    def test_lines_come_back_newest_first_with_their_level_and_source(self):
        self.add(logging.INFO, "first")
        self.add(logging.WARNING, "second", name="presto_ai_dash.sources.litellm")
        view = self.buffer.view()
        self.assertEqual([line["message"] for line in view["lines"]], ["second", "first"])
        self.assertEqual(view["lines"][0]["level"], "WARNING")
        self.assertEqual(view["lines"][0]["logger"], "presto_ai_dash.sources.litellm")
        self.assertEqual((view["matched"], view["held"], view["capacity"]), (2, 2, 50))

    def test_a_page_is_limited_and_says_how_many_more_match(self):
        for number in range(30):
            self.add(logging.INFO, "line %d" % number)
        view = self.buffer.view(limit=10)
        self.assertEqual(len(view["lines"]), 10)
        self.assertEqual(view["matched"], 30)
        self.assertEqual(view["lines"][0]["message"], "line 29")
        self.assertEqual(self.buffer.view(limit="junk")["lines"][0]["message"], "line 29")
        self.assertEqual(len(self.buffer.view(limit=10**9)["lines"]), 30)

    def test_the_buffer_never_holds_more_than_its_capacity(self):
        for number in range(80):
            self.add(logging.INFO, "line %d" % number)
        view = self.buffer.view(limit=1000)
        self.assertEqual(view["held"], 50)
        self.assertEqual(view["lines"][-1]["message"], "line 30")

    def test_a_level_keeps_that_level_and_above(self):
        for level in (logging.DEBUG, logging.INFO, logging.WARNING, logging.ERROR):
            self.add(level, "at %s" % logging.getLevelName(level))
        self.assertEqual([l["level"] for l in self.buffer.view(level="WARNING")["lines"]],
                         ["ERROR", "WARNING"])
        self.assertEqual(self.buffer.view(level="ERROR")["matched"], 1)
        self.assertEqual(self.buffer.view(level="nonsense")["matched"], 4)

    def test_a_search_matches_the_message_or_the_source_ignoring_case(self):
        self.add(logging.INFO, "polling LiteLLM", name="presto_ai_dash.a")
        self.add(logging.INFO, "something else", name="presto_ai_dash.codex")
        self.assertEqual(self.buffer.view(text="litellm")["matched"], 1)
        self.assertEqual(self.buffer.view(text="CODEX")["matched"], 1)
        self.assertEqual(self.buffer.view(text="nothing like this")["matched"], 0)

    def test_a_repeating_line_is_counted_even_when_others_come_between(self):
        for _ in range(5):
            self.add(logging.WARNING, "litellm daily activity returned HTTP 403")
            self.add(logging.WARNING, "codex not found", name="presto_ai_dash.codex")
        view = self.buffer.view()
        self.assertEqual(view["held"], 2)
        counts = {line["message"]: line["count"] for line in view["lines"]}
        self.assertEqual(counts, {"litellm daily activity returned HTTP 403": 5,
                                  "codex not found": 5})
        self.assertGreater(view["lines"][0]["time"], 1000.0 + 5)  # the last time it was seen

    def test_a_line_that_keeps_coming_stays_at_the_top(self):
        self.add(logging.WARNING, "keeps failing")
        self.add(logging.INFO, "something else")
        self.add(logging.WARNING, "keeps failing")
        self.assertEqual([(l["message"], l["count"]) for l in self.buffer.view()["lines"]],
                         [("keeps failing", 2), ("something else", 1)])

    def test_a_url_stays_readable_and_a_file_url_is_hidden(self):
        self.add(logging.INFO, "page at http://0.0.0.0:8788/admin/x and http://h/a/b/c")
        self.add(logging.INFO, "reading file:///home/me/secret.json")
        texts = [line["message"] for line in self.buffer.view()["lines"]]
        self.assertEqual(texts[1], "page at http://0.0.0.0:8788/admin/x and http://h/a/b/c")
        self.assertNotIn("secret.json", texts[0])

    def test_secrets_paths_and_tracebacks_are_never_kept(self):
        try:
            raise ValueError("password=hunter2 in C:\\Users\\me\\dash.toml")
        except ValueError:
            import sys as system
            self.buffer.emit(record(logging.ERROR, "failed with token=abc123 at /home/me/x.db",
                                    exc_info=system.exc_info()))
        text = self.buffer.view()["lines"][0]["message"]
        for leaked in ("abc123", "hunter2", "/home/me", "C:\\Users"):
            self.assertNotIn(leaked, text)
        self.assertTrue(text.endswith("(ValueError)"))

    def test_the_default_capacity_is_bounded(self):
        self.assertEqual(LogBuffer().max_lines, MAX_LINES)

    def test_emit_never_raises(self):
        bad = record(logging.INFO, "%s %s")
        bad.args = (1,)  # too few arguments for the format
        self.buffer.emit(bad)


class LogEndpointTests(AdminServerBase):
    def setUp(self):
        super().setUp()
        logging.getLogger().addHandler(self.manager.logs)
        self.addCleanup(logging.getLogger().removeHandler, self.manager.logs)

    def test_it_needs_the_admin_token(self):
        self.assertEqual(self.request("/api/admin/logs", token=None)[0], 401)
        self.assertEqual(self.request("/api/admin/logs", token="wrong")[0], 401)

    def test_it_shows_what_the_daemon_logged_and_filters_it(self):
        logging.getLogger("presto_ai_dash.test").warning("the gateway refused the key")
        status, _, body = self.request("/api/admin/logs?level=WARNING&q=gateway")
        self.assertEqual(status, 200)
        self.assertEqual(body["lines"][0]["message"], "the gateway refused the key")
        self.assertTrue(body["lines"][0]["time"].endswith("Z") or "+" in body["lines"][0]["time"])
        self.assertIn("level", body)
        self.assertEqual(self.request("/api/admin/logs?q=zzzzzz")[2]["matched"], 0)

    def test_the_limit_is_honoured_and_bad_values_do_not_break_it(self):
        for number in range(7):
            logging.getLogger("presto_ai_dash.test").warning("line %d", number)
        self.assertEqual(len(self.request("/api/admin/logs?limit=3")[2]["lines"]), 3)
        self.assertEqual(self.request("/api/admin/logs?limit=abc&level=%FF")[0], 200)

    def test_it_only_reads(self):
        status = self.request("/api/admin/logs", method="POST", data={})[0]
        self.assertIn(status, (401, 403, 404, 405))


class LogPageTests(unittest.TestCase):
    def test_the_page_offers_filter_search_and_more(self):
        root = Path(__file__).resolve().parents[2] / "src" / "presto_ai_dash" / "web"
        page = (root / "index.html").read_text(encoding="utf-8")
        script = (root / "admin.js").read_text(encoding="utf-8")
        for needle in ('id="log-level"', 'id="log-search"', 'id="log-lines"', 'id="log-more"'):
            self.assertIn(needle, page)
        self.assertIn("/api/admin/logs?limit=", script)
        self.assertIn("await refreshLog();", script)


if __name__ == "__main__":
    unittest.main()
