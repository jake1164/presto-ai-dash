"""The percent -> status mapping: when a quota window raises an alert.

The daemon decides the user-facing status; the device only draws it. These
tests cover the boundaries, the rules that keep an alert honest (a fresh
available window only, never a window that is about to reset) and the
worst-window rule.

Synthetic fixtures only: no provider is contacted and no real reading is
read.
"""

from datetime import datetime, timedelta, timezone
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "..", "src"))

from envutil import clean_env
from presto_ai_dash.alert_policy import (DEFAULT_CRITICAL_PERCENT, DEFAULT_WARNING_PERCENT,
                                   RESET_GRACE_SECONDS, apply_threshold, is_quiet,
                                   level_for_percent, parse_clock, threshold_status,
                                   validate_thresholds)
from presto_ai_dash.dashboard import build_instance_payload
from presto_ai_dash.models import SourceSnapshot
from presto_ai_dash.quotas import quota_report, quota_window

NOW = datetime(2026, 10, 20, 12, tzinfo=timezone.utc)
HOUR = timedelta(hours=1)
DAY = timedelta(days=1)


def report(*specs, stale_after=900):
    """A quota report with one window per (label, percent, reset, state)."""
    windows = []
    for label, percent, reset, state in specs:
        windows.append(quota_window(label.lower(), label, percent, reset,
                                    NOW - timedelta(seconds=30), 300, state=state))
    return quota_report("test", "Test", windows, stale_after)


def aged(quota, now=NOW):
    """The report as the payload builder sees it: aged at read time."""
    with patch("presto_ai_dash.quotas.now_utc", return_value=now):
        from presto_ai_dash.quotas import present_quota
        return present_quota(quota, now)


def level(quota, now=NOW, warning=DEFAULT_WARNING_PERCENT,
          critical=DEFAULT_CRITICAL_PERCENT):
    """The threshold status of a report, judged at a fixed moment."""
    return threshold_status(quota, warning, critical, now)


class ThresholdBoundariesTests(unittest.TestCase):
    def test_79_percent_is_below_the_warning(self):
        self.assertIsNone(level_for_percent(79))
        self.assertEqual(level_for_percent(80), "near_limit")

    def test_94_percent_is_a_warning_and_95_is_critical(self):
        self.assertEqual(level_for_percent(94), "near_limit")
        self.assertEqual(level_for_percent(95), "critical")

    def test_99_percent_is_critical_and_100_is_rate_limited(self):
        self.assertEqual(level_for_percent(99), "critical")
        self.assertEqual(level_for_percent(100), "rate_limited")

    def test_the_defaults_are_80_and_95(self):
        self.assertEqual(DEFAULT_WARNING_PERCENT, 80)
        self.assertEqual(DEFAULT_CRITICAL_PERCENT, 95)

    def test_the_thresholds_can_be_moved(self):
        self.assertIsNone(level_for_percent(69, 70, 90))
        self.assertEqual(level_for_percent(70, 70, 90), "near_limit")
        self.assertEqual(level_for_percent(90, 70, 90), "critical")

    def test_a_junk_percentage_never_alerts(self):
        for value in (None, "80", True, [80]):
            self.assertIsNone(level_for_percent(value))


class ThresholdValidationTests(unittest.TestCase):
    def test_the_warning_must_be_below_the_critical(self):
        with self.assertRaises(ValueError):
            validate_thresholds(95, 95)
        with self.assertRaises(ValueError):
            validate_thresholds(96, 95)

    def test_both_must_be_whole_numbers_in_range(self):
        for value in (0, 101, 80.0, "80", True, None):
            with self.assertRaises(ValueError):
                validate_thresholds(value, 95)
            with self.assertRaises(ValueError):
                validate_thresholds(80, value)

    def test_a_valid_pair_passes(self):
        self.assertEqual(validate_thresholds(80, 95), (80, 95))


class ThresholdStatusTests(unittest.TestCase):
    def test_a_fresh_available_window_raises_the_warning(self):
        quota = report(("5h", 85, NOW + 3 * HOUR, "available"))
        self.assertEqual(level(aged(quota)), "near_limit")

    def test_a_window_below_the_warning_says_nothing(self):
        quota = report(("5h", 10, NOW + 3 * HOUR, "available"))
        self.assertIsNone(level(aged(quota)))

    def test_a_window_that_resets_within_thirty_minutes_does_not_alert(self):
        quota = report(("5h", 99, NOW + timedelta(minutes=29), "available"))
        self.assertIsNone(level(aged(quota)))

    def test_a_window_that_resets_after_thirty_minutes_alerts(self):
        quota = report(("5h", 99, NOW + timedelta(minutes=31), "available"))
        self.assertEqual(level(aged(quota)), "critical")

    def test_the_grace_period_is_thirty_minutes(self):
        self.assertEqual(RESET_GRACE_SECONDS, 30 * 60)

    def test_a_stale_window_never_alerts(self):
        quota = report(("5h", 99, NOW + 3 * HOUR, "available"))
        stale = aged(quota, now=NOW + timedelta(hours=2))
        self.assertEqual(stale["windows"][0]["state"], "stale")
        self.assertIsNone(level(stale))

    def test_an_unavailable_window_never_alerts(self):
        quota = report(("5h", None, None, "unavailable"))
        self.assertIsNone(level(aged(quota)))

    def test_a_window_whose_reset_has_passed_never_alerts(self):
        quota = report(("5h", 99, NOW - HOUR, "available"))
        passed = aged(quota)
        self.assertEqual(passed["windows"][0]["state"], "stale")
        self.assertIsNone(level(passed))

    def test_the_worst_window_decides(self):
        quota = report(("5h", 81, NOW + 3 * HOUR, "available"),
                       ("weekly", 96, NOW + 5 * DAY, "available"))
        self.assertEqual(level(aged(quota)), "critical")

    def test_a_stale_window_never_hides_a_fresh_one(self):
        quota = report(("5h", 99, NOW - HOUR, "available"),
                       ("weekly", 82, NOW + 5 * DAY, "available"))
        aged_report = aged(quota)
        self.assertEqual(level(aged_report), "near_limit")

    def test_a_report_without_windows_says_nothing(self):
        self.assertIsNone(threshold_status({}))
        self.assertIsNone(threshold_status({"windows": "no"}))
        self.assertIsNone(threshold_status(None))

    def test_the_worse_of_two_statuses_wins(self):
        self.assertEqual(apply_threshold("ok", "near_limit"), "near_limit")
        self.assertEqual(apply_threshold("active", "critical"), "critical")
        self.assertEqual(apply_threshold("critical", "near_limit"), "critical")
        self.assertEqual(apply_threshold("ok", None), "ok")


class PayloadStatusTests(unittest.TestCase):
    """The mapping reaches the payload: the card and the source row."""

    def build(self, quota, alerts=None, now=NOW):
        row = {"id": "acc", "type": "codex", "name": "Account", "enabled": True}
        snapshot = SourceSnapshot("acc", "ok", NOW, {"quota": quota})
        with patch("presto_ai_dash.quotas.now_utc", return_value=now), \
                patch("presto_ai_dash.dashboard.now_utc", return_value=now):
            return build_instance_payload({"acc": snapshot}, [row], alerts, now)

    def test_a_near_full_window_makes_the_source_near_limit(self):
        quota = report(("5h", 85, NOW + 3 * HOUR, "available"))
        payload = self.build(quota)
        self.assertEqual(payload["sources"][0]["status"], "near_limit")
        self.assertEqual(payload["cards"][1]["status"], "near_limit")

    def test_a_critical_window_makes_the_source_critical(self):
        quota = report(("5h", 96, NOW + 3 * HOUR, "available"))
        payload = self.build(quota)
        self.assertEqual(payload["sources"][0]["status"], "critical")

    def test_a_full_window_stays_rate_limited(self):
        quota = report(("5h", 100, NOW + 3 * HOUR, "available"))
        payload = self.build(quota)
        self.assertEqual(payload["sources"][0]["status"], "rate_limited")

    def test_the_overall_status_follows_the_worst_source(self):
        quota = report(("5h", 85, NOW + 3 * HOUR, "available"))
        payload = self.build(quota)
        self.assertEqual(payload["overall_status"], "near_limit")

    def test_a_stale_window_leaves_the_status_alone(self):
        quota = report(("5h", 85, NOW + 3 * HOUR, "available"))
        payload = self.build(quota, now=NOW + timedelta(hours=2))
        self.assertNotEqual(payload["sources"][0]["status"], "near_limit")

    def test_the_thresholds_are_the_ones_the_settings_say(self):
        quota = report(("5h", 85, NOW + 3 * HOUR, "available"))
        payload = self.build(quota, {"warning_percent": 90, "critical_percent": 95})
        self.assertEqual(payload["sources"][0]["status"], "ok")


class QuietHoursTests(unittest.TestCase):
    def test_a_clock_string_is_parsed(self):
        self.assertEqual(parse_clock("22:00"), 22 * 60)
        self.assertEqual(parse_clock("07:05"), 7 * 60 + 5)
        for value in ("24:00", "7", "aa:bb", None, 700):
            self.assertIsNone(parse_clock(value))

    def test_a_window_that_wraps_around_midnight(self):
        start, end = "22:00", "07:00"
        self.assertTrue(is_quiet(datetime(2026, 10, 20, 23, 0), start, end))
        self.assertTrue(is_quiet(datetime(2026, 10, 20, 6, 59), start, end))
        self.assertFalse(is_quiet(datetime(2026, 10, 20, 7, 0), start, end))
        self.assertFalse(is_quiet(datetime(2026, 10, 20, 12, 0), start, end))

    def test_a_window_inside_one_day(self):
        self.assertTrue(is_quiet(datetime(2026, 10, 20, 13, 0), "12:00", "14:00"))
        self.assertFalse(is_quiet(datetime(2026, 10, 20, 15, 0), "12:00", "14:00"))

    def test_an_unusable_window_is_never_quiet(self):
        self.assertFalse(is_quiet(datetime(2026, 10, 20, 23, 0), "nonsense", "07:00"))

    def test_the_quiet_hours_follow_the_time_zone_variable(self):
        # A container runs in UTC unless it is told otherwise, and the quiet
        # hours are the local time of the machine the daemon runs on.
        moment = datetime(2026, 10, 20, 21, 0, tzinfo=timezone.utc)
        with clean_env(TZ="UTC+05:30"):
            # 21:00 UTC is 02:30 the next day there: inside the quiet window.
            self.assertTrue(is_quiet(moment, "22:00", "07:00"))
        with clean_env(TZ="UTC"):
            self.assertFalse(is_quiet(moment, "22:00", "07:00"))


if __name__ == "__main__":
    unittest.main()
