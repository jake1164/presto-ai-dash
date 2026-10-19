"""The HTTP ingest route: auth order, limits, throttle (plan Phase 3)."""

import json
import time
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, build_opener, ProxyHandler

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from presto_ai_dash.api import ApiServer, _IngestRateLimiter
from presto_ai_dash.ingest import SCHEMA_NAME
from test_management import ManagementBase


def envelope(percent=23.5, host="laptop", windows=True):
    """A valid Claude push, shape-matching what the collector sends."""
    body = {"schema": SCHEMA_NAME,
            "collector": {"name": "claude-statusline", "version": "1", "host": host},
            "client_time": "2026-10-18T14:02:11+00:00"}
    if windows:
        body["windows"] = [
            {"id": "five_hour", "label": "5h", "period_minutes": 300,
             "used_percent": percent, "resets_at": "2026-10-18T18:00:00+00:00",
             "state": "available"},
            {"id": "seven_day", "label": "Weekly", "period_minutes": 10080,
             "used_percent": None, "resets_at": None, "state": "unavailable"}]
    return body


class IngestApiBase(ManagementBase):
    def setUp(self):
        super().setUp()
        self.disable_fake()
        self.row = self.manager.save({"type": "claude", "name": "Claude personal",
                                      "enabled": True,
                                      "config": {"settings_path": str(Path(self.temp.name) / "settings.json")}})
        self.ingest_on = True
        self.limiter = _IngestRateLimiter()
        self.api = ApiServer("127.0.0.1", 0, self.storage, "device-token",
                             status_provider=self.manager.status_payload,
                             ingest_enabled=lambda: self.ingest_on,
                             ingest_rate_limiter=self.limiter)
        self.api.start()
        self.addCleanup(self.api.stop)
        self.base_url = "http://127.0.0.1:%d" % self.api.actual_port
        self.opener = build_opener(ProxyHandler({}))
        self.ingest_path = "/api/v1/ingest"

    def token(self):
        return self.storage.create_ingest_token(self.row["id"])

    def post(self, body, token="auto", raw=None, headers=None, path=None):
        if token == "auto":
            token = self.token()
        extra = {"Authorization": "Bearer " + token} if token is not None else {}
        if raw is None:
            raw = json.dumps(body).encode("utf-8")
        extra["Content-Type"] = "application/json"
        extra.update(headers or {})
        request = Request(self.base_url + (path or self.ingest_path), data=raw,
                          headers=extra, method="POST")
        try:
            response = self.opener.open(request, timeout=3)
        except HTTPError as exc:
            response = exc
        with response:
            data = response.read()
            return response.status, dict(response.headers), data


class RouteTests(IngestApiBase):
    def test_disabled_returns_404_for_valid_and_invalid_tokens_alike(self):
        self.ingest_on = False
        for name, token in (("valid", self.token()), ("invalid", "not-a-token")):
            with self.subTest(case=name):
                status, _, body = self.post(envelope(), token=token)
                self.assertEqual(status, 404)
                self.assertEqual(json.loads(body), {"error": "not_found"})

    def test_unknown_malformed_and_empty_tokens_are_unauthorised(self):
        envelope_bytes = json.dumps(envelope()).encode("utf-8")
        cases = {"absent": dict(token=None),
                 "malformed": dict(token="wrong-token"),
                 "empty-bearer": dict(token=""),
                 "wrong-scheme": dict(token=None,
                                      headers={"Authorization": "Basic abc"})}
        for name, options in cases.items():
            with self.subTest(case=name):
                status, headers, _ = self.post(envelope(), raw=envelope_bytes, **options)
                self.assertEqual(status, 401)
                self.assertEqual(headers["WWW-Authenticate"], "Bearer")

    def test_revoked_and_rotated_old_tokens_are_unauthorised(self):
        revoked = self.storage.create_ingest_token(self.row["id"])
        self.storage.revoke_ingest_token(self.row["id"])
        self.assertEqual(self.post(envelope(), token=revoked)[0], 401)
        fresh = self.storage.create_ingest_token(self.row["id"])
        self.assertEqual(self.post(envelope(), token=revoked)[0], 401)
        self.assertEqual(self.post(envelope(), token=fresh)[0], 204)

    def test_shared_device_token_is_refused_for_ingest(self):
        status, _, _ = self.post(envelope(), token="device-token")
        self.assertEqual(status, 401)

    def test_ingest_token_cannot_read_the_status_route(self):
        request = Request(self.base_url + "/api/v1/status",
                          headers={"Authorization": "Bearer " + self.token()})
        try:
            response = self.opener.open(request, timeout=3)
        except HTTPError as exc:
            response = exc
        with response:
            self.assertEqual(response.status, 401)
            response.read()

    def test_oversize_content_length_is_413_before_the_body_is_read(self):
        status, _, body = self.post(envelope(), raw=b"",
                                    headers={"Content-Length": "16385"})
        self.assertEqual(status, 413)
        self.assertEqual(json.loads(body), {"error": "body_too_large"})

    def test_bad_content_length_is_400(self):
        for value in ("abc", "-5"):
            with self.subTest(value=value):
                status, _, _ = self.post(envelope(), raw=b"",
                                         headers={"Content-Length": value})
                self.assertEqual(status, 400)

    def test_chunked_transfer_encoding_is_unsupported(self):
        status, _, _ = self.post(envelope(), headers={"Transfer-Encoding": "chunked"})
        self.assertEqual(status, 400)
        self.assertEqual(self.post(envelope())[0], 204)  # the same body is fine unchunked

    def test_invalid_json_is_400_without_echoing_the_input(self):
        status, _, body = self.post(envelope(), raw=b"{oops")
        self.assertEqual(status, 400)
        self.assertNotIn(b"{oops", body)

    def test_unknown_envelope_key_is_400_without_echoing_the_key(self):
        bad = envelope()
        bad["baywatch"] = "suspicious_value"
        status, _, body = self.post(bad)
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body), {"error": "invalid_envelope"})
        self.assertNotIn(b"baywatch", body)
        self.assertNotIn(b"suspicious_value", body)

    def test_disabled_instance_and_deleted_instance_tokens_are_unauthorised(self):
        token = self.token()
        self.manager.save({"enabled": False, "revision": self.row["revision"]}, self.row["id"])
        self.assertEqual(self.post(envelope(), token=token)[0], 401)
        latest = next(row for row in self.manager.list_sources()["sources"]
                      if row["id"] == self.row["id"])
        self.manager.delete(latest["id"], latest["revision"])
        self.assertEqual(self.post(envelope(), token=token)[0], 401)

    def test_heartbeat_204_sets_collector_seen_at_and_leaves_quota_untouched(self):
        self.limiter.INTERVAL_SECONDS = 0.0  # two quick posts, one account
        token = self.token()
        self.assertEqual(self.post(envelope(), token=token)[0], 204)
        before = self.storage.observation(self.row["id"])
        self.assertEqual(before["report"]["windows"][0]["used_percent"], 23.5)
        self.assertEqual(self.post(envelope(windows=False), token=token)[0], 204)
        after = self.storage.observation(self.row["id"])
        self.assertEqual(after["report"], before["report"])
        self.assertNotEqual(after["collector_seen_at"], before["collector_seen_at"])
        self.assertEqual(after["collector_host"], "laptop")

    def test_token_never_appears_in_a_response_body_or_in_the_log(self):
        token = self.token()
        with self.assertLogs("presto_ai_dash.api", level="DEBUG") as logs:
            self.assertEqual(self.post(envelope(), token=token)[0], 204)
            self.ingest_on = False
            self.assertEqual(self.post(envelope(), token=token)[0], 404)
            self.ingest_on = True
            self.assertEqual(self.post(envelope(), token="wrong-token")[0], 401)
        for output in logs.output:
            self.assertNotIn(token, output)


class ClockTests(IngestApiBase):
    def test_second_fast_post_is_429_then_204_after_the_interval(self):
        clock = {"now": 5000.0}
        limiter_patch = patch("presto_ai_dash.api.time.monotonic",
                              side_effect=lambda: clock["now"])
        limiter_patch.start()
        self.addCleanup(limiter_patch.stop)
        token = self.token()
        self.assertEqual(self.post(envelope(), token=token)[0], 204)
        clock["now"] += 0.5
        status, _, _ = self.post(envelope(), token=token)
        self.assertEqual(status, 429)
        clock["now"] += 3.0
        self.assertEqual(self.post(envelope(), token=token)[0], 204)


class LimiterUnitTests(unittest.TestCase):
    def test_second_call_within_the_interval_is_limited(self):
        limiter = _IngestRateLimiter()
        self.assertTrue(limiter.check("token-hash", 100.0))
        self.assertFalse(limiter.check("token-hash", 101.0))
        self.assertTrue(limiter.check("other-hash", 101.0))
        self.assertTrue(limiter.check("token-hash", 102.1))

    def test_the_map_drops_old_entries_instead_of_growing(self):
        limiter = _IngestRateLimiter()
        for index in range(limiter.MAX_ENTRIES + 50):
            self.assertTrue(limiter.check("hash-%d" % index, 500.0 + index))
        self.assertLessEqual(len(limiter._seen), limiter.MAX_ENTRIES)


if __name__ == "__main__":
    unittest.main()
