"""Admin ingest controls: state toggle, tokens, collector download (plan Phase 3)."""

import base64
import json
from pathlib import Path
import sys
import unittest
from urllib.error import HTTPError
from urllib.request import Request, build_opener, ProxyHandler

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from presto_ai_dash.api import ApiServer
from presto_ai_dash.config import ingest_enabled
from envutil import clean_env
from test_management import AdminServerBase

COLLECTOR_FILE = Path(__file__).resolve().parents[1] / "src" / "presto_ai_dash" / \
    "collectors" / "claude_statusline.py"


def bearer_token_response(opener, url, token, body, method="POST"):
    """POST raw bytes with a bearer token; returns (status, body bytes)."""
    request = Request(url, data=body, method=method,
                      headers={"Authorization": "Bearer " + token,
                               "Content-Type": "application/json"})
    try:
        response = opener.open(request, timeout=3)
    except HTTPError as exc:
        response = exc
    with response:
        return response.status, response.read()


class AdminIngestTests(AdminServerBase):
    def setUp(self):
        super().setUp()
        self.disable_fake()
        self.row = self.manager.save({"type": "claude", "name": "Claude personal",
                                      "enabled": True,
                                      "config": {"settings_path": str(Path(self.temp.name) / "settings.json")}})
        # The status port wired exactly like the daemon: the live switch.
        self.status_api = ApiServer(
            "127.0.0.1", 0, self.storage, "device-token",
            status_provider=self.manager.status_payload,
            ingest_enabled=lambda: ingest_enabled(self.config,
                                                  self.storage.ingest_enabled_setting()))
        self.status_api.start()
        self.addCleanup(self.status_api.stop)
        self.ingest_url = ("http://127.0.0.1:%d/api/v1/ingest"
                           % self.status_api.actual_port)

    def token_path(self):
        return "/api/admin/sources/%s/ingest-token" % self.row["id"]

    def create_token(self):
        status, _, data = self.request(self.token_path(), "POST")
        self.assertEqual(status, 201)
        return data

    def test_ingest_state_defaults_to_off_and_unlocked(self):
        status, _, state = self.request("/api/admin/ingest")
        self.assertEqual(status, 200)
        self.assertEqual(state, {"enabled": False, "locked_by_environment": False})

    def test_toggle_requires_authorization_and_validates_the_body(self):
        self.assertEqual(self.request("/api/admin/ingest", token=None)[0], 401)
        cookie, session, _ = self.login()
        headers = {"Cookie": cookie, "Origin": self.base_url}
        self.assertEqual(self.request("/api/admin/ingest", "PUT", {"enabled": True},
                                      token=None, headers=headers)[0], 403)
        headers["X-CSRF-Token"] = session["csrf_token"]
        for data in ("text", {}, {"enabled": "yes"}, {"enabled": True, "why": 1},
                     {"enabled": None}):
            with self.subTest(data=data):
                self.assertEqual(self.request("/api/admin/ingest", "PUT", data,
                                              token=None, headers=headers)[0], 400)

    def test_environment_locks_the_toggle_read_only(self):
        for value in ("1", "0"):
            with clean_env(DASH_INGEST_ENABLED=value):
                with self.subTest(value=value):
                    status, _, state = self.request("/api/admin/ingest")
                    self.assertEqual(status, 200)
                    self.assertEqual(state["locked_by_environment"], True)
                    self.assertEqual(self.request("/api/admin/ingest", "PUT",
                                                  {"enabled": True})[0], 409)
        self.assertIsNone(self.storage.ingest_enabled_setting())

    def test_token_is_returned_once_with_paste_ready_snippets(self):
        data = self.create_token()
        self.assertEqual(set(data), {"token", "posix_snippet", "powershell_snippet"})
        encoded = data["powershell_snippet"].rsplit(" ", 1)[1]
        decoded = base64.b64decode(encoded).decode("utf-16-le")
        for snippet in (data["posix_snippet"], decoded):
            self.assertIn("--token-file", snippet)
            self.assertIn("claude_statusline.py", snippet)
            self.assertIn("/api/v1/ingest", snippet)
            self.assertNotIn(data["token"], snippet)
        # One later listing: no token, no hash, only that one exists.
        status, _, listing = self.request("/api/admin/sources")
        entry = next(row for row in listing["sources"] if row["id"] == self.row["id"])
        self.assertEqual(entry["ingest"]["token_exists"], True)
        self.assertIsNotNone(entry["ingest"]["created_at"])
        self.assertIsNone(entry["ingest"]["collector_seen_at"])
        self.assertIsNone(entry["ingest"]["collector_host"])
        self.assertIsNone(entry["ingest"]["last_progress_at"])
        self.assertNotIn(data["token"], json.dumps(listing))

    def test_rotate_invalidates_the_old_token_end_to_end(self):
        first = self.create_token()["token"]
        second = self.create_token()["token"]
        self.assertNotEqual(first, second)
        self.assertEqual(self.request("/api/admin/ingest", "PUT", {"enabled": True})[0], 200)
        self.assertEqual(bearer_token_response(self.opener, self.ingest_url, first,
                                               b'{"schema": "x"}')[0], 401)
        self.assertEqual(bearer_token_response(self.opener, self.ingest_url, second,
                                               json.dumps({
            "schema": "presto-ai-dash.observation/1",
            "collector": {"name": "c", "version": "1", "host": "laptop"},
            "client_time": "2026-10-18T14:02:11+00:00"}).encode())[0], 204)

    def test_revoke_stops_the_token(self):
        token = self.create_token()["token"]
        status, _, result = self.request(self.token_path(), "DELETE")
        self.assertEqual(status, 200)
        self.assertEqual(result, {"revoked": True})
        self.assertEqual(self.request("/api/admin/ingest", "PUT", {"enabled": True})[0], 200)
        self.assertEqual(bearer_token_response(self.opener, self.ingest_url, token,
                                               json.dumps({
            "schema": "presto-ai-dash.observation/1",
            "collector": {"name": "c", "version": "1", "host": "laptop"},
            "client_time": "2026-10-18T14:02:11+00:00"}).encode())[0], 401)
        _, _, entry = self.request("/api/admin/sources")
        claude = next(row for row in entry["sources"] if row["id"] == self.row["id"])
        self.assertFalse(claude["ingest"]["token_exists"])

    def test_token_routes_require_cookie_csrf_and_exact_origin(self):
        for method in ("POST", "DELETE"):
            with self.subTest(method=method):
                self.assertEqual(self.request(self.token_path(), method, token=None)[0], 401)
        cookie, session, _ = self.login()
        headers = {"Cookie": cookie, "Origin": self.base_url,
                   "X-CSRF-Token": session["csrf_token"]}
        for changes in ({"X-CSRF-Token": "wrong"},
                        {"Origin": "http://localhost:%d" % self.admin.actual_port}):
            with self.subTest(changes=changes):
                self.assertEqual(self.request(self.token_path(), "POST", token=None,
                                              headers={**headers, **changes})[0], 403)
        self.assertEqual(self.request(self.token_path(), "POST", token=None,
                                      headers=headers)[0], 201)

    def test_non_push_sources_never_receive_or_keep_tokens(self):
        litellm = self.manager.save({"type": "litellm", "name": "Gateway"})
        path = "/api/admin/sources/%s/ingest-token" % litellm["id"]
        self.assertEqual(self.request(path, "POST")[0], 400)
        self.assertEqual(self.request(path, "DELETE")[0], 400)
        _, _, listing = self.request("/api/admin/sources")
        entry = next(row for row in listing["sources"] if row["id"] == litellm["id"])
        self.assertNotIn("ingest", entry)
        fake = next(row for row in listing["sources"] if row["type"] == "fake")
        self.assertNotIn("ingest", fake)

    def test_unknown_source_id_is_a_404(self):
        path = "/api/admin/sources/no-such-id/ingest-token"
        self.assertEqual(self.request(path, "POST")[0], 404)
        self.assertEqual(self.request(path, "DELETE")[0], 404)

    def test_collector_download_is_the_exact_file(self):
        status, headers, body = self.request("/api/admin/collector")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "text/x-python; charset=utf-8")
        self.assertIn("attachment", headers["Content-Disposition"])
        self.assertEqual(body, COLLECTOR_FILE.read_bytes())
        self.assertEqual(self.request("/api/admin/collector", token=None)[0], 401)

    def test_stored_toggle_changes_the_route_live_without_a_restart(self):
        self.assertEqual(bearer_token_response(self.opener, self.ingest_url,
                                               "unused", b"{}")[0], 404)
        self.assertEqual(self.request("/api/admin/ingest", "PUT", {"enabled": True})[0], 200)
        token = self.create_token()["token"]
        self.assertEqual(bearer_token_response(self.opener, self.ingest_url, token,
                                               json.dumps({
            "schema": "presto-ai-dash.observation/1",
            "collector": {"name": "c", "version": "1", "host": "laptop"},
            "client_time": "2026-10-18T14:02:11+00:00"}).encode())[0], 204)
        # Off again, without touching any process.
        status, _, state = self.request("/api/admin/ingest", "PUT", {"enabled": False})
        self.assertEqual(status, 200)
        self.assertEqual(state["enabled"], False)
        self.assertEqual(bearer_token_response(self.opener, self.ingest_url, token,
                                               json.dumps({
            "schema": "presto-ai-dash.observation/1",
            "collector": {"name": "c", "version": "1", "host": "laptop"},
            "client_time": "2026-10-18T14:02:11+00:00"}).encode())[0], 404)


class IngestPageTests(AdminServerBase):
    """Frontend smoke markers; the page itself is not browser-tested here."""

    def test_page_assets_wire_the_ingest_controls(self):
        status, _, html = self.request("/")
        self.assertEqual(status, 200)
        for marker in (b"ingest-enabled", b"ingest-lock", b"download-collector"):
            self.assertIn(marker, html)
        _, _, script = self.request("/admin.js")
        for marker in (b"/api/admin/ingest", b"/ingest-token", b"shown only once"):
            self.assertIn(marker, script)
        _, _, style = self.request("/admin.css")
        self.assertIn(b"token-box", style)


if __name__ == "__main__":
    unittest.main()
