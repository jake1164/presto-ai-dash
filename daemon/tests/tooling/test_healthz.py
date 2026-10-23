"""The container's health check: one route, one answer, no token.

``GET /healthz`` is the only unauthenticated route of the daemon. It exists
so a container can tell whether the daemon is alive. It says one thing and
nothing else, and every other route still needs its token.
"""

import json
from pathlib import Path
import sys
import unittest
from urllib.error import HTTPError
from urllib.request import Request, build_opener, ProxyHandler

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from presto_ai_dash.api import ApiServer
from admin.test_management import ManagementBase


class HealthTests(ManagementBase):
    """One route, one answer, and the token rules unchanged."""

    def setUp(self):
        super().setUp()
        self.api = ApiServer("127.0.0.1", 0, self.storage, "device-token",
                             status_provider=self.manager.status_payload)
        self.api.start()
        self.addCleanup(self.api.stop)
        self.base_url = "http://127.0.0.1:%d" % self.api.actual_port
        self.opener = build_opener(ProxyHandler({}))

    def get(self, path, token=None, headers=None):
        extra = {"Authorization": "Bearer " + token} if token else {}
        extra.update(headers or {})
        request = Request(self.base_url + path, headers=extra, method="GET")
        try:
            response = self.opener.open(request, timeout=3)
        except HTTPError as exc:
            response = exc
        with response:
            body = response.read()
            return response.status, dict(response.headers), body

    def test_the_health_check_answers_without_a_token(self):
        status, _, body = self.get("/healthz")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"ok": True})

    def test_the_answer_holds_nothing_else(self):
        _, headers, body = self.get("/healthz")
        self.assertEqual(set(json.loads(body)), {"ok"})
        text = body.decode("utf-8")
        # No version, no source names, no counts, no times.
        for secret in ("version", "source", "claude", "codex", "ollama", "litellm",
                       "token", "database", "uptime", "updated_at"):
            self.assertNotIn(secret, text.lower())
        self.assertEqual(headers["Cache-Control"], "no-store")

    def test_a_token_does_not_change_the_answer(self):
        _, _, body = self.get("/healthz", token="device-token")
        self.assertEqual(json.loads(body), {"ok": True})

    def test_the_token_routes_still_need_the_token(self):
        status, _, body = self.get("/api/v1/status")
        self.assertEqual(status, 401)
        self.assertEqual(json.loads(body), {"error": "unauthorised"})
        status, _, body = self.get("/api/v1/status", token="device-token")
        self.assertEqual(status, 200)
        self.assertIn("sources", json.loads(body))

    def test_a_wrong_token_is_refused_on_the_token_routes(self):
        for token in ("wrong-token", "", "device-tokenx"):
            with self.subTest(token=token):
                status, _, _ = self.get("/api/v1/status", token=token)
                self.assertEqual(status, 401)

    def test_the_other_routes_still_hide(self):
        for path in ("/", "/api/v1", "/api/v1/nothing", "/api/admin/sources"):
            with self.subTest(path=path):
                status, _, body = self.get(path)
                self.assertIn(status, (401, 404))
                self.assertNotIn(b"claude", body.lower())


if __name__ == "__main__":
    unittest.main()
