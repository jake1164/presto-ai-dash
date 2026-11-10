"""How old a Claude quota looks: the age of the reading, never of the report.

A heartbeat or a collector's file read proves a computer is alive, not that
Claude produced new quota, so neither may make a number look fresher. Each
source ages by its own threshold, and a stale number keeps its value.
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from presto_ai_dash.ingest import SCHEMA_NAME, ingest
from presto_ai_dash.quotas import present_quota
from presto_ai_dash.sources.claude import ClaudeSource
from admin.test_management import ManagementBase
from ingest.test_pushed_readings import usage_cache


def minutes_ago(minutes):
    return datetime.now(timezone.utc).replace(microsecond=0) - timedelta(minutes=minutes)


def status_line(host, made, percent=55.0):
    reset = made + timedelta(hours=3)
    return {"schema": SCHEMA_NAME,
            "collector": {"name": "claude-statusline", "version": "2", "host": host},
            "client_time": made.isoformat(),
            "windows": [{"id": "five_hour", "label": "5h", "period_minutes": 300,
                         "used_percent": percent, "resets_at": reset.isoformat(),
                         "state": "available"},
                        {"id": "seven_day", "label": "Weekly", "period_minutes": 10080,
                         "used_percent": 20.0, "resets_at": (made + timedelta(days=3)).isoformat(),
                         "state": "available"}],
            "evidence": {"five_hour": "a" * 64, "seven_day": "b" * 64},
            "progress": "c" * 64}


def heartbeat(host, sent, **extra):
    body = {"schema": SCHEMA_NAME,
            "collector": {"name": "claude-statusline", "version": "2", "host": host},
            "client_time": sent.isoformat()}
    body.update(extra)
    return body


class FreshnessBase(ManagementBase):
    def setUp(self):
        super().setUp()
        self.row = self.manager.save({"type": "claude", "name": "Claude", "enabled": True,
                                      "config": {"settings_path": str(Path(self.temp.name) / "s.json")}})
        self.id = self.row["id"]

    def push(self, body, received=None):
        return ingest(self.storage, self.id, body, now=received or datetime.now(timezone.utc))

    def shown(self):
        """The quota windows as the display layer sees them now, by id."""
        source = ClaudeSource({"settings_path": str(Path(self.temp.name) / "s.json")})
        source.id = self.id
        source.attach_storage(self.storage)
        report = present_quota(source.collect().metrics["quota"], datetime.now(timezone.utc))
        return {window["id"]: window for window in report["windows"]}


class HeartbeatTests(FreshnessBase):
    def test_a_heartbeat_from_another_computer_does_not_freshen_the_quota(self):
        self.push(status_line("windows", minutes_ago(20)), minutes_ago(20))
        self.push(heartbeat("mac", minutes_ago(0)))
        five = self.shown()["five_hour"]
        self.assertEqual(five["state"], "stale")
        self.assertEqual(five["used_percent"], 55.0)          # last known, still shown

    def test_a_status_line_reading_inside_its_threshold_is_current(self):
        self.push(status_line("windows", minutes_ago(10)), minutes_ago(10))
        self.assertEqual(self.shown()["five_hour"]["state"], "available")


class PerSourceThresholdTests(FreshnessBase):
    def push_cache(self, fetched_minutes_ago):
        fetched = minutes_ago(fetched_minutes_ago)
        self.push(heartbeat("mac", minutes_ago(0), usage_cache=usage_cache(
            now=datetime.now(timezone.utc), fetched_at=fetched.isoformat())))

    def test_a_usage_cache_reading_uses_the_cache_threshold(self):
        self.push_cache(20)              # older than the 15 min status-line limit, inside 30
        five = self.shown()["five_hour"]
        self.assertEqual(five["source"], "usage cache")
        self.assertEqual(five["state"], "available")

    def test_an_old_usage_cache_reading_is_stale_but_keeps_its_value(self):
        self.push_cache(49)              # the case from the feasibility test
        five = self.shown()["five_hour"]
        self.assertEqual(five["state"], "stale")
        self.assertEqual(five["used_percent"], 31.0)

    def test_the_cache_never_hides_a_fresher_status_line_reading(self):
        self.push_cache(49)
        self.push(status_line("windows", minutes_ago(2), percent=60.0), minutes_ago(2))
        five = self.shown()["five_hour"]
        self.assertEqual(five["source"], "terminal export")
        self.assertEqual(five["used_percent"], 60.0)
        self.assertEqual(five["state"], "available")


class LateReportTests(FreshnessBase):
    def test_a_late_report_ages_by_when_it_was_made(self):
        self.push(status_line("windows", minutes_ago(20)))      # sent 20 min ago, arrives now
        self.assertEqual(self.shown()["five_hour"]["state"], "stale")


if __name__ == "__main__":
    unittest.main()
