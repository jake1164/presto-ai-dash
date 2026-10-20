"""Phase 5d: the usage cache inside the Claude source, the card and the admin page.

The reader itself is covered by test_claude_usage_cache.py. Here the cache is
merged with the terminal export, labelled everywhere it is shown, and proved
to change nothing when it is switched off. All fixtures are synthetic and live
in a temp directory; no test reads ~/.claude.json.
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path
import json
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from presto_ai_dash.dashboard import _account_card, build_instance_payload
from presto_ai_dash.models import SourceSnapshot
from presto_ai_dash.quotas import (claude_provenance, present_quota, quota_report,
                                    quota_window)
from presto_ai_dash.sources.claude import ClaudeSource
from test_claude_usage_cache import cache_document
from test_management import ManagementBase

NOW = datetime(2026, 10, 20, 8, tzinfo=timezone.utc)
DAY = timedelta(days=1)


def observation_for(report, seen=None, host="laptop"):
    """The storage.observation row shape ClaudeSource.collect consumes."""
    return {"report": report, "evidence": {}, "collector_seen_at": seen,
            "collector_host": host, "collector_version": None,
            "last_progress_at": seen, "progress": None, "last_activity_at": None}


def terminal_observation(observed, percent=12.0, weekly=34.0):
    """A stored terminal-export reading, as the status line would push it."""
    report = quota_report("anthropic", "Claude", [
        quota_window("five_hour", "5h", percent, NOW + timedelta(hours=4),
                     observed, 300),
        quota_window("seven_day", "Weekly", weekly, NOW + timedelta(days=4),
                     observed, 10080)])
    report["note"] = ClaudeSource.report_note
    return observation_for(report, seen=observed.isoformat())


class FakeStorage:
    """Just enough storage for one source: the stored observation."""

    def __init__(self, observation=None):
        self.observation_row = observation
        self.reads = 0

    def observation(self, source_id):
        self.reads += 1
        return self.observation_row


class SourceBase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / ".claude").mkdir()
        self.settings = self.root / ".claude" / "settings.json"
        self.settings.write_text("{}", encoding="utf-8")
        self.cache = self.root / ".claude.json"

    def write_cache(self, document=None, path=None):
        target = Path(path) if path else self.cache
        target.write_text(json.dumps(document if document is not None
                                     else cache_document()), encoding="utf-8")
        return target

    def source(self, observation=None, usage_cache=True, usage_cache_path=""):
        config = {"settings_path": str(self.settings), "stale_after_seconds": 900,
                  "usage_cache_path": usage_cache_path}
        if usage_cache:
            config["usage_cache"] = True
        source = ClaudeSource(config)
        source.id = "claude-1"
        source.attach_storage(FakeStorage(observation))
        return source

    def collect(self, source):
        with patch("presto_ai_dash.sources.claude.now_utc", return_value=NOW):
            return source.collect()

    def window(self, report, window_id):
        return next(w for w in report["windows"] if w["id"] == window_id)


class MergeTests(SourceBase):
    """The newest reading per window wins; the terminal export wins a tie."""

    def test_a_newer_cache_reading_wins_the_window(self):
        self.write_cache(cache_document(fetched=NOW - timedelta(minutes=5)))
        report = self.collect(self.source(terminal_observation(NOW - 6 * DAY))).metrics["quota"]
        window = self.window(report, "five_hour")
        self.assertEqual(window["used_percent"], 12.0)
        self.assertEqual(window["source"], "usage cache")
        self.assertEqual(window["observed_at"], (NOW - timedelta(minutes=5)).isoformat())

    def test_an_older_cache_reading_does_not_win_the_window(self):
        self.write_cache(cache_document(fetched=NOW - 6 * DAY))
        report = self.collect(self.source(terminal_observation(NOW - timedelta(hours=1)))).metrics["quota"]
        window = self.window(report, "five_hour")
        self.assertEqual(window["used_percent"], 12.0)
        self.assertEqual(window["source"], "terminal export")

    def test_a_tie_goes_to_the_terminal_export(self):
        observed = NOW - timedelta(hours=2)
        self.write_cache(cache_document(fetched=observed))
        report = self.collect(self.source(terminal_observation(observed))).metrics["quota"]
        self.assertEqual(self.window(report, "five_hour")["source"], "terminal export")

    def test_the_cache_alone_fills_the_windows(self):
        self.write_cache(cache_document(fetched=NOW - timedelta(minutes=5)))
        report = self.collect(self.source(None)).metrics["quota"]
        self.assertEqual(self.window(report, "five_hour")["used_percent"], 12.0)
        self.assertEqual(self.window(report, "five_hour")["source"], "usage cache")
        self.assertEqual(self.window(report, "seven_day")["used_percent"], 66.0)
        self.assertEqual(self.window(report, "seven_day")["source"], "usage cache")

    def test_a_window_only_the_cache_reports_comes_from_the_cache(self):
        self.write_cache(cache_document(fetched=NOW - timedelta(minutes=5), weekly=None))
        report = self.collect(self.source(terminal_observation(NOW - 6 * DAY))).metrics["quota"]
        self.assertEqual(self.window(report, "five_hour")["source"], "usage cache")
        self.assertEqual(self.window(report, "seven_day")["source"], "terminal export")
        self.assertEqual(self.window(report, "seven_day")["used_percent"], 34.0)

    def test_without_a_reading_the_windows_stay_unavailable(self):
        report = self.collect(self.source(None)).metrics["quota"]
        for window in report["windows"]:
            self.assertIsNone(window["used_percent"])
            self.assertEqual(window["state"], "unavailable")

    def test_every_window_says_where_it_came_from(self):
        self.write_cache(cache_document(fetched=NOW - timedelta(minutes=5)))
        report = self.collect(self.source(terminal_observation(NOW - 6 * DAY))).metrics["quota"]
        for window in report["windows"]:
            self.assertIn(window["source"], ("terminal export", "usage cache"))

    def test_the_terminal_export_windows_are_labelled_too(self):
        report = self.collect(self.source(terminal_observation(NOW - 6 * DAY),
                                           usage_cache=False)).metrics["quota"]
        for window in report["windows"]:
            self.assertEqual(window["source"], "terminal export")

    def test_the_cache_is_not_stored_anywhere(self):
        self.write_cache(cache_document(fetched=NOW - timedelta(minutes=5)))
        storage = FakeStorage(terminal_observation(NOW - 6 * DAY))
        source = self.source(terminal_observation(NOW - 6 * DAY))
        source.attach_storage(storage)
        before = json.dumps(storage.observation_row, sort_keys=True)
        self.collect(source)
        after = json.dumps(storage.observation_row, sort_keys=True)
        self.assertEqual(before, after)


class StatusTests(SourceBase):
    """A cache failure never makes a working source worse."""

    def test_a_fresh_cache_reading_is_a_fresh_quota(self):
        self.write_cache(cache_document(fetched=NOW - timedelta(minutes=2)))
        snapshot = self.collect(self.source(terminal_observation(NOW - 6 * DAY)))
        self.assertEqual(snapshot.status, "ok")
        presented = present_quota(snapshot.metrics["quota"], NOW)
        self.assertIsNone(presented["hint"])
        self.assertEqual(presented["windows"][0]["state"], "available")

    def test_an_old_cache_reading_ages_like_any_other_reading(self):
        self.write_cache(cache_document(fetched=NOW - 2 * DAY))
        snapshot = self.collect(self.source(terminal_observation(NOW - 6 * DAY)))
        presented = present_quota(snapshot.metrics["quota"], NOW)
        self.assertEqual(presented["windows"][0]["state"], "stale")
        self.assertIsNotNone(presented["hint"])

    def test_an_unavailable_reader_leaves_the_terminal_export_alone(self):
        broken = self.root / ".claude.json"
        broken.write_text("{broken", encoding="utf-8")
        source = self.source(terminal_observation(NOW - timedelta(minutes=1)))
        with patch("presto_ai_dash.sources.claude.now_utc", return_value=NOW):
            snapshot = source.collect()
        self.assertEqual(snapshot.status, "ok")
        report = snapshot.metrics["quota"]
        self.assertIs(report["usage_cache"]["available"], False)
        self.assertEqual(self.window(report, "five_hour")["source"], "terminal export")

    def test_a_missing_cache_never_marks_the_source_offline(self):
        source = self.source(terminal_observation(NOW - timedelta(minutes=1)))
        snapshot = self.collect(source)
        self.assertEqual(snapshot.status, "ok")
        self.assertNotEqual(snapshot.status, "offline")
        self.assertFalse(snapshot.metrics["quota"]["usage_cache"]["available"])

    def test_the_cache_alone_gives_a_usable_account(self):
        self.write_cache(cache_document(fetched=NOW - timedelta(minutes=2)))
        snapshot = self.collect(self.source(None))
        self.assertEqual(snapshot.status, "ok")


class NoteTests(SourceBase):
    """The note keeps the terminal-export wording and adds the cache fact."""

    def test_the_note_keeps_the_terminal_export_wording(self):
        self.write_cache()
        report = self.collect(self.source(terminal_observation(NOW - 6 * DAY))).metrics["quota"]
        self.assertIn("terminal export", report["note"])
        self.assertIn("usage cache", report["note"])

    def test_the_reader_off_keeps_the_old_note(self):
        report = self.collect(self.source(terminal_observation(NOW - 6 * DAY),
                                           usage_cache=False)).metrics["quota"]
        self.assertEqual(report["note"],
                         "Claude Code terminal export; idle readings become stale. "
                         "The VS Code panel does not run the status line, so quota "
                         "only updates from terminal sessions.")

    def test_the_awaiting_note_mentions_the_cache_when_it_is_on(self):
        report = self.collect(self.source(None)).metrics["quota"]
        self.assertIn("usage cache", report["note"])

    def test_the_cache_alone_names_both_sources(self):
        self.write_cache()
        report = self.collect(self.source(None)).metrics["quota"]
        self.assertIn("terminal export", report["note"])
        self.assertIn("usage cache", report["note"])


class ReaderOffTests(SourceBase):
    """With the reader off, the source behaves exactly as before."""

    def test_nothing_of_the_cache_reaches_the_report(self):
        report = self.collect(self.source(terminal_observation(NOW - 6 * DAY),
                                           usage_cache=False)).metrics["quota"]
        self.assertNotIn("usage_cache", report)
        self.assertNotIn("local_activity", report)

    def test_the_reader_off_is_the_default(self):
        fields = {field["key"]: field for field in ClaudeSource.config_fields}
        self.assertEqual(fields["usage_cache"]["type"], "bool")
        self.assertIs(fields["usage_cache"]["default"], False)
        self.assertEqual(fields["usage_cache"]["label"],
                         "Read Claude Code's local usage cache (usage numbers only)")
        self.assertEqual(fields["usage_cache_path"]["type"], "string")
        self.assertEqual(fields["usage_cache_path"]["default"], "")
        self.assertEqual(fields["usage_cache_path"]["label"],
                         "Claude Code state file (blank: automatic)")


class QuotaWindowTests(unittest.TestCase):
    """The window label of the source is accepted, and validated."""

    def test_a_window_can_carry_its_source(self):
        window = quota_window("five_hour", "5h", 12, NOW + DAY, NOW, 300,
                               source="usage cache")
        self.assertEqual(window["source"], "usage cache")

    def test_a_bad_source_label_is_refused(self):
        for source in ("", "x" * 40, "café", 5):
            with self.subTest(source=source), self.assertRaises(ValueError):
                quota_window("five_hour", "5h", 12, NOW + DAY, NOW, 300, source=source)


class CardTests(SourceBase):
    """The detail card names the source and shows the credit."""

    def report(self, source="usage cache", credit=True, observed=NOW - timedelta(hours=2)):
        report = quota_report("anthropic", "Claude", [
            quota_window("five_hour", "5h", 12, NOW + timedelta(hours=4), observed, 300,
                         source=source),
            quota_window("seven_day", "Weekly", 34, NOW + timedelta(days=4), observed,
                         10080, source=source)])
        report["collector_seen_at"] = observed.isoformat()
        report["collector_host"] = "laptop"
        report["collector_version"] = "2.1"
        report["last_progress_at"] = observed.isoformat()
        report["usage_cache"] = {"available": True, "reason": None, "message": None,
                                 "path": "/home/you/.claude.json",
                                 "fetched_at": observed.isoformat(),
                                 "credit": ({"limit": 100, "used": 0, "currency": "USD",
                                             "resets_at": (NOW + 16 * DAY).isoformat()}
                                            if credit else None),
                                 "account_changed": False}
        return report

    def card(self, report):
        snapshot = SourceSnapshot("c", "ok", NOW, {"quota": report})
        with patch("presto_ai_dash.dashboard.now_utc", return_value=NOW):
            return _account_card({"id": "c", "name": "Claude", "type": "claude"},
                                 snapshot, NOW)

    def test_the_quota_line_names_the_source_and_the_age(self):
        detail = self.card(self.report(source="usage cache")).detail
        self.assertIn("Quota via cache, 2 h old", detail)

    def test_the_terminal_export_is_named_too(self):
        detail = self.card(self.report(source="terminal export", credit=False)).detail
        self.assertIn("Quota via export, 2 h old", detail)

    def test_the_credit_line_is_shown(self):
        detail = self.card(self.report()).detail
        self.assertIn("Credit: $100 of $100 (exp 11-05)", detail)

    def test_without_a_credit_there_is_no_credit_line(self):
        detail = self.card(self.report(credit=False)).detail
        self.assertFalse(any(line.startswith("Credit") for line in detail), detail)

    def test_every_line_stays_inside_the_detail_budget(self):
        preview_dir = Path(__file__).resolve().parents[2] / "tools" / "presto_preview"
        sys.path.insert(0, str(preview_dir))
        from framebuffer import Framebuffer
        from presto_ai_dash.dashboard import DETAIL_LINE_CHARS
        display = Framebuffer()
        display.set_font("bitmap6")
        available = display.width - (8 + 6) - 8
        for line in self.card(self.report()).detail:
            with self.subTest(line=line):
                self.assertLessEqual(len(line), DETAIL_LINE_CHARS, line)
                self.assertLessEqual(display.measure_text(line, 1), available, line)
                self.assertTrue(line.isascii(), line)

    def test_the_presto_payload_tolerates_the_source_label(self):
        report = self.report()
        snapshot = SourceSnapshot("c", "ok", NOW, {"quota": report})
        payload = build_instance_payload({"c": snapshot},
                                         [{"id": "c", "type": "claude", "name": "Claude",
                                           "enabled": True}])
        source = payload["sources"][0]
        self.assertEqual(source["quota"]["windows"][0]["source"], "usage cache")
        self.assertEqual(source["quota"]["usage_cache"]["credit"]["limit"], 100)


class AdminPageTests(ManagementBase):
    """The admin page explains the cache, the credit and the window source."""

    def setUp(self):
        super().setUp()
        self.disable_fake()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.row = self.manager.save({"type": "claude", "name": "Claude personal",
                                      "enabled": True,
                                      "config": {"settings_path": str(self.root / "settings.json"),
                                                 "usage_cache": True}})
        self.report = quota_report("anthropic", "Claude", [
            quota_window("five_hour", "5h", 12, NOW + timedelta(hours=4),
                         NOW - timedelta(hours=2), 300, source="usage cache"),
            quota_window("seven_day", "Weekly", 34, NOW + timedelta(days=4),
                         NOW - timedelta(hours=2), 10080, source="terminal export")])
        self.report["collector_seen_at"] = (NOW - timedelta(hours=2)).isoformat()
        self.report["collector_host"] = "laptop"
        self.report["collector_version"] = "2.1"
        self.report["last_progress_at"] = (NOW - timedelta(hours=2)).isoformat()
        self.report["usage_cache"] = {
            "available": True, "reason": None, "message": None,
            "path": "/home/you/.claude.json",
            "fetched_at": (NOW - timedelta(hours=2)).isoformat(),
            "credit": {"limit": 100, "used": 0, "currency": "USD",
                       "resets_at": (NOW + 16 * DAY).isoformat()},
            "account_changed": False}
        self.report["hint"] = "No report 2h - run claude"

    def entry(self):
        self.storage.save_snapshot(SourceSnapshot(self.row["id"], "ok", NOW,
                                                  metrics={"quota": self.report}))
        with patch("presto_ai_dash.quotas.now_utc", return_value=NOW):
            listing = self.manager.list_sources()
        return next(row for row in listing["sources"] if row["id"] == self.row["id"])

    def test_the_cache_line_names_the_reading_the_age_and_the_path(self):
        lines = self.entry()["provenance"]
        self.assertTrue(lines["usage_cache"].startswith("Usage cache: ok - fetched "),
                        lines["usage_cache"])
        self.assertIn("(2 h old)", lines["usage_cache"])
        self.assertIn("/home/you/.claude.json", lines["usage_cache"])

    def test_the_credit_line_is_shown(self):
        lines = self.entry()["provenance"]
        self.assertIn("Credit: $100 of $100 left", lines["credit"])
        self.assertIn("expires", lines["credit"])

    def test_the_quota_line_names_the_source_of_the_newest_reading(self):
        lines = self.entry()["provenance"]
        self.assertTrue(lines["quota"].startswith("Quota: usage cache - "), lines["quota"])

    def test_the_status_note_says_how_to_refresh_the_cache(self):
        lines = self.entry()["provenance"]
        self.assertIn("opening /usage in Claude Code refreshes the cache",
                      lines["status_note"])

    def test_every_window_carries_its_source_in_the_listing(self):
        entry = self.entry()
        self.assertEqual([window["source"] for window in entry["quota"]["windows"]],
                         ["usage cache", "terminal export"])

    def test_the_reader_off_line_says_where_to_turn_it_on(self):
        row = next(row for row in self.manager.list_sources()["sources"]
                   if row["id"] == self.row["id"])
        self.manager.save({"revision": row["revision"],
                           "config": dict(row["config"], usage_cache=False)}, row["id"])
        lines = self.entry()["provenance"]
        self.assertEqual(lines["usage_cache"],
                         "Usage cache: off (tick 'Read Claude Code's local usage cache' "
                         "in Edit)")

    def test_an_unavailable_cache_line_says_why(self):
        report = dict(self.report)
        report["usage_cache"] = {"available": False,
                                 "reason": "no usage cache found",
                                 "message": "no Claude Code usage cache found; it is "
                                            "created when Claude Code first fetches usage",
                                 "path": None, "fetched_at": None, "credit": None,
                                 "account_changed": False}
        self.storage.save_snapshot(SourceSnapshot(self.row["id"], "ok", NOW,
                                                  metrics={"quota": report}))
        with patch("presto_ai_dash.quotas.now_utc", return_value=NOW):
            listing = self.manager.list_sources()
        entry = next(row for row in listing["sources"] if row["id"] == self.row["id"])
        self.assertTrue(entry["provenance"]["usage_cache"].startswith(
            "Usage cache: UNAVAILABLE - "), entry["provenance"]["usage_cache"])

    def test_the_page_script_shows_the_new_lines(self):
        source = Path(__file__).resolve().parents[1] / "src" / "presto_ai_dash" / "web" / "admin.js"
        script = source.read_text(encoding="utf-8")
        self.assertIn('"usage_cache"', script)
        self.assertIn('"credit"', script)
        self.assertIn("quota.source", script)


if __name__ == "__main__":
    unittest.main()
