"""The read-only endpoints behind the Status tab.

``/api/admin/status`` carries the daemon's own state, the clients that poll
it, the health of every account, the local readers, the remote collectors and
the recent problems. ``/api/admin/config`` carries the effective configuration
and where each value came from. Both are GET only, and they change nothing.
"""

import json
import logging
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from presto_ai_dash.models import SourceSnapshot
from presto_ai_dash import __version__
from presto_ai_dash.storage import Storage
from admin.test_management import AdminServerBase


class StatusEndpointTests(AdminServerBase):
    """The Status tab's own endpoint."""

    def setUp(self):
        super().setUp()
        # The problems buffer is attached to the log by the daemon; here it is
        # attached by hand so a logged warning can be seen.
        logging.getLogger().addHandler(self.manager.problems)
        self.addCleanup(logging.getLogger().removeHandler, self.manager.problems)

    def test_it_needs_authorization(self):
        self.assertEqual(self.request("/api/admin/status", token=None)[0], 401)
        self.assertEqual(self.request("/api/admin/status", token="wrong")[0], 401)
        self.assertEqual(self.request("/api/admin/status")[0], 200)

    def test_a_browser_session_may_read_it(self):
        cookie, _, _ = self.login()
        status, _, body = self.request("/api/admin/status", token=None,
                                       headers={"Cookie": cookie})
        self.assertEqual(status, 200)
        self.assertIn("daemon", body)

    def test_it_is_read_only(self):
        for method in ("POST", "PUT", "DELETE"):
            with self.subTest(method=method):
                self.assertEqual(self.request("/api/admin/status", method, {})[0], 404)

    def test_the_daemon_block_names_the_version_schema_uptime_and_poll_interval(self):
        status, _, body = self.request("/api/admin/status")
        self.assertEqual(status, 200)
        daemon = body["daemon"]
        self.assertEqual(daemon["version"], __version__)
        self.assertEqual(daemon["schema_version"], Storage.SCHEMA_VERSION)
        self.assertGreaterEqual(daemon["uptime_seconds"], 0)
        self.assertIn("uptime", daemon)
        self.assertEqual(daemon["poll_interval"], self.config["daemon"]["poll_interval"])

    def test_the_accounts_carry_their_health(self):
        self.assertEqual(self.request("/api/admin/status")[0], 200)
        status, _, body = self.request("/api/admin/status")
        accounts = body["accounts"]
        self.assertTrue(accounts)
        for account in accounts:
            for key in ("id", "name", "type", "enabled", "status", "last_collection",
                        "last_problem"):
                self.assertIn(key, account)
        names = [account["name"] for account in accounts]
        # The seeded accounts are the source types dash.toml can configure;
        # the removed demo source is not one of them.
        self.assertIn("Ollama", names)
        self.assertNotIn("Fake", names)

    def test_an_account_without_a_reading_says_so(self):
        _, _, body = self.request("/api/admin/status")
        waiting = [account for account in body["accounts"]
                   if account["last_collection"] is None]
        self.assertTrue(waiting)
        self.assertEqual(waiting[0]["last_problem"], "no reading yet")

    def test_an_account_problem_is_reported_in_plain_words(self):
        row = self.manager.save({"type": "litellm", "name": "Gateway",
                                  "enabled": True,
                                  "config": {"base_url": "http://localhost:4000"}})
        self.storage.save_snapshot(SourceSnapshot(row["id"], "offline",
                                                  error="the provider refused the key"))
        _, _, body = self.request("/api/admin/status")
        account = next(item for item in body["accounts"] if item["id"] == row["id"])
        self.assertEqual(account["last_problem"], "the provider refused the key")
        self.assertEqual(account["status"], "offline")

    def test_the_clients_are_listed(self):
        self.manager.clients.record("presto", "10.0.0.9", "abc123")
        _, _, body = self.request("/api/admin/status")
        clients = body["clients"]
        self.assertEqual(len(clients), 1)
        self.assertEqual(clients[0]["name"], "presto")
        self.assertEqual(clients[0]["address"], "10.0.0.9")
        self.assertEqual(clients[0]["revision"], "abc123")
        self.assertTrue(clients[0]["connected"])

    def test_a_client_that_stopped_polling_is_not_connected(self):
        self.manager.clients.record("presto", "10.0.0.9", "abc123",
                                    now=1000.0)
        self.manager.clients._clock = lambda: 1000.0 + 10 ** 6
        _, _, body = self.request("/api/admin/status")
        self.assertFalse(body["clients"][0]["connected"])

    def test_the_clients_carry_their_diagnostics(self):
        self.manager.clients.record("presto", "10.0.0.9", "abc123",
                                    diag="err=OSError 110;st=conn;n=3;free=41")
        _, _, body = self.request("/api/admin/status")
        diag = body["clients"][0]["diag"]
        self.assertEqual(diag["err"], "OSError 110")
        self.assertEqual(diag["st"], "conn")
        self.assertEqual(diag["n"], 3)
        self.assertEqual(diag["free"], 41)

    def test_a_client_without_diagnostics_carries_an_empty_dict(self):
        self.manager.clients.record("presto", "10.0.0.9", "abc123")
        _, _, body = self.request("/api/admin/status")
        self.assertEqual(body["clients"][0]["diag"], {})

    def test_a_hostile_diagnostics_header_never_reaches_the_page(self):
        self.manager.clients.record("presto", "10.0.0.9", "abc123",
                                    diag="err=<script>alert(1)</script>;secret=token")
        _, _, body = self.request("/api/admin/status")
        self.assertEqual(body["clients"][0]["diag"], {})
        self.assertNotIn("script", json.dumps(body))

    def test_the_readers_and_the_collectors_are_listed(self):
        _, _, body = self.request("/api/admin/status")
        self.assertIsInstance(body["readers"], list)
        self.assertIsInstance(body["collectors"], list)

    def test_a_claude_reader_is_reported(self):
        row = self.manager.save({"type": "claude", "name": "Claude",
                                  "enabled": True,
                                  "config": {"settings_path": str(Path(self.temp.name) / "s.json"),
                                             "activity_logs": True}})
        report = {"provider": "anthropic", "product": "Claude",
                  "windows": [], "stale_after_seconds": 900, "error": None,
                  "local_activity": {"available": False, "reason": "no projects directory",
                                     "complete": True}}
        self.storage.save_snapshot(SourceSnapshot(row["id"], "warning",
                                                  metrics={"quota": report}))
        _, _, body = self.request("/api/admin/status")
        reader = next(item for item in body["readers"]
                      if item["reader"] == "local activity")
        self.assertEqual(reader["state"], "unavailable")
        self.assertIn("no projects directory", reader["detail"])

    def test_a_remote_collector_is_reported(self):
        row = self.manager.save({"type": "claude", "name": "Claude",
                                  "enabled": True,
                                  "config": {"settings_path": str(Path(self.temp.name) / "s.json")}})
        self.assertEqual(self.request("/api/admin/status")[0], 200)
        _, _, body = self.request("/api/admin/status")
        collector = next(item for item in body["collectors"]
                         if item["account"] == "Claude")
        self.assertEqual(collector["state"], "no reports yet")

    def test_the_problems_are_listed(self):
        logger = logging.getLogger("presto_ai_dash.test_status")
        logger.warning("a problem worth seeing")
        _, _, body = self.request("/api/admin/status")
        messages = [problem["message"] for problem in body["problems"]]
        self.assertIn("a problem worth seeing", messages)

    def test_no_secret_is_published(self):
        row = self.manager.save({"type": "litellm", "name": "Gateway",
                                  "enabled": True, "api_key": "test-provider-key",
                                  "config": {"base_url": "http://localhost:4000"}})
        self.assertIsNotNone(row)
        _, _, body = self.request("/api/admin/status")
        self.assertNotIn("test-provider-key", json.dumps(body))


class EffectiveConfigEndpointTests(AdminServerBase):
    """The read-only effective configuration."""

    def test_it_needs_authorization_and_is_read_only(self):
        self.assertEqual(self.request("/api/admin/config", token=None)[0], 401)
        self.assertEqual(self.request("/api/admin/config")[0], 200)
        for method in ("POST", "PUT", "DELETE"):
            with self.subTest(method=method):
                self.assertEqual(self.request("/api/admin/config", method, {})[0], 404)

    def test_every_setting_names_its_origin(self):
        _, _, body = self.request("/api/admin/config")
        settings = body["settings"]
        self.assertTrue(settings)
        for entry in settings:
            self.assertIn(entry["origin"], ("environment", "dash.toml", "database", "default"))
            self.assertIn("name", entry)
            self.assertIn("value", entry)

    def test_the_poll_interval_and_the_ports_are_there(self):
        _, _, body = self.request("/api/admin/config")
        names = [entry["name"] for entry in body["settings"]]
        for expected in ("Poll interval", "Retention", "Daemon host", "Daemon port",
                         "Admin port", "Timezone"):
            self.assertIn(expected, names)

    def test_no_token_is_published(self):
        _, _, body = self.request("/api/admin/config")
        self.assertNotIn("unit-secret-admin-token", json.dumps(body))


class PageWiringTests(AdminServerBase):
    """The page asks for the two new endpoints."""

    def test_the_script_reads_the_status_and_the_configuration(self):
        status, _, script = self.request("/admin.js")
        self.assertEqual(status, 200)
        self.assertIn(b"/api/admin/status", script)
        self.assertIn(b"/api/admin/config", script)

    def test_the_page_has_the_three_tabs(self):
        status, _, html = self.request("/")
        self.assertEqual(status, 200)
        for marker in (b'role="tablist"', b'role="tab"', b'role="tabpanel"'):
            self.assertIn(marker, html)
        for name in (b"services", b"status", b"settings"):
            self.assertIn(b'id="tab-' + name + b'"', html)
            self.assertIn(b'id="panel-' + name + b'"', html)

    def test_the_script_keeps_the_tab_in_the_hash_and_in_local_storage(self):
        status, _, script = self.request("/admin.js")
        self.assertEqual(status, 200)
        for marker in (b"#services", b"#status", b"#settings", b"localStorage",
                       b"try {", b"catch (_)"):
            self.assertIn(marker, script)


class ClientDiagnosticsPageTests(AdminServerBase):
    """The Status tab shows what the device reported about itself."""

    def script(self):
        status, _, script = self.request("/admin.js")
        self.assertEqual(status, 200)
        return script.decode("utf-8")

    def test_the_table_has_the_diagnostics_columns(self):
        script = self.script()
        self.assertIn("buildClientTable", script)
        for column in ("Last error", "Failures", "Free", "Signal", "Uptime"):
            self.assertIn('"%s"' % column, script)

    def test_a_missing_value_is_a_dash_and_never_undefined(self):
        script = self.script()
        self.assertIn("NO_VALUE", script)
        self.assertIn("undefined", script)  # the guard, not a printed value
        # The cells are built from the helpers, so nothing says "undefined".
        for call in ("diagValue(diag, \"n\")", "diagWithUnit(diag, \"free\", \"KB\")",
                     "diagWithUnit(diag, \"rssi\", \"dBm\")", "diagUptime(diag.up)",
                     "diagError(diag)"):
            self.assertIn(call, script)

    def test_the_help_note_says_a_silent_device_must_be_checked_otherwise(self):
        script = self.script()
        self.assertIn("A device that cannot reach the daemon cannot report a problem here",
                      script)
        self.assertIn("Last seen", script)

    def test_the_values_are_never_written_as_html(self):
        script = self.script()
        self.assertNotIn("innerHTML", script)
        self.assertIn("textContent", script)


if __name__ == "__main__":
    unittest.main()
