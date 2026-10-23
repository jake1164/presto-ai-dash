"""End to end: the Phase 2 collector script feeds the payload the Presto reads."""

import io
import json
import time
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.request import ProxyHandler, Request, build_opener

from presto_ai_dash.api import ApiServer
from presto_ai_dash.collectors import presto_collector
from presto_ai_dash.config import ingest_enabled
from presto_ai_dash.management import ManagedCollector

from admin.test_management import AdminServerBase

NOW = datetime.now(timezone.utc)


def status_input(percent=12.0, weekly=34.5):
    """One synthetic Claude status-line payload, shaped like the real export."""
    return json.dumps({
        "session_id": "private-session-identity", "cwd": "/work",
        "transcript_path": "/private/transcript", "version": "2.1.296",
        "cost": {"total_api_duration_ms": 421230},
        "rate_limits": {
            "five_hour": {"used_percentage": percent,
                          "resets_at": int((NOW + timedelta(hours=5)).timestamp())},
            "seven_day": {"used_percentage": weekly,
                          "resets_at": int((NOW + timedelta(days=7)).timestamp())}}})


class RoundTripTests(AdminServerBase):
    def setUp(self):
        super().setUp()
        self.disable_fake()
        self.row = self.manager.save({"type": "claude", "name": "Claude personal",
                                      "enabled": True,
                                      "config": {"settings_path": str(Path(self.temp.name) / "settings.json")}})
        self.status_api = ApiServer(
            "127.0.0.1", 0, self.storage, "device-token",
            status_provider=self.manager.status_payload,
            ingest_enabled=lambda: ingest_enabled(self.config,
                                                  self.storage.ingest_enabled_setting()))
        self.status_api.start()
        self.addCleanup(self.status_api.stop)
        self.ingest_url = ("http://127.0.0.1:%d/api/v1/ingest"
                           % self.status_api.actual_port)
        # The collector's module-level opener must not inherit any proxy settings
        # from the developer's environment; keep loopback traffic direct.
        self.addCleanup(setattr, presto_collector, "_OPENER", presto_collector._OPENER)
        presto_collector._OPENER = build_opener(
            ProxyHandler({}), presto_collector._NoRedirect())
        self.state_path = str(Path(self.temp.name) / "collector-state.json")
        self.token_file = Path(self.temp.name) / "claude.token"
        self.token = self.create_token()["token"]
        self.token_file.write_text(self.token, encoding="utf-8")
        self.assertEqual(self.request("/api/admin/ingest", "PUT", {"enabled": True})[0], 200)
        self.runtime = ManagedCollector(self.manager, self.config, threading.Event())

    def create_token(self):
        status, _, data = self.request(
            "/api/admin/sources/%s/ingest-token" % self.row["id"], "POST")
        self.assertEqual(status, 201)
        return data

    def run_collector(self, raw):
        stdin = io.TextIOWrapper(io.BytesIO(raw.encode("utf-8")))
        with patch("sys.stdin", stdin), patch("sys.stdout", io.StringIO()):
            result = presto_collector.main(
                ["claude-statusline", "--url", self.ingest_url, "--token-file", str(self.token_file),
                 "--host", "laptop"], state_path=self.state_path)
        self.assertEqual(result, 0)
        return result

    def device_status(self):
        request = Request("http://127.0.0.1:%d/api/v1/status"
                          % self.status_api.actual_port,
                          headers={"Authorization": "Bearer device-token"})
        with self.opener.open(request, timeout=3) as response:
            return json.loads(response.read())

    def observe(self):
        """One managed poll, then the payload the Presto device would download."""
        self.runtime.collect_once()
        payload = self.device_status()
        windows = next(row["quota"]["windows"] for row in payload["sources"]
                       if row["id"] == self.row["id"])
        return payload["cards"][-1], windows

    def push_and_collect(self, raw):
        self.run_collector(raw)
        return self.observe()

    def test_collector_script_updates_the_presto_payload(self):
        card, windows = self.push_and_collect(status_input())
        self.assertEqual(card["title"], "Claude personal")
        self.assertEqual(card["status"], "ok")
        self.assertEqual(card["primary"], "5h: 12%")
        self.assertEqual(card["secondary"], "Weekly: 34.5%")
        self.assertEqual(windows[0]["label"], "5h")  # the Presto reads labels, not window ids
        self.assertEqual(windows[0]["used_percent"], 12.0)
        self.assertEqual(windows[0]["state"], "available")
        self.assertIsNotNone(windows[0]["resets_at"])
        # A fresh reading answers no hint; it ages into one only when stale.
        self.assertIsNone(next(row for row in self.device_status()["sources"]
                               if row["id"] == self.row["id"])["quota"].get("hint"))
        self.assertFalse(card["detail"][0].endswith("unavailable"))
        # The admin page reports the collector trail; the token appears nowhere.
        _, _, listing = self.request("/api/admin/sources")
        entry = next(row for row in listing["sources"] if row["id"] == self.row["id"])
        self.assertEqual(entry["ingest"]["collector_host"], "laptop")
        self.assertIsNotNone(entry["ingest"]["collector_seen_at"])
        self.assertIsNotNone(entry["ingest"]["last_progress_at"])
        self.assertNotIn(self.token, json.dumps(listing))
        self.assertNotIn(self.token, json.dumps(self.device_status()))

    def test_heartbeat_reports_the_collector_without_touching_quota(self):
        _, before = self.push_and_collect(status_input())
        time.sleep(2.1)  # the ingest route throttles one token for two seconds
        self.run_collector(json.dumps({"version": "2.1.296"}))  # no rate_limits
        card, after = self.observe()
        self.assertEqual(before, after)  # a heartbeat never invents a reading
        self.assertEqual(card["status"], "ok")
        _, _, listing = self.request("/api/admin/sources")
        entry = next(row for row in listing["sources"] if row["id"] == self.row["id"])
        self.assertIsNotNone(entry["ingest"]["collector_seen_at"])
        self.assertIsNotNone(entry["ingest"]["last_progress_at"])

    def test_revoked_token_is_refused_and_the_collector_exits_zero(self):
        _, windows = self.push_and_collect(status_input())
        self.assertEqual(windows[0]["used_percent"], 12.0)
        status, _, result = self.request(
            "/api/admin/sources/%s/ingest-token" % self.row["id"], "DELETE")
        self.assertEqual(status, 200)
        self.assertEqual(result, {"revoked": True})
        self.run_collector(status_input(percent=90.0, weekly=66.0))
        _, windows = self.observe()
        self.assertEqual([window["used_percent"] for window in windows], [12.0, 34.5])
        _, _, listing = self.request("/api/admin/sources")
        entry = next(row for row in listing["sources"] if row["id"] == self.row["id"])
        self.assertFalse(entry["ingest"]["token_exists"])


if __name__ == "__main__":
    unittest.main()
