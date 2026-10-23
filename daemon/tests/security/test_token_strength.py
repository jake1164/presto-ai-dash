"""A token somebody chose must be hard to guess; a failed guess is slowed and seen.

The daemon's own tokens are 192 and 256 bits and cannot be guessed. A token
the operator chooses can be, and so can a placeholder copied from the
documentation. The daemon refuses those at startup, pauses before answering
a bearer token that did not match, and logs the address (never the token) of
the client that sent it.
"""

import json
import os
import secrets
import tempfile
import time
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from presto_ai_dash.admin import AdminServer
from presto_ai_dash.api import ApiServer, _AuthFailures
from presto_ai_dash.config import (ConfigError, MIN_TOKEN_LENGTH, PLACEHOLDER_TOKENS,
                                   check_token_strength, load_config)
from admin.test_management import ManagementBase

GOOD = "Kq7-mW2x_Rt9vLp4Zd8Nb"  # 21 characters, many different ones


class TokenStrengthTests(unittest.TestCase):
    def load(self, toml, env=None):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "dash.toml"
            path.write_text(toml, encoding="utf-8")
            clean = {name: value for name, value in os.environ.items()
                     if name not in ("DASH_SHARED_TOKEN", "DASH_ADMIN_TOKEN")}
            clean.update(env or {})
            with patch.dict(os.environ, clean, clear=True):
                return load_config(path)

    def refused(self, toml, env=None):
        with self.assertRaises(ConfigError) as caught:
            self.load(toml, env)
        return str(caught.exception)

    def test_a_short_shared_token_is_refused_and_never_echoed(self):
        message = self.refused('[daemon]\nshared_token = "hunter2-short"\n')
        self.assertIn("daemon.shared_token", message)
        self.assertIn(str(MIN_TOKEN_LENGTH), message)
        self.assertNotIn("hunter2-short", message)

    def test_a_short_admin_token_is_refused(self):
        self.assertIn("admin.token", self.refused('[admin]\ntoken = "short-token"\n'))

    def test_a_token_from_the_environment_is_checked_too(self):
        self.assertIn("admin.token", self.refused("", {"DASH_ADMIN_TOKEN": "short"}))
        self.assertIn("daemon.shared_token", self.refused("", {"DASH_SHARED_TOKEN": "short"}))

    def test_every_placeholder_from_the_documentation_is_refused(self):
        for value in sorted(PLACEHOLDER_TOKENS) + ["Change-Me", "A-Long-Random-String-Here"]:
            with self.subTest(value=value):
                message = self.refused('[daemon]\nshared_token = "%s"\n' % value)
                self.assertIn("placeholder", message)

    def test_the_placeholders_in_the_shipped_files_are_covered(self):
        root = Path(__file__).resolve().parents[3]
        example = (root / "presto" / "config.example.py").read_text(encoding="utf-8")
        self.assertIn("change-me", example)
        self.assertIn("change-me", PLACEHOLDER_TOKENS)
        install = (root / "docs" / "user" / "getting-started" /
                   "install-the-daemon.md").read_text(encoding="utf-8")
        self.assertNotIn("a-long-random-string-here", install)

    def test_a_repetitive_token_is_refused(self):
        self.assertIn("repetitive", self.refused('[daemon]\nshared_token = "%s"\n' % ("a" * 24)))
        self.assertIn("repetitive", self.refused('[admin]\ntoken = "%s"\n' % ("ab" * 12)))

    def test_a_strong_token_and_an_empty_one_are_accepted(self):
        config = self.load('[daemon]\nshared_token = "%s"\n[admin]\ntoken = "%s"\n'
                           % (GOOD, GOOD + "-admin"))
        self.assertEqual(config["daemon"]["shared_token"], GOOD)
        self.assertEqual(self.load("")["daemon"]["shared_token"], "")

    def test_a_token_of_exactly_the_minimum_length_is_accepted(self):
        token = "Ab1-Cd2_Ef3Gh4Ij"[:MIN_TOKEN_LENGTH]
        self.assertEqual(len(token), MIN_TOKEN_LENGTH)
        check_token_strength("daemon.shared_token", token)
        with self.assertRaises(ConfigError):
            check_token_strength("daemon.shared_token", token[:-1])

    def test_the_tokens_the_daemon_generates_pass_its_own_check(self):
        for size in (24, 32):
            check_token_strength("x", secrets.token_urlsafe(size))

    def test_the_example_files_still_load(self):
        root = Path(__file__).resolve().parents[3]
        with patch.dict(os.environ, {}, clear=False):
            load_config(root / "examples" / "config.example.toml")


class AuthFailureTableTests(unittest.TestCase):
    def test_the_first_failure_and_then_each_power_of_ten_are_reported(self):
        table = _AuthFailures()
        reported = [table.record("10.0.0.9", 0.0) for _ in range(1000)]
        self.assertEqual([count for count in reported if count], [1, 10, 100, 1000])
        self.assertEqual(reported[0], 1)
        self.assertIsNone(reported[1])

    def test_each_address_is_counted_on_its_own(self):
        table = _AuthFailures()
        self.assertEqual(table.record("10.0.0.1", 0.0), 1)
        self.assertEqual(table.record("10.0.0.2", 0.0), 1)
        self.assertIsNone(table.record("10.0.0.1", 1.0))

    def test_a_new_minute_starts_a_new_count(self):
        table = _AuthFailures()
        table.record("10.0.0.1", 0.0)
        table.record("10.0.0.1", 1.0)
        self.assertEqual(table.record("10.0.0.1", 61.0), 1)

    def test_the_table_cannot_grow_without_limit(self):
        table = _AuthFailures()
        for number in range(_AuthFailures.MAX_ADDRESSES * 3):
            table.record("10.1.%d.%d" % divmod(number, 250), float(number))
        self.assertLessEqual(len(table._entries), _AuthFailures.MAX_ADDRESSES)


class FailedSignInTests(ManagementBase):
    DELAY = 0.4

    def setUp(self):
        super().setUp()
        self.disable_fake()
        self.api = ApiServer("127.0.0.1", 0, self.storage, "device-token-0123456789",
                             status_provider=self.manager.status_payload,
                             ingest_enabled=lambda: True)
        self.api._server.RequestHandlerClass.auth_failure_delay = self.DELAY
        self.api.start()
        self.addCleanup(self.api.stop)
        self.opener = build_opener(ProxyHandler({}))

    def get(self, path, token=None, port=None, method="GET", body=None):
        headers = {"Authorization": "Bearer " + token} if token else {}
        data = json.dumps(body).encode() if body is not None else None
        if data:
            headers["Content-Type"] = "application/json"
        request = Request("http://127.0.0.1:%d%s" % (port or self.api.actual_port, path),
                          headers=headers, data=data, method=method)
        started = time.monotonic()
        try:
            with self.opener.open(request, timeout=5) as response:
                status = response.status
        except HTTPError as exc:
            status = exc.code
        return status, time.monotonic() - started

    def request(self, *args, **kwargs):
        """The status, and the pauses the server took before answering."""
        with patch("presto_ai_dash.api.time.sleep") as sleep:
            status, _ = self.get(*args, **kwargs)
        return status, [call.args[0] for call in sleep.call_args_list]

    def test_a_wrong_token_is_answered_after_a_pause(self):
        status, elapsed = self.get("/api/v1/status", "wrong-token-value-123456")
        self.assertEqual(status, 401)
        self.assertGreaterEqual(elapsed, self.DELAY * 0.9)

    def test_the_pause_is_the_configured_one(self):
        status, pauses = self.request("/api/v1/status", "wrong-token-value-123456")
        self.assertEqual(status, 401)
        self.assertEqual(pauses, [self.DELAY])

    def test_the_right_token_is_not_delayed(self):
        status, pauses = self.request("/api/v1/status", "device-token-0123456789")
        self.assertEqual(status, 200)
        self.assertEqual(pauses, [])

    def test_a_request_with_no_token_at_all_is_not_delayed_or_logged(self):
        with self.assertNoLogs("presto_ai_dash.api", level="WARNING"):
            status, pauses = self.request("/api/v1/status")
        self.assertEqual(status, 401)
        self.assertEqual(pauses, [])

    def test_the_failure_is_logged_with_the_address_and_never_the_token(self):
        secret = "guess-that-must-not-be-logged-1"
        with self.assertLogs("presto_ai_dash.api", level="WARNING") as logged:
            self.get("/api/v1/status", secret)
        text = "\n".join(logged.output)
        self.assertIn("127.0.0.1", text)
        self.assertIn("status", text)
        self.assertNotIn(secret, text)
        self.assertNotIn("device-token", text)

    def test_many_failures_do_not_flood_the_log(self):
        self.api._server.RequestHandlerClass.auth_failure_delay = 0
        with self.assertLogs("presto_ai_dash.api", level="WARNING") as logged:
            for number in range(12):
                self.get("/api/v1/status", "wrong-%d-xxxxxxxxxxxx" % number)
        self.assertEqual(len(logged.records), 2)  # the 1st and the 10th

    def test_the_alerts_route_is_slowed_too(self):
        status, elapsed = self.get("/api/v1/alerts/fake", "wrong-token-value-123456",
                                   method="POST", body={"enabled": False})
        self.assertEqual(status, 401)
        self.assertGreaterEqual(elapsed, self.DELAY * 0.9)

    def test_an_unknown_ingest_token_is_slowed_and_logged(self):
        with self.assertLogs("presto_ai_dash.api", level="WARNING"):
            status, elapsed = self.get("/api/v1/ingest", "no-such-ingest-token-1234",
                                       method="POST", body={})
        self.assertEqual(status, 401)
        self.assertGreaterEqual(elapsed, self.DELAY * 0.9)

    def test_an_ingest_request_with_no_token_is_not_logged(self):
        with self.assertNoLogs("presto_ai_dash.api", level="WARNING"):
            status, pauses = self.request("/api/v1/ingest", method="POST", body={})
        self.assertEqual(status, 401)
        self.assertEqual(pauses, [])


class AdminFailedSignInTests(ManagementBase):
    DELAY = 0.4

    def setUp(self):
        super().setUp()
        self.admin = AdminServer(0, self.manager, "admin-token-0123456789")
        self.admin._server.RequestHandlerClass.auth_failure_delay = self.DELAY
        self.admin.start()
        self.addCleanup(self.admin.stop)
        self.opener = build_opener(ProxyHandler({}))

    def get(self, path, token=None):
        headers = {"Authorization": "Bearer " + token} if token else {}
        request = Request("http://127.0.0.1:%d%s" % (self.admin.actual_port, path),
                          headers=headers)
        started = time.monotonic()
        try:
            with self.opener.open(request, timeout=5) as response:
                status = response.status
        except HTTPError as exc:
            status = exc.code
        return status, time.monotonic() - started

    def request(self, *args, **kwargs):
        """The status, and the pauses the server took before answering."""
        with patch("presto_ai_dash.api.time.sleep") as sleep:
            status, _ = self.get(*args, **kwargs)
        return status, [call.args[0] for call in sleep.call_args_list]

    def test_a_wrong_admin_token_is_slowed_and_logged_as_admin(self):
        secret = "wrong-admin-token-xxxxxxxx"
        with self.assertLogs("presto_ai_dash.api", level="WARNING") as logged:
            status, elapsed = self.get("/api/admin/sources", secret)
        self.assertEqual(status, 401)
        self.assertGreaterEqual(elapsed, self.DELAY * 0.9)
        text = "\n".join(logged.output)
        self.assertIn("admin", text)
        self.assertNotIn(secret, text)

    def test_the_right_admin_token_is_not_delayed(self):
        status, pauses = self.request("/api/admin/sources", "admin-token-0123456789")
        self.assertEqual(status, 200)
        self.assertEqual(pauses, [])

    def test_opening_the_page_without_a_session_is_not_delayed_or_logged(self):
        with self.assertNoLogs("presto_ai_dash.api", level="WARNING"):
            status, pauses = self.request("/api/admin/session")
        self.assertEqual(status, 401)
        self.assertEqual(pauses, [])

    def test_the_status_and_admin_servers_keep_separate_counts(self):
        from presto_ai_dash.api import BoundedRequestHandler
        status_handler = ApiServer("127.0.0.1", 0, self.storage, "device-token-0123456789")
        self.addCleanup(status_handler.stop)
        self.assertIsNot(self.admin._server.RequestHandlerClass.auth_failures,
                         status_handler._server.RequestHandlerClass.auth_failures)
        self.assertIsNot(self.admin._server.RequestHandlerClass.auth_failures,
                         BoundedRequestHandler.auth_failures)


if __name__ == "__main__":
    unittest.main()
