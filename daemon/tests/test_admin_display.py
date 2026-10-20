"""The Home display setting: where it comes from, and how it is changed.

The setting is the maximum number of overflow-strip lines on Presto Home.
The environment beats the admin page's stored value, which beats dash.toml;
one line is the default.
"""

import os
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import presto_path  # noqa: F401  (puts the device modules on the path)
from presto_ai_dash.config import ConfigError, load_config, overflow_rows
from envutil import clean_env
from test_management import AdminServerBase


class OverflowPrecedenceTests(unittest.TestCase):
    """Environment, then the stored value, then the file, then the default."""

    def config(self, overflow=None):
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / "dash.toml"
            if overflow is None:
                path.write_text("", encoding="utf-8")
            else:
                path.write_text("[display]\noverflow_rows = %d\n" % overflow,
                                encoding="utf-8")
            with clean_env():
                return load_config(str(path))

    def test_the_default_is_one_line(self):
        self.assertEqual(overflow_rows(self.config()), 1)
        self.assertEqual(overflow_rows(self.config(), stored=2), 2)

    def test_the_file_is_used_when_nothing_else_is_set(self):
        self.assertEqual(overflow_rows(self.config(3)), 3)
        self.assertEqual(self.config(2)["display"]["overflow_rows"], 2)

    def test_the_stored_value_beats_the_file(self):
        self.assertEqual(overflow_rows(self.config(3), stored=0), 0)
        self.assertEqual(overflow_rows(self.config(1), stored=2), 2)

    def test_the_environment_beats_everything(self):
        with clean_env(DASH_OVERFLOW_ROWS="3"):
            self.assertEqual(overflow_rows(self.config(1), stored=2), 3)
        with clean_env(DASH_OVERFLOW_ROWS="0"):
            self.assertEqual(overflow_rows(self.config(3), stored=2), 0)

    def test_an_unusable_environment_value_fails_closed_to_the_default(self):
        for value in ("seven", "2.5", "-1", "4", ""):
            with self.subTest(value=value):
                with clean_env(DASH_OVERFLOW_ROWS=value):
                    self.assertEqual(overflow_rows(self.config(3), stored=2), 1)

    def test_an_unusable_environment_value_stops_the_daemon_at_startup(self):
        for value in ("seven", "2.5", "-1", "4"):
            with self.subTest(value=value):
                with clean_env(DASH_OVERFLOW_ROWS=value):
                    with self.assertRaises(ConfigError) as caught:
                        load_config()
                self.assertIn("DASH_OVERFLOW_ROWS", str(caught.exception))

    def test_a_bad_file_value_is_refused(self):
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / "dash.toml"
            path.write_text("[display]\noverflow_rows = 7\n", encoding="utf-8")
            with clean_env():
                with self.assertRaises(ConfigError) as caught:
                    load_config(str(path))
            self.assertIn("overflow_rows", str(caught.exception))


class DisplayAdminTests(AdminServerBase):
    """The admin page's Home display control."""

    def test_the_state_is_the_default_and_unlocked(self):
        status, _, state = self.request("/api/admin/display")
        self.assertEqual(status, 200)
        self.assertEqual(state, {"overflow_rows": 1, "locked_by_environment": False})

    def test_the_control_needs_authorization_and_a_valid_body(self):
        self.assertEqual(self.request("/api/admin/display", token=None)[0], 401)
        cookie, session, _ = self.login()
        headers = {"Cookie": cookie, "Origin": self.base_url}
        # A browser session without the CSRF token may not change anything.
        self.assertEqual(self.request("/api/admin/display", "PUT", {"overflow_rows": 2},
                                      token=None, headers=headers)[0], 403)
        headers["X-CSRF-Token"] = session["csrf_token"]
        for data in ("text", {}, {"overflow_rows": "2"}, {"overflow_rows": True},
                     {"overflow_rows": None}, {"overflow_rows": 2, "why": 1},
                     {"overflow_rows": -1}, {"overflow_rows": 4}):
            with self.subTest(data=data):
                self.assertEqual(self.request("/api/admin/display", "PUT", data,
                                              token=None, headers=headers)[0], 400)
        self.assertIsNone(self.storage.overflow_rows_setting())

    def test_the_environment_locks_the_control_read_only(self):
        with clean_env(DASH_OVERFLOW_ROWS="2"):
            status, _, state = self.request("/api/admin/display")
            self.assertEqual(status, 200)
            self.assertEqual(state["locked_by_environment"], True)
            self.assertEqual(state["overflow_rows"], 2)
            self.assertEqual(self.request("/api/admin/display", "PUT",
                                          {"overflow_rows": 3})[0], 409)
        self.assertIsNone(self.storage.overflow_rows_setting())

    def test_a_change_is_stored_and_applies_to_the_next_payload(self):
        status, _, state = self.request("/api/admin/display", "PUT", {"overflow_rows": 3})
        self.assertEqual(status, 200)
        self.assertEqual(state["overflow_rows"], 3)
        self.assertEqual(self.storage.overflow_rows_setting(), 3)
        self.assertEqual(self.manager.status_payload()["display"], {"overflow_rows": 3})
        # A second change replaces the first one.
        self.request("/api/admin/display", "PUT", {"overflow_rows": 0})
        self.assertEqual(self.manager.status_payload()["display"], {"overflow_rows": 0})
        self.assertEqual(self.storage.overflow_rows_setting(), 0)

    def test_the_payload_carries_the_display_settings(self):
        payload = self.manager.status_payload()
        self.assertIn("display", payload)
        self.assertEqual(payload["display"], {"overflow_rows": 1})
        self.request("/api/admin/display", "PUT", {"overflow_rows": 2})
        self.assertEqual(self.manager.status_payload()["display"], {"overflow_rows": 2})

    def test_an_old_device_tolerates_the_new_key(self):
        """The device ignores keys it does not know."""
        from cards import parse_cards
        from net import embedded_payload
        payload = embedded_payload()
        payload["display"] = {"overflow_rows": 2}
        cards, error = parse_cards(payload)
        self.assertIsNone(error)
        self.assertEqual([card.id for card in cards],
                         ["dash", "ollama", "litellm"])

    def test_the_page_wires_the_control(self):
        status, _, html = self.request("/")
        self.assertEqual(status, 200)
        for marker in (b"overflow-rows", b"display-lock", b"Home display"):
            self.assertIn(marker, html)
        self.assertIn(b"Most lines of names for services that do not fit; 0 turns it off.",
                      html)
        _, _, script = self.request("/admin.js")
        self.assertIn(b"/api/admin/display", script)


if __name__ == "__main__":
    unittest.main()
