"""The Claude usage cache has its own stale threshold.

The cache's windows used to be judged by the source's `stale_after_seconds`
(900 by default), which is meant for the terminal export. The cache is only
refreshed when /usage is opened in Claude Code, so that threshold called good
readings stale too early. A Claude account now sets the cache threshold on its
own; windows from the terminal export keep the export's threshold, and a
merged report judges every window by the source it came from.

All fixtures are synthetic and live in a temp directory; no test reads
~/.claude.json.
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path
import json
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from presto_ai_dash.management import validate_settings
from presto_ai_dash.quotas import (claude_hint, present_quota, quota_report,
                                    quota_window)
from presto_ai_dash.sources.claude import ClaudeSource
from providers.test_claude_usage_cache import cache_document

NOW = datetime(2026, 10, 20, 8, tzinfo=timezone.utc)
DAY = timedelta(days=1)
THREE_THOUSAND = timedelta(seconds=3000)


def window(window_id, label, percent, observed, reset, period, source):
    """One measured window that says where its reading came from."""
    return quota_window(window_id, label, percent, reset, observed, period,
                        source=source)


def report(cache_stale=None, cache_age=THREE_THOUSAND, export_age=THREE_THOUSAND):
    """A Claude report with one window from the cache and one from the export."""
    windows = []
    if export_age is not None:
        windows.append(window("five_hour", "5h", 12.0, NOW - export_age,
                              NOW + 4 * timedelta(hours=1), 300, "terminal export"))
    if cache_age is not None:
        windows.append(window("seven_day", "Weekly", 66.0, NOW - cache_age,
                              NOW + 4 * DAY, 10080, "usage cache"))
    result = quota_report("anthropic", "Claude", windows, stale_after_seconds=900)
    result["collector_seen_at"] = (NOW - DAY).isoformat()
    result["collector_host"] = "laptop"
    result["collector_version"] = "2.1"
    result["last_progress_at"] = (NOW - DAY).isoformat()
    if cache_age is not None:
        result["usage_cache"] = {"available": True, "reason": None, "message": None,
                                 "path": "/home/you/.claude.json",
                                 "fetched_at": (NOW - cache_age).isoformat(),
                                 "credit": None, "account_changed": False}
    if cache_stale is not None:
        result["usage_cache_stale_seconds"] = cache_stale
    return result


def states(presented, window_id):
    return next(w["state"] for w in presented["windows"] if w["id"] == window_id)


class CacheWindowStateTests(unittest.TestCase):
    """A cache window is judged by the cache threshold, not the export's."""

    def test_a_cache_window_3000_seconds_old_is_fresh_at_the_default(self):
        presented = present_quota(report(cache_stale=7200), NOW)
        self.assertEqual(states(presented, "seven_day"), "available")

    def test_a_cache_window_3000_seconds_old_is_stale_when_the_setting_is_300(self):
        presented = present_quota(report(cache_stale=300), NOW)
        self.assertEqual(states(presented, "seven_day"), "stale")

    def test_an_export_window_3000_seconds_old_is_stale_at_900(self):
        for cache_stale in (7200, 300, None):
            with self.subTest(cache_stale=cache_stale):
                presented = present_quota(report(cache_stale=cache_stale), NOW)
                self.assertEqual(states(presented, "five_hour"), "stale")

    def test_a_fresh_export_window_stays_fresh(self):
        presented = present_quota(report(cache_stale=300,
                                         export_age=timedelta(seconds=100)), NOW)
        self.assertEqual(states(presented, "five_hour"), "available")


class MergedReportTests(unittest.TestCase):
    """A merged report judges each window by its own source."""

    def test_the_cache_window_is_fresh_and_the_export_window_is_stale(self):
        presented = present_quota(report(cache_stale=7200), NOW)
        self.assertEqual(states(presented, "seven_day"), "available")
        self.assertEqual(states(presented, "five_hour"), "stale")

    def test_both_windows_are_stale_with_a_small_cache_threshold(self):
        presented = present_quota(report(cache_stale=300), NOW)
        self.assertEqual(states(presented, "seven_day"), "stale")
        self.assertEqual(states(presented, "five_hour"), "stale")

    def test_another_provider_is_not_changed(self):
        """A report without a cache threshold behaves exactly as before."""
        other = quota_report("openai", "Codex", [
            quota_window("codex:primary", "Weekly", 2, NOW + 6 * DAY,
                         NOW - timedelta(seconds=100), 10080)], 180)
        self.assertEqual(states(present_quota(other, NOW), "codex:primary"),
                         "available")
        old = quota_report("openai", "Codex", [
            quota_window("codex:primary", "Weekly", 2, NOW + 6 * DAY,
                         NOW - timedelta(seconds=200), 10080)], 180)
        self.assertEqual(states(present_quota(old, NOW), "codex:primary"), "stale")

    def test_a_cache_window_without_a_threshold_keeps_the_export_threshold(self):
        """A report from before the setting existed behaves as it did."""
        presented = present_quota(report(cache_stale=None), NOW)
        self.assertEqual(states(presented, "seven_day"), "stale")


class HintTests(unittest.TestCase):
    """The hint agrees with the window states."""

    def test_a_fresh_cache_window_leaves_no_hint(self):
        self.assertIsNone(claude_hint(report(cache_stale=7200), NOW))

    def test_a_stale_cache_window_has_a_hint(self):
        self.assertIsNotNone(claude_hint(report(cache_stale=300), NOW))

    def test_a_stale_export_window_keeps_its_own_hint(self):
        """Without a cache reading the export's own rules apply."""
        hint = claude_hint(report(cache_age=None, cache_stale=7200), NOW)
        self.assertIsNotNone(hint)
        self.assertIn("claude", hint)

    def test_another_provider_keeps_its_hint_rules(self):
        plain = quota_report("anthropic", "Claude", [
            quota_window("five_hour", "5h", 12, NOW + 4 * timedelta(hours=1),
                         NOW - timedelta(seconds=100), 300)], 900)
        plain["collector_seen_at"] = (NOW - DAY).isoformat()
        self.assertIsNone(claude_hint(plain, NOW))


class ConfigFieldTests(unittest.TestCase):
    """The setting is declared on the Claude source."""

    def fields(self):
        return {field["key"]: field for field in ClaudeSource.config_fields}

    def test_the_field_is_declared_with_its_bounds(self):
        field = self.fields()["usage_cache_stale_seconds"]
        self.assertEqual(field["type"], "int")
        self.assertEqual(field["default"], 7200)
        self.assertEqual(field["minimum"], 300)
        self.assertEqual(field["maximum"], 604800)
        self.assertEqual(field["label"], "Mark usage cache readings stale after (seconds)")

    def test_the_field_comes_after_the_usage_cache_path(self):
        keys = [field["key"] for field in ClaudeSource.config_fields]
        self.assertEqual(keys.index("usage_cache_stale_seconds"),
                         keys.index("usage_cache_path") + 1)


class ValidationTests(unittest.TestCase):
    """The setting is validated like the other integer fields."""

    def settings(self, **values):
        base = {"settings_path": "/home/you/.claude/settings.json"}
        base.update(values)
        return base

    def test_the_accepted_values_are_the_bounds_and_the_default(self):
        for value in (300, 7200, 604800):
            with self.subTest(value=value):
                result = validate_settings(ClaudeSource,
                                           self.settings(usage_cache_stale_seconds=value))
                self.assertEqual(result["usage_cache_stale_seconds"], value)

    def test_a_missing_value_uses_the_default(self):
        result = validate_settings(ClaudeSource, self.settings())
        self.assertEqual(result["usage_cache_stale_seconds"], 7200)

    def test_a_value_out_of_range_is_refused(self):
        for value in (299, 0, -1, 604801):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    validate_settings(ClaudeSource,
                                      self.settings(usage_cache_stale_seconds=value))

    def test_a_value_that_is_not_an_integer_is_refused(self):
        for value in ("7200", 7200.5, True, None, [7200]):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    validate_settings(ClaudeSource,
                                      self.settings(usage_cache_stale_seconds=value))


class FakeStorage:
    """Just enough storage for one source: the stored terminal reading."""

    def __init__(self, observation=None):
        self.observation_row = observation

    def observation(self, source_id):
        return self.observation_row


class SourceBase(unittest.TestCase):
    """The source hands the threshold to the report."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / ".claude").mkdir()
        self.settings = self.root / ".claude" / "settings.json"
        self.settings.write_text("{}", encoding="utf-8")
        self.cache = self.root / ".claude.json"

    def write_cache(self, document=None):
        self.cache.write_text(json.dumps(document if document is not None
                                         else cache_document(fetched=NOW - THREE_THOUSAND)),
                               encoding="utf-8")
        return self.cache

    def observation(self, observed, percent=12.0, weekly=34.0):
        """A stored terminal-export reading, as the status line would push it."""
        stored = quota_report("anthropic", "Claude", [
            quota_window("five_hour", "5h", percent, NOW + 4 * timedelta(hours=1),
                         observed, 300),
            quota_window("seven_day", "Weekly", weekly, NOW + 4 * DAY, observed, 10080)])
        return {"report": stored, "evidence": {}, "collector_seen_at": observed.isoformat(),
                "collector_host": "laptop", "collector_version": None,
                "last_progress_at": observed.isoformat(), "progress": None,
                "last_activity_at": None}

    def source(self, observation=None, **config):
        settings = {"settings_path": str(self.settings), "stale_after_seconds": 900,
                    "usage_cache": True}
        settings.update(config)
        source = ClaudeSource(settings)
        source.id = "claude-1"
        source.attach_storage(FakeStorage(observation))
        return source

    def collect(self, source):
        with patch("presto_ai_dash.sources.claude.now_utc", return_value=NOW):
            return source.collect()

    def window(self, report, window_id):
        return next(w for w in report["windows"] if w["id"] == window_id)


class SourceReportTests(SourceBase):
    """The report carries the account's cache threshold."""

    def test_the_report_carries_the_configured_threshold(self):
        self.write_cache()
        report = self.collect(self.source(self.observation(NOW - 6 * DAY),
                                          usage_cache_stale_seconds=3000)).metrics["quota"]
        self.assertEqual(report["usage_cache_stale_seconds"], 3000)

    def test_a_missing_setting_uses_the_default(self):
        self.write_cache()
        report = self.collect(self.source(self.observation(NOW - 6 * DAY))).metrics["quota"]
        self.assertEqual(report["usage_cache_stale_seconds"], 7200)

    def test_the_export_keeps_its_own_threshold(self):
        self.write_cache()
        report = self.collect(self.source(self.observation(NOW - 6 * DAY),
                                          usage_cache_stale_seconds=300)).metrics["quota"]
        self.assertEqual(report["stale_after_seconds"], 900)


class SourceStateTests(SourceBase):
    """The source's own windows follow the rule."""

    def test_a_cache_window_3000_seconds_old_is_fresh_at_the_default(self):
        self.write_cache()
        snapshot = self.collect(self.source(self.observation(NOW - 6 * DAY)))
        presented = present_quota(snapshot.metrics["quota"], NOW)
        self.assertEqual(self.window(presented, "five_hour")["state"], "available")
        self.assertIsNone(presented["hint"])

    def test_a_cache_window_3000_seconds_old_is_stale_when_the_setting_is_300(self):
        self.write_cache()
        snapshot = self.collect(self.source(self.observation(NOW - 6 * DAY),
                                            usage_cache_stale_seconds=300))
        presented = present_quota(snapshot.metrics["quota"], NOW)
        self.assertEqual(self.window(presented, "five_hour")["state"], "stale")
        self.assertIsNotNone(presented["hint"])

    def test_an_export_window_3000_seconds_old_is_stale_at_900(self):
        # The cache reading is older, so the terminal export keeps the window.
        self.write_cache(cache_document(fetched=NOW - 6 * DAY))
        snapshot = self.collect(self.source(self.observation(NOW - THREE_THOUSAND),
                                            usage_cache_stale_seconds=7200))
        presented = present_quota(snapshot.metrics["quota"], NOW)
        self.assertEqual(self.window(presented, "five_hour")["source"],
                         "terminal export")
        self.assertEqual(self.window(presented, "five_hour")["state"], "stale")

    def test_a_merged_report_judges_each_window_by_its_own_source(self):
        # The cache reports only the 5h window, so the weekly one stays with
        # the terminal export.
        self.write_cache(cache_document(fetched=NOW - THREE_THOUSAND, weekly=None))
        snapshot = self.collect(self.source(self.observation(NOW - 6 * DAY)))
        presented = present_quota(snapshot.metrics["quota"], NOW)
        self.assertEqual(self.window(presented, "five_hour")["source"], "usage cache")
        self.assertEqual(self.window(presented, "five_hour")["state"], "available")
        self.assertEqual(self.window(presented, "seven_day")["source"],
                         "terminal export")
        self.assertEqual(self.window(presented, "seven_day")["state"], "stale")

    def test_a_merged_report_with_a_small_cache_threshold_ages_both(self):
        self.write_cache(cache_document(fetched=NOW - THREE_THOUSAND, weekly=None))
        snapshot = self.collect(self.source(self.observation(NOW - 6 * DAY),
                                            usage_cache_stale_seconds=300))
        presented = present_quota(snapshot.metrics["quota"], NOW)
        self.assertEqual(self.window(presented, "five_hour")["state"], "stale")
        self.assertEqual(self.window(presented, "seven_day")["state"], "stale")


if __name__ == "__main__":
    unittest.main()
