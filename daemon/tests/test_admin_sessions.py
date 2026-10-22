"""Browser sessions: bound to the token that made them, limited, and revocable.

A leaked admin token used to leave a session behind that outlived rotating the
token, and nothing signed every browser out. A configured token is stable, so a
session now dies with it. A generated token changes at every start by design
(the page promises a browser stays signed in through that), so for those there
is a sign-out-everywhere action. The session cookie can be marked Secure for a
page that is served over HTTPS.
"""

import json
import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from presto_ai_dash.admin import AdminServer
from presto_ai_dash.admin_sessions import AdminSessions, SESSION_SECONDS, token_binding
from presto_ai_dash.config import ConfigError, load_config
from presto_ai_dash.effective import effective_settings
from presto_ai_dash.management import SourceConfiguration
from presto_ai_dash.storage import Storage
from test_management import ManagementBase

TOKEN = "configured-admin-token-0123456789"
OTHER = "another-admin-token-9876543210"


class SessionTestBase(ManagementBase):
    def setUp(self):
        super().setUp()
        self.opener = build_opener(ProxyHandler({}))

    def serve(self, token=TOKEN, generated=False, secure=False, port=0):
        server = AdminServer(port, self.manager, token, token_generated=generated,
                             secure_cookie=secure)
        server.start()
        self.addCleanup(server.stop)
        return server

    def call(self, server, path, method="GET", token=TOKEN, cookie=None, data=None,
             csrf=None, origin=None):
        headers = {"Authorization": "Bearer " + token} if token else {}
        if cookie:
            headers["Cookie"] = cookie
        if csrf:
            headers["X-CSRF-Token"] = csrf
        if origin:
            headers["Origin"] = origin
        body = None
        if data is not None:
            body = json.dumps(data).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = Request("http://127.0.0.1:%d%s" % (server.actual_port, path), data=body,
                          headers=headers, method=method)
        try:
            response = self.opener.open(request, timeout=5)
        except HTTPError as exc:
            response = exc
        with response:
            raw = response.read()
            text = raw.decode("utf-8", "replace")
            return response.status, dict(response.headers), (
                json.loads(text) if text.startswith("{") else text)

    def login(self, server, token=TOKEN):
        status, headers, session = self.call(server, "/api/admin/session", "POST", token=token)
        self.assertEqual(status, 201)
        return headers["Set-Cookie"].split(";", 1)[0], session, headers

    def alive(self, server, cookie):
        return self.call(server, "/api/admin/session", token=None, cookie=cookie)[0] == 200

    def restart(self, server, **options):
        """The same daemon port and database, started again with other options."""
        port = server.actual_port
        server.stop()
        return self.serve(port=port, **options)


class BindingTests(SessionTestBase):
    def test_a_configured_token_that_stays_the_same_keeps_its_sessions_across_a_restart(self):
        server = self.serve()
        cookie, _, _ = self.login(server)
        server = self.restart(server)
        self.assertTrue(self.alive(server, cookie))

    def test_rotating_a_configured_token_ends_the_sessions_made_under_the_old_one(self):
        server = self.serve()
        cookie, _, _ = self.login(server)
        self.assertTrue(self.alive(server, cookie))
        server = self.restart(server, token=OTHER)
        self.assertFalse(self.alive(server, cookie))
        # The new token works, and starts sessions of its own.
        fresh, _, _ = self.login(server, token=OTHER)
        self.assertTrue(self.alive(server, fresh))

    def test_a_session_that_ended_with_its_token_does_not_come_back_with_the_old_token(self):
        server = self.serve()
        cookie, _, _ = self.login(server)
        server = self.restart(server, token=OTHER)
        self.assertFalse(self.alive(server, cookie))
        server = self.restart(server, token=TOKEN)
        self.assertFalse(self.alive(server, cookie))

    def test_the_dead_sessions_are_removed_not_just_ignored(self):
        server = self.serve()
        self.login(server)
        self.login(server)
        self.assertEqual(self.storage.admin_session_count(time.time(), token_binding(TOKEN, False)), 2)
        self.restart(server, token=OTHER)
        self.assertEqual(self.storage.admin_session_count(time.time(), token_binding(OTHER, False)), 0)
        self.assertEqual(self.storage._conn.execute("SELECT COUNT(*) FROM admin_sessions").fetchone()[0], 0)

    def test_a_generated_token_that_changes_keeps_its_sessions(self):
        server = self.serve(token="startup-code-one-0123456789", generated=True)
        cookie, _, _ = self.login(server, token="startup-code-one-0123456789")
        server = self.restart(server, token="startup-code-two-0123456789", generated=True)
        self.assertTrue(self.alive(server, cookie))

    def test_changing_between_a_generated_and_a_configured_token_ends_sessions(self):
        server = self.serve(token="startup-code-one-0123456789", generated=True)
        cookie, _, _ = self.login(server, token="startup-code-one-0123456789")
        server = self.restart(server, token=TOKEN, generated=False)
        self.assertFalse(self.alive(server, cookie))
        configured, _, _ = self.login(server)
        server = self.restart(server, token="startup-code-three-0123456789", generated=True)
        self.assertFalse(self.alive(server, configured))

    def test_the_token_is_not_stored_and_its_derived_binding_is_not_the_token(self):
        server = self.serve()
        self.login(server)
        rows = self.storage._conn.execute("SELECT binding FROM admin_sessions").fetchall()
        self.assertEqual(len(rows), 1)
        binding = rows[0]["binding"]
        self.assertTrue(binding.startswith("token:"))
        self.assertNotIn(TOKEN, binding)
        self.assertNotIn(TOKEN, json.dumps([tuple(row) for row in self.storage._conn.execute(
            "SELECT * FROM admin_sessions")]))

    def test_two_tokens_have_two_bindings_and_one_token_always_the_same_one(self):
        self.assertEqual(token_binding(TOKEN, False), token_binding(TOKEN, False))
        self.assertNotEqual(token_binding(TOKEN, False), token_binding(OTHER, False))
        self.assertEqual(token_binding(TOKEN, True), token_binding(OTHER, True))
        self.assertNotEqual(token_binding(TOKEN, True), token_binding(TOKEN, False))

    def test_a_session_from_before_binding_existed_is_not_accepted(self):
        server = self.serve()
        sessions = AdminSessions(self.storage, binding="")
        credential, _ = sessions.create()
        cookie = "presto_admin_session_%d=%s" % (server.actual_port, credential)
        self.assertFalse(self.alive(server, cookie))


class MigrationTests(unittest.TestCase):
    def test_a_version_7_database_gains_the_binding_and_its_old_sessions_end(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)  # registered first, so it runs after the database closes
        path = Path(temp.name) / "old.db"
        Storage(path).close()
        connection = sqlite3.connect(path)
        try:
            connection.execute("ALTER TABLE admin_sessions DROP COLUMN binding")
        except sqlite3.OperationalError:
            connection.close()
            self.skipTest("this SQLite cannot drop a column to build the old schema")
        connection.execute("INSERT INTO admin_sessions (token_hash, csrf_token, expires_at)"
                           " VALUES ('h', 'c', ?)", (time.time() + 1000,))
        connection.execute("PRAGMA user_version = 7")
        connection.commit()
        connection.close()
        storage = Storage(path)
        self.addCleanup(storage.close)
        columns = {row["name"] for row in storage._conn.execute("PRAGMA table_info(admin_sessions)")}
        self.assertIn("binding", columns)
        self.assertEqual(storage._conn.execute("PRAGMA user_version").fetchone()[0], 8)
        # The old row has no binding, so no current token accepts it.
        self.assertIsNone(storage.admin_session("h", time.time(), token_binding(TOKEN, False)))
        self.assertIsNone(storage.admin_session("h", time.time(), token_binding(TOKEN, True)))


class SessionLimitTests(SessionTestBase):
    def test_the_oldest_sessions_go_first_when_there_are_too_many(self):
        server = self.serve()
        cookies = [self.login(server)[0] for _ in range(Storage.ADMIN_SESSION_LIMIT + 5)]
        count = self.storage.admin_session_count(time.time(), token_binding(TOKEN, False))
        self.assertEqual(count, Storage.ADMIN_SESSION_LIMIT)
        self.assertFalse(self.alive(server, cookies[0]))
        self.assertTrue(self.alive(server, cookies[-1]))

    def test_the_cap_is_a_storage_rule_and_takes_a_limit(self):
        now = time.time()
        for index in range(6):
            self.storage.create_admin_session("h%d" % index, "c", now + 100 + index, now, "b", limit=3)
        left = {row[0] for row in self.storage._conn.execute("SELECT token_hash FROM admin_sessions")}
        self.assertEqual(left, {"h3", "h4", "h5"})


class SignOutEverywhereTests(SessionTestBase):
    def test_it_needs_authorisation(self):
        server = self.serve()
        self.assertEqual(self.call(server, "/api/admin/sessions", "DELETE", token=None)[0], 401)
        self.assertEqual(self.call(server, "/api/admin/sessions", "DELETE", token="wrong-" + TOKEN)[0], 401)

    def test_the_admin_token_signs_every_browser_out(self):
        server = self.serve()
        cookies = [self.login(server)[0] for _ in range(3)]
        with self.assertLogs("presto_ai_dash.admin", level="WARNING") as logged:
            status, headers, body = self.call(server, "/api/admin/sessions", "DELETE")
        self.assertEqual((status, body), (200, {"signed_out": 3}))
        self.assertIn("Max-Age=0", headers["Set-Cookie"])
        self.assertTrue(any("(3)" in line for line in logged.output))
        for cookie in cookies:
            self.assertFalse(self.alive(server, cookie))
        # Not a lock-out: the token still works, and can start a new session.
        self.assertEqual(self.call(server, "/api/admin/sources")[0], 200)
        self.assertTrue(self.alive(server, self.login(server)[0]))

    def test_a_browser_session_can_do_it_with_its_csrf_token_and_origin_only(self):
        server = self.serve()
        cookie, session, _ = self.login(server)
        other, _, _ = self.login(server)
        origin = "http://127.0.0.1:%d" % server.actual_port
        self.assertEqual(self.call(server, "/api/admin/sessions", "DELETE", token=None,
                                   cookie=cookie)[0], 403)  # no CSRF token, no origin
        self.assertEqual(self.call(server, "/api/admin/sessions", "DELETE", token=None, cookie=cookie,
                                   csrf="wrong", origin=origin)[0], 403)
        self.assertTrue(self.alive(server, other))
        status, _, body = self.call(server, "/api/admin/sessions", "DELETE", token=None, cookie=cookie,
                                    csrf=session["csrf_token"], origin=origin)
        self.assertEqual((status, body), (200, {"signed_out": 2}))
        self.assertFalse(self.alive(server, cookie))
        self.assertFalse(self.alive(server, other))

    def test_with_nobody_signed_in_it_reports_zero(self):
        server = self.serve()
        self.assertEqual(self.call(server, "/api/admin/sessions", "DELETE")[2], {"signed_out": 0})

    def test_it_ends_sessions_whichever_token_made_them(self):
        server = self.serve(token="startup-code-one-0123456789", generated=True)
        cookie, _, _ = self.login(server, token="startup-code-one-0123456789")
        server = self.restart(server, token="startup-code-two-0123456789", generated=True)
        self.assertTrue(self.alive(server, cookie))  # the generated token's promise
        self.call(server, "/api/admin/sessions", "DELETE", token="startup-code-two-0123456789")
        self.assertFalse(self.alive(server, cookie))

    def test_the_other_session_route_still_ends_only_one(self):
        server = self.serve()
        cookie, session, _ = self.login(server)
        other, _, _ = self.login(server)
        origin = "http://127.0.0.1:%d" % server.actual_port
        status, _, body = self.call(server, "/api/admin/session", "DELETE", token=None, cookie=cookie,
                                    csrf=session["csrf_token"], origin=origin)
        self.assertEqual((status, body), (200, {"logged_out": True}))
        self.assertFalse(self.alive(server, cookie))
        self.assertTrue(self.alive(server, other))


class SecureCookieTests(SessionTestBase):
    def cookie_header(self, headers):
        return headers["Set-Cookie"]

    def test_by_default_the_cookie_is_as_it_always_was(self):
        server = self.serve()
        _, _, headers = self.login(server)
        header = self.cookie_header(headers)
        self.assertTrue(header.startswith("presto_admin_session_%d=" % server.actual_port), header)
        self.assertNotIn("Secure", header)
        for attribute in ("HttpOnly", "SameSite=Strict", "Path=/api/admin"):
            self.assertIn(attribute, header)

    def test_when_asked_the_cookie_is_secure_and_carries_the_secure_prefix(self):
        server = self.serve(secure=True)
        cookie, _, headers = self.login(server)
        header = self.cookie_header(headers)
        self.assertTrue(header.startswith("__Secure-presto_admin_session_%d=" % server.actual_port), header)
        self.assertIn("; Secure", header)
        for attribute in ("HttpOnly", "SameSite=Strict", "Path=/api/admin"):
            self.assertIn(attribute, header)
        self.assertTrue(self.alive(server, cookie))

    def test_a_secure_server_does_not_take_the_plain_cookie_name(self):
        # The prefix is the browser's guarantee that the cookie came over HTTPS.
        # A server that also took the plain name would give that guarantee away.
        plain = self.serve()
        cookie, _, _ = self.login(plain)
        secure = self.restart(plain, secure=True)
        self.assertFalse(self.alive(secure, cookie))

    def test_signing_out_clears_the_cookie_with_the_same_flags(self):
        server = self.serve(secure=True)
        cookie, session, _ = self.login(server)
        origin = "http://127.0.0.1:%d" % server.actual_port
        _, headers, _ = self.call(server, "/api/admin/session", "DELETE", token=None, cookie=cookie,
                                  csrf=session["csrf_token"], origin=origin)
        self.assertIn("Max-Age=0", headers["Set-Cookie"])
        self.assertIn("; Secure", headers["Set-Cookie"])
        self.assertTrue(headers["Set-Cookie"].startswith("__Secure-"))


class ConfigurationTests(unittest.TestCase):
    def load(self, toml="", env=None):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "dash.toml"
            path.write_text(toml, encoding="utf-8")
            clean = {name: value for name, value in os.environ.items()
                     if not name.startswith("DASH_")}
            clean.update(env or {})
            with patch.dict(os.environ, clean, clear=True):
                return load_config(path)

    def test_it_is_off_by_default(self):
        self.assertIs(self.load()["admin"]["secure_cookie"], False)

    def test_the_file_and_the_environment_can_turn_it_on_and_the_environment_wins(self):
        self.assertIs(self.load("[admin]\nsecure_cookie = true\n")["admin"]["secure_cookie"], True)
        self.assertIs(self.load(env={"DASH_ADMIN_SECURE_COOKIE": "yes"})["admin"]["secure_cookie"], True)
        self.assertIs(self.load("[admin]\nsecure_cookie = true\n",
                                {"DASH_ADMIN_SECURE_COOKIE": "0"})["admin"]["secure_cookie"], False)

    def test_a_value_that_is_not_a_boolean_is_refused(self):
        with self.assertRaises(ConfigError):
            self.load('[admin]\nsecure_cookie = "yes"\n')
        with self.assertRaises(ConfigError):
            self.load(env={"DASH_ADMIN_SECURE_COOKIE": "maybe"})

    def test_the_effective_configuration_says_where_the_value_came_from(self):
        environ = {"DASH_ADMIN_SECURE_COOKIE": "1"}
        config = self.load(env=environ)
        rows = effective_settings(config, None, None, environ)
        row = next(item for item in rows if item["name"] == "Admin cookie marked Secure")
        self.assertEqual(row["value"], "yes")
        self.assertEqual(row["origin"], "environment")
        row = next(item for item in effective_settings(self.load(), None, None, {})
                   if item["name"] == "Admin cookie marked Secure")
        self.assertEqual((row["value"], row["origin"]), ("no", "default"))


class DaemonWiringTests(ManagementBase):
    """The daemon hands the admin server what it needs to apply all this."""

    def test_the_admin_server_is_built_with_the_token_kind_and_the_cookie_setting(self):
        from presto_ai_dash.app import Daemon
        for generated, secure in ((True, False), (False, True)):
            with self.subTest(generated=generated, secure=secure):
                config = load_config()
                config["daemon"].update(database=str(Path(self.temp.name) / ("wire-%s.db" % generated)),
                                        host="127.0.0.1", port=0, shared_token="device-token-0123456789")
                config["admin"].update(enabled=True, port=0, secure_cookie=secure,
                                       token="" if generated else TOKEN)
                daemon = Daemon(config)
                self.addCleanup(daemon.stop)
                with patch("presto_ai_dash.app.AdminServer") as admin:
                    daemon.setup()
                kwargs = admin.call_args.kwargs
                self.assertEqual(kwargs["token_generated"], generated)
                self.assertEqual(kwargs["secure_cookie"], secure)


if __name__ == "__main__":
    unittest.main()
