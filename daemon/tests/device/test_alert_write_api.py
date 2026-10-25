"""The device's one write: POST /api/v1/alerts/<source id>.

It sets one service's alerts on or off. It never toggles, it changes only
that one setting, and it is not a provider action: it touches the daemon's
own database and nothing else.

Synthetic fixtures only: a temporary database and a real API server.
"""

import json
import logging
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, build_opener, ProxyHandler

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from presto_ai_dash.api import ApiServer, _AlertRateLimiter
from admin.test_management import ManagementBase


class AlertWriteBase(ManagementBase):
    def setUp(self):
        super().setUp()
        self.row = self.manager.save({"type": "litellm", "name": "Gateway",
                                      "enabled": True,
                                      "config": {"base_url": "http://localhost:4000"}})
        self.other = self.manager.save({"type": "litellm", "name": "Second",
                                         "enabled": True,
                                         "config": {"base_url": "http://localhost:4001"}})
        self.limiter = _AlertRateLimiter()
        self.api = ApiServer("127.0.0.1", 0, self.storage, "device-token",
                             status_provider=self.manager.status_payload,
                             alert_writer=self.manager.set_service_alert_enabled,
                             alert_rate_limiter=self.limiter)
        self.api.start()
        self.addCleanup(self.api.stop)
        self.base_url = "http://127.0.0.1:%d" % self.api.actual_port
        self.opener = build_opener(ProxyHandler({}))
        self.path = "/api/v1/alerts/" + self.row["id"]

    def post(self, path=None, body=b'{"enabled": false}', token="device-token",
             headers=None, method="POST", content_type="application/json"):
        extra = {"Authorization": "Bearer " + token} if token is not None else {}
        if content_type:
            extra["Content-Type"] = content_type
        extra.update(headers or {})
        request = Request(self.base_url + (path or self.path), data=body,
                          headers=extra, method=method)
        try:
            response = self.opener.open(request, timeout=3)
        except HTTPError as exc:
            response = exc
        with response:
            data = response.read()
            try:
                payload = json.loads(data)
            except ValueError:
                payload = data
            return response.status, dict(response.headers), payload

    def status(self):
        request = Request(self.base_url + "/api/v1/status",
                          headers={"Authorization": "Bearer device-token"})
        with self.opener.open(request, timeout=3) as response:
            return json.loads(response.read())


class AuthenticationTests(AlertWriteBase):
    def test_the_shared_token_is_required(self):
        for token in (None, "", "wrong-token", "device-toke"):
            with self.subTest(token=token):
                status, headers, _ = self.post(token=token)
                self.assertEqual(status, 401)
                self.assertEqual(headers["WWW-Authenticate"], "Bearer")

    def test_an_ingest_token_does_not_work(self):
        """A push token is write-only for one account; it is not a device."""
        row = self.manager.save({"type": "claude", "name": "Claude personal",
                                  "enabled": True,
                                  "config": {"settings_path": str(Path(self.temp.name) / "settings.json")}})
        token = self.storage.create_ingest_token(row["id"])
        status, _, _ = self.post(path="/api/v1/alerts/" + row["id"], token=token)
        self.assertEqual(status, 401)


class StrictBodyTests(AlertWriteBase):
    def test_the_body_must_be_exactly_one_boolean(self):
        cases = [b'{"enabled": "yes"}', b'{"enabled": 1}', b'{"enabled": null}',
                 b'{"enabled": true, "extra": 1}', b'{"warning_percent": 50}',
                 b'{}', b'[]', b'not json', b'']
        for body in cases:
            with self.subTest(body=body):
                self.assertEqual(self.post(body=body)[0], 400)
        self.assertIsNone(self.storage.service_alert_settings(self.row["id"]))

    def test_the_content_length_is_required_and_bounded(self):
        # No Content-Length at all: the request is refused.
        status, _, _ = self.post(headers={"Content-Length": ""})
        self.assertIn(status, (400, 411))
        # More than 256 bytes: refused before the body is read.
        status, _, _ = self.post(body=b'{"enabled": true, "pad": "' + b"x" * 300 + b'"}')
        self.assertEqual(status, 413)

    def test_the_content_type_must_be_json(self):
        self.assertEqual(self.post(content_type="text/plain")[0], 415)

    def test_a_chunked_body_is_refused(self):
        status, _, _ = self.post(headers={"Transfer-Encoding": "chunked"})
        self.assertEqual(status, 400)

    def test_an_unknown_service_is_not_found(self):
        self.assertEqual(self.post(path="/api/v1/alerts/nosuchservice")[0], 404)

    def test_other_methods_are_not_allowed(self):
        for method in ("GET", "PUT", "DELETE", "PATCH"):
            with self.subTest(method=method):
                status, headers, _ = self.post(method=method, body=None)
                self.assertEqual(status, 405)
                self.assertEqual(headers["Allow"], "POST")


class EffectTests(AlertWriteBase):
    def test_it_sets_the_state_and_never_toggles(self):
        status, _, body = self.post(body=b'{"enabled": false}')
        self.assertEqual(status, 200)
        self.assertEqual(body, {"ok": True, "alerts": False, "master": True})
        # The same write again changes nothing.
        status, _, body = self.post(body=b'{"enabled": false}')
        self.assertEqual(status, 200)
        self.assertFalse(body["alerts"])
        self.assertEqual(self.storage.service_alert_settings(self.row["id"]),
                         {"enabled": False})

    def test_it_changes_nothing_else(self):
        self.manager.set_service_alerts(self.row["id"],
                                        {"warning_percent": 50, "critical_percent": 60})
        before = next(item for item in self.storage.source_instances()
                      if item["id"] == self.row["id"])
        self.manager.changed.clear()
        self.post(body=b'{"enabled": false}')
        after = next(item for item in self.storage.source_instances()
                     if item["id"] == self.row["id"])
        self.assertEqual(after["revision"], before["revision"])
        self.assertEqual(after["config"], before["config"])
        self.assertFalse(self.manager.changed.is_set(),
                         "the collector was woken up for an alert switch")
        # The thresholds are untouched, and so is the other service.
        stored = self.storage.service_alert_settings(self.row["id"])
        self.assertEqual(stored["warning_percent"], 50)
        self.assertEqual(stored["critical_percent"], 60)
        self.assertIsNone(self.storage.service_alert_settings(self.other["id"]))
        # The master switch is untouched.
        self.assertTrue(self.manager.alerts_state()["enabled"])

    def test_the_master_switch_is_reported(self):
        self.request_alerts_off()
        status, _, body = self.post(body=b'{"enabled": true}')
        self.assertEqual(status, 200)
        self.assertFalse(body["alerts"], "the master is off, so nothing alerts")
        self.assertFalse(body["master"])

    def request_alerts_off(self):
        body = {"enabled": False, "warning_percent": 80, "critical_percent": 95,
                "sound": False, "quiet_hours": False, "quiet_start": "22:00",
                "quiet_end": "07:00", "pop_to_top": True, "mute_hours": 4}
        self.manager.set_alerts(body)

    def test_the_change_is_visible_in_the_status_payload(self):
        self.post(body=b'{"enabled": false}')
        payload = self.status()
        source = next(s for s in payload["sources"] if s["id"] == self.row["id"])
        self.assertFalse(source["alerts"])
        other = next(s for s in payload["sources"] if s["id"] == self.other["id"])
        self.assertTrue(other["alerts"])

    def test_the_admin_listing_agrees_with_the_device(self):
        self.post(body=b'{"enabled": false}')
        listing = {row["id"]: row for row in self.manager.list_sources()["sources"]}
        self.assertFalse(listing[self.row["id"]]["alerts"]["enabled"])
        self.assertTrue(listing[self.other["id"]]["alerts"]["enabled"])
        state = self.manager.service_alert_state(self.row["id"])
        self.assertFalse(state["enabled"])
        self.assertFalse(state["effective"]["enabled"])


class LoggingTests(AlertWriteBase):
    def test_the_line_names_the_client_and_never_the_token(self):
        with self.assertLogs("presto_ai_dash.api", level="INFO") as captured:
            self.post(body=b'{"enabled": false}',
                      headers={"X-Presto-Client": "kitchen-display"})
        text = "\n".join(captured.output)
        self.assertIn("alerts for %s turned off by kitchen-display" % self.row["id"],
                      text)
        self.assertNotIn("device-token", text)

    def test_a_hostile_client_name_is_sanitised(self):
        # A name that is too long is not a name: the daemon says "unknown".
        from presto_ai_dash.clients import sanitize_header
        self.assertEqual(sanitize_header("bad\r\nname"), "unknown")
        with self.assertLogs("presto_ai_dash.api", level="INFO") as captured:
            self.post(body=b'{"enabled": false}',
                      headers={"X-Presto-Client": "x" * 60})
        text = "\n".join(captured.output)
        self.assertIn("turned off by unknown", text)
        self.assertNotIn("x" * 60, text)


class RateLimitTests(AlertWriteBase):
    def test_thirty_writes_a_minute_are_allowed_and_the_next_is_not(self):
        for index in range(30):
            status, _, _ = self.post(body=b'{"enabled": false}')
            self.assertEqual(status, 200, "write %d" % index)
        status, _, body = self.post(body=b'{"enabled": false}')
        self.assertEqual(status, 429)
        self.assertEqual(body["error"], "rate_limited")

    def test_the_limiter_is_separate_from_the_ingest_one(self):
        from presto_ai_dash.api import _IngestRateLimiter
        ingest = _IngestRateLimiter()
        self.assertIsNot(ingest, self.limiter)
        # The ingest limiter never sees these writes.
        self.assertEqual(ingest.check("device-token", 0.0), True)


if __name__ == "__main__":
    unittest.main()
