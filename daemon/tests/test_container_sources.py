"""What a source says when its files are not there.

A container has no ``~/.claude`` and no Codex login, so the readers that
open files on the user's machine have nothing to read. They must say so
plainly, in the status, and never take the daemon down.
"""

import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from presto_ai_dash.sources.claude import ClaudeSource
from presto_ai_dash.sources.codex import CodexSource


class EmptyStorage:
    """Just enough storage for one source: it has no stored reading."""

    def observation(self, source_id):
        return None


class ContainerClaudeTests(unittest.TestCase):
    """A home directory with nothing in it: the case of a container."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)

    def source(self):
        """A Claude source whose profile is an empty home directory."""
        config = {"settings_path": str(self.home / ".claude" / "settings.json"),
                  "stale_after_seconds": 900, "usage_cache": True,
                  "activity_logs": True, "active_seconds": 300}
        source = ClaudeSource(config)
        source.id = "claude-1"
        source.attach_storage(EmptyStorage())
        return source

    def test_the_usage_cache_says_it_is_unavailable_and_why(self):
        report = self.source().collect().metrics["quota"]
        cache = report["usage_cache"]
        self.assertFalse(cache["available"])
        self.assertTrue(cache["reason"])
        self.assertTrue(cache["message"])

    def test_the_activity_reader_says_it_is_unavailable_and_why(self):
        report = self.source().collect().metrics["quota"]
        activity = report["local_activity"]
        self.assertFalse(activity["available"])
        self.assertTrue(activity["reason"])

    def test_the_source_reports_a_plain_status_and_does_not_crash(self):
        snapshot = self.source().collect_safely()
        self.assertIn(snapshot.status, ("warning", "ok", "active"))
        self.assertIsNone(snapshot.error)
        report = snapshot.metrics["quota"]
        # No reading is shown as a fresh zero.
        for window in report["windows"]:
            self.assertEqual(window["state"], "unavailable")
            self.assertIsNone(window["used_percent"])

    def test_the_note_says_the_reading_is_awaited(self):
        report = self.source().collect().metrics["quota"]
        self.assertIn("Awaiting", report["note"])

    def test_the_report_never_carries_a_path_or_a_home_name(self):
        report = self.source().collect().metrics["quota"]
        text = json.dumps(report)
        self.assertNotIn(str(self.home), text)


class ContainerCodexTests(unittest.TestCase):
    """Codex needs a local program and a local login: neither is in a container."""

    def test_a_missing_codex_program_is_an_offline_status_not_a_crash(self):
        source = CodexSource({"executable": str(self.home() / "no-codex-here"),
                              "profile_home": "", "timeout": 1,
                              "refresh_seconds": 60})
        snapshot = source.collect_safely()
        self.assertEqual(snapshot.status, "offline")
        self.assertIn("Codex", snapshot.error)
        report = snapshot.metrics["quota"]
        self.assertEqual(report["windows"][0]["state"], "unavailable")
        self.assertIsNone(report["windows"][0]["used_percent"])

    @staticmethod
    def home():
        return Path(tempfile.gettempdir())


if __name__ == "__main__":
    unittest.main()
