"""Two computers reporting one account: the newest reading of each window wins.

A reading's age is when Claude produced it, not when the report reached the
daemon, and a heartbeat or a late arrival never makes a quota look newer. The
sender's clock is only believed inside a bounded skew of the daemon's own.
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from presto_ai_dash.ingest import SCHEMA_NAME, ingest
from admin.test_management import ManagementBase
from ingest.test_pushed_readings import usage_cache

T0 = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(hours=1)
RESET_A = T0 + timedelta(hours=3)          # the 5h window in force at T0
RESET_B = T0 + timedelta(hours=8)          # the 5h window after it resets
WEEK_1 = T0 + timedelta(days=3)
WEEK_2 = T0 + timedelta(days=10)


def at(minutes=0, seconds=0):
    return T0 + timedelta(minutes=minutes, seconds=seconds)


def report(host, sent, five, reset5, seven=20.0, reset7=WEEK_1, tag="a", **extra):
    """A status-line style report with both windows, as one computer would send it."""
    def window(key, label, minutes, percent, reset):
        return {"id": key, "label": label, "period_minutes": minutes, "used_percent": percent,
                "resets_at": reset.isoformat(), "state": "available"}
    body = {"schema": SCHEMA_NAME,
            "collector": {"name": "claude-statusline", "version": "2", "host": host},
            "client_time": sent.isoformat(),
            "windows": [window("five_hour", "5h", 300, five, reset5),
                        window("seven_day", "Weekly", 10080, seven, reset7)],
            "evidence": {"five_hour": (tag + "5").ljust(64, "0"),
                         "seven_day": (tag + "7").ljust(64, "0")},
            "progress": tag.ljust(64, "0")}
    body.update(extra)
    return body


def heartbeat(host, sent):
    return {"schema": SCHEMA_NAME,
            "collector": {"name": "claude-statusline", "version": "2", "host": host},
            "client_time": sent.isoformat()}


class MergeBase(ManagementBase):
    def setUp(self):
        super().setUp()
        self.row = self.manager.save({"type": "claude", "name": "Claude", "enabled": True,
                                      "config": {"settings_path": str(Path(self.temp.name) / "s.json")}})
        self.id = self.row["id"]

    def push(self, body, received):
        return ingest(self.storage, self.id, body, now=received)

    def windows(self):
        stored = self.storage.observation(self.id)["report"]["windows"]
        return {window["id"]: window for window in stored}

    def five(self):
        return self.windows()["five_hour"]


class OutOfOrderTests(MergeBase):
    def test_a_delayed_older_report_does_not_replace_a_newer_one(self):
        self.push(report("mac", at(5), 55.0, RESET_A, tag="b"), at(5, 1))
        self.push(report("windows", at(0), 40.0, RESET_A, tag="a"), at(9))
        self.assertEqual(self.five()["used_percent"], 55.0)
        self.assertEqual(datetime.fromisoformat(self.five()["observed_at"]), at(5))

    def test_reports_in_order_still_update(self):
        self.push(report("windows", at(0), 40.0, RESET_A, tag="a"), at(0, 1))
        self.push(report("mac", at(5), 55.0, RESET_A, tag="b"), at(5, 1))
        self.assertEqual(self.five()["used_percent"], 55.0)

    def test_a_delayed_report_still_ages_by_when_it_was_made(self):
        self.push(report("windows", at(0), 40.0, RESET_A), at(120))   # two hours late
        self.assertEqual(datetime.fromisoformat(self.five()["observed_at"]), at(0))

    def test_a_replayed_report_changes_nothing(self):
        self.push(report("mac", at(5), 55.0, RESET_A, tag="b"), at(5, 1))
        self.push(report("windows", at(0), 40.0, RESET_A, tag="a"), at(9))
        self.push(report("windows", at(0), 40.0, RESET_A, tag="a"), at(30))
        self.assertEqual(self.five()["used_percent"], 55.0)
        self.assertEqual(datetime.fromisoformat(self.five()["observed_at"]), at(5))


class ResetBoundaryTests(MergeBase):
    def test_a_report_for_the_old_window_cannot_replace_the_new_one(self):
        self.push(report("mac", at(181), 3.0, RESET_B, tag="c"), at(181, 1))
        self.push(report("windows", at(179), 90.0, RESET_A, tag="d"), at(182))
        self.assertEqual(self.five()["used_percent"], 3.0)
        self.assertEqual(datetime.fromisoformat(self.five()["resets_at"]), RESET_B)

    def test_the_new_window_replaces_the_old_one(self):
        self.push(report("windows", at(179), 90.0, RESET_A, tag="d"), at(179, 1))
        self.push(report("mac", at(181), 3.0, RESET_B, tag="c"), at(181, 1))
        self.assertEqual(self.five()["used_percent"], 3.0)

    def test_a_reset_time_that_drifts_a_second_is_the_same_window(self):
        self.push(report("mac", at(5), 55.0, RESET_A, tag="b"), at(5, 1))
        self.push(report("windows", at(6), 56.0, RESET_A + timedelta(seconds=1), tag="e"),
                  at(6, 1))
        self.assertEqual(self.five()["used_percent"], 56.0)


class WindowsAreIndependentTests(MergeBase):
    def test_each_window_keeps_its_own_newest_reading(self):
        # The first computer already knows the new 5h window; the second still
        # sees the old one but has a newer weekly reading.
        self.push(report("mac", at(10), 50.0, RESET_B, seven=20.0, tag="a"), at(10, 1))
        self.push(report("windows", at(12), 10.0, RESET_A, seven=25.0, tag="b"), at(12, 1))
        windows = self.windows()
        self.assertEqual(windows["five_hour"]["used_percent"], 50.0)
        self.assertEqual(windows["seven_day"]["used_percent"], 25.0)
        self.assertEqual(datetime.fromisoformat(windows["five_hour"]["observed_at"]), at(10))
        self.assertEqual(datetime.fromisoformat(windows["seven_day"]["observed_at"]), at(12))


class PlaceholderWindowTests(MergeBase):
    def test_a_cache_only_report_does_not_block_an_earlier_made_reading(self):
        # The cache report creates empty windows stamped with its receive time;
        # a status-line reading made just before it, arriving after, must win.
        body = heartbeat("mac", at(10))
        body["usage_cache"] = usage_cache(now=at(10), fetched_at=at(0).isoformat())
        self.push(body, at(10, 1))
        self.assertIsNone(self.five()["used_percent"])
        self.push(report("windows", at(8), 55.0, RESET_A), at(10, 2))
        self.assertEqual(self.five()["used_percent"], 55.0)
        self.assertEqual(datetime.fromisoformat(self.five()["observed_at"]), at(8))

    def test_an_older_report_without_a_measurement_does_not_wipe_a_reading(self):
        self.push(report("mac", at(10), 55.0, RESET_A), at(10, 1))
        empty = report("windows", at(5), 0.0, RESET_A)
        for window in empty["windows"]:
            window.update(used_percent=None, resets_at=None, state="unavailable")
        self.push(empty, at(11))
        self.assertEqual(self.five()["used_percent"], 55.0)


class HeartbeatTests(MergeBase):
    def test_a_heartbeat_never_refreshes_the_quota_age(self):
        self.push(report("mac", at(5), 55.0, RESET_A), at(5, 1))
        self.push(heartbeat("windows", at(30)), at(30))
        self.assertEqual(datetime.fromisoformat(self.five()["observed_at"]), at(5))
        seen = self.storage.observation(self.id)["collector_seen_at"]
        self.assertEqual(datetime.fromisoformat(seen), at(30))


class UntrustedClockTests(MergeBase):
    def test_a_sender_clock_far_ahead_falls_back_to_the_receive_time(self):
        self.push(report("mac", at(0) + timedelta(days=3), 55.0, RESET_A), at(1))
        self.assertEqual(datetime.fromisoformat(self.five()["observed_at"]), at(1))

    def test_a_sender_clock_an_hour_ahead_falls_back_to_the_receive_time(self):
        self.push(report("mac", at(60), 55.0, RESET_A), at(1))
        self.assertEqual(datetime.fromisoformat(self.five()["observed_at"]), at(1))

    def test_a_sender_clock_a_little_ahead_is_capped_at_the_receive_time(self):
        self.push(report("mac", at(1, 30), 55.0, RESET_A), at(1))
        self.assertLessEqual(datetime.fromisoformat(self.five()["observed_at"]), at(1))

    def test_a_sender_clock_weeks_behind_falls_back_to_the_receive_time(self):
        self.push(report("mac", at(0) - timedelta(days=30), 55.0, RESET_A), at(1))
        self.assertEqual(datetime.fromisoformat(self.five()["observed_at"]), at(1))

    def test_an_unreadable_sender_time_falls_back_to_the_receive_time(self):
        body = report("mac", at(0), 55.0, RESET_A)
        del body["client_time"]
        self.push(body, at(1))
        self.assertEqual(datetime.fromisoformat(self.five()["observed_at"]), at(1))


class RedrawTests(MergeBase):
    def test_the_same_evidence_keeps_the_original_age(self):
        self.push(report("mac", at(5), 55.0, RESET_A, tag="b"), at(5, 1))
        self.push(report("mac", at(20), 55.0, RESET_A, tag="b"), at(20, 1))
        self.assertEqual(datetime.fromisoformat(self.five()["observed_at"]), at(5))


class ProgressTests(MergeBase):
    def test_a_delayed_report_does_not_move_progress_backwards(self):
        self.push(report("mac", at(10), 55.0, RESET_A, tag="b"), at(10, 1))
        self.push(report("windows", at(2), 41.0, RESET_A, tag="a"), at(11))
        stored = self.storage.observation(self.id)
        self.assertEqual(datetime.fromisoformat(stored["last_progress_at"]), at(10))
        self.assertEqual(stored["progress"], "b".ljust(64, "0"))


class PushedUsageCacheTests(MergeBase):
    def cache(self, fetched, percent):
        return usage_cache(now=fetched + timedelta(minutes=20), fetched_at=fetched.isoformat(),
                           windows=[{"id": "five_hour", "label": "5h", "period_minutes": 300,
                                     "used_percent": percent, "resets_at": RESET_A.isoformat(),
                                     "state": "available"}])

    def stored_cache(self):
        return self.storage.observation(self.id)["report"]["pushed_usage_cache"]

    def test_an_older_cache_from_another_computer_does_not_replace_a_newer_one(self):
        self.push(report("mac", at(30), 55.0, RESET_A, usage_cache=self.cache(at(25), 54.0)),
                  at(30, 1))
        self.push(report("windows", at(32), 56.0, RESET_A, tag="b",
                         usage_cache=self.cache(at(0), 30.0)), at(32, 1))
        self.assertEqual(self.stored_cache()["windows"][0]["used_percent"], 54.0)

    def test_an_older_cache_alone_does_not_replace_a_newer_one(self):
        self.push(report("mac", at(30), 55.0, RESET_A, usage_cache=self.cache(at(25), 54.0)),
                  at(30, 1))
        body = heartbeat("windows", at(33))
        body["usage_cache"] = self.cache(at(0), 30.0)
        self.push(body, at(33, 1))
        self.assertEqual(self.stored_cache()["windows"][0]["used_percent"], 54.0)

    def test_a_newer_cache_replaces_an_older_one(self):
        self.push(report("mac", at(30), 55.0, RESET_A, usage_cache=self.cache(at(0), 30.0)),
                  at(30, 1))
        self.push(report("windows", at(32), 56.0, RESET_A, tag="b",
                         usage_cache=self.cache(at(25), 54.0)), at(32, 1))
        self.assertEqual(self.stored_cache()["windows"][0]["used_percent"], 54.0)


if __name__ == "__main__":
    unittest.main()
