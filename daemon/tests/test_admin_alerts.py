"""The alert settings: where they come from, and how they are changed.

The settings apply to every service. The admin page's stored value beats
dash.toml, which beats the built-in default. There is no environment
variable: these are the user's own choices.

Synthetic fixtures only: a temporary database, a temporary config file.
"""

from datetime import datetime
import os
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import presto_path  # noqa: F401  (puts the device modules on the path)
from presto_ai_dash.config import ConfigError, alert_settings, load_config
from envutil import clean_env
from test_management import AdminServerBase

DEFAULTS = {"warning_percent": 80, "critical_percent": 95, "sound": True,
            "quiet_hours": False, "quiet_start": "22:00", "quiet_end": "07:00",
            "pop_to_top": True}


def body(**changes):
    values = dict(DEFAULTS)
    values.update(changes)
    return values


class AlertPrecedenceTests(unittest.TestCase):
    """The stored value, then the file, then the default."""

    def config(self, text=""):
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / "dash.toml"
            path.write_text(text, encoding="utf-8")
            with clean_env():
                return load_config(str(path))

    def test_the_defaults_are_used_when_nothing_else_is_set(self):
        settings = alert_settings(self.config())
        for key, value in DEFAULTS.items():
            self.assertEqual(settings[key], value, key)
        self.assertFalse(settings["quiet"])

    def test_the_file_is_used_when_nothing_is_stored(self):
        config = self.config("[alerts]\nwarning_percent = 70\ncritical_percent = 90\n")
        settings = alert_settings(config)
        self.assertEqual(settings["warning_percent"], 70)
        self.assertEqual(settings["critical_percent"], 90)

    def test_the_stored_value_beats_the_file(self):
        config = self.config("[alerts]\nwarning_percent = 70\ncritical_percent = 90\n")
        settings = alert_settings(config, {"warning_percent": 85})
        self.assertEqual(settings["warning_percent"], 85)
        # The other value still comes from the file.
        self.assertEqual(settings["critical_percent"], 90)

    def test_a_stored_value_that_is_broken_falls_back_to_the_default(self):
        settings = alert_settings(self.config(), {"warning_percent": 500,
                                                  "critical_percent": 10})
        self.assertEqual(settings["warning_percent"], 80)
        self.assertEqual(settings["critical_percent"], 95)

    def test_the_quiet_flag_follows_the_local_time(self):
        config = self.config("[alerts]\nquiet_hours = true\n")
        quiet = alert_settings(config, now=datetime(2026, 10, 20, 23, 0))
        self.assertTrue(quiet["quiet"])
        awake = alert_settings(config, now=datetime(2026, 10, 20, 12, 0))
        self.assertFalse(awake["quiet"])

    def test_quiet_hours_off_means_never_quiet(self):
        config = self.config("[alerts]\nquiet_hours = false\n")
        settings = alert_settings(config, now=datetime(2026, 10, 20, 23, 0))
        self.assertFalse(settings["quiet"])

    def test_a_bad_file_value_is_refused(self):
        for text in ("[alerts]\nwarning_percent = 0\n",
                     "[alerts]\ncritical_percent = 101\n",
                     "[alerts]\nwarning_percent = 95\ncritical_percent = 95\n",
                     "[alerts]\nsound = \"yes\"\n",
                     "[alerts]\nquiet_start = \"25:00\"\n"):
            with self.subTest(text=text):
                with self.assertRaises(ConfigError):
                    self.config(text)


class AlertAdminTests(AdminServerBase):
    """The admin page's Alerts section."""

    def test_the_state_is_the_default(self):
        status, _, state = self.request("/api/admin/alerts")
        self.assertEqual(status, 200)
        for key, value in DEFAULTS.items():
            self.assertEqual(state[key], value, key)

    def test_the_control_needs_authorization_and_a_valid_body(self):
        self.assertEqual(self.request("/api/admin/alerts", token=None)[0], 401)
        cookie, session, _ = self.login()
        headers = {"Cookie": cookie, "Origin": self.base_url}
        # A browser session without the CSRF token may not change anything.
        self.assertEqual(self.request("/api/admin/alerts", "PUT", body(),
                                      token=None, headers=headers)[0], 403)
        headers["X-CSRF-Token"] = session["csrf_token"]
        bad_bodies = [
            "text", {}, body(why=1), body(warning_percent="80"),
            body(warning_percent=0), body(warning_percent=101),
            body(critical_percent=0), body(critical_percent=101),
            body(warning_percent=95), body(warning_percent=96),
            body(sound="yes"), body(sound=1), body(quiet_hours="no"),
            body(pop_to_top="no"), body(quiet_start="25:00"),
            body(quiet_end="nonsense"), body(quiet_start=2200),
        ]
        for data in bad_bodies:
            with self.subTest(data=data):
                self.assertEqual(self.request("/api/admin/alerts", "PUT", data,
                                              token=None, headers=headers)[0], 400)
        self.assertEqual(self.storage.alerts_settings(), {})

    def test_a_change_is_stored_and_applies_to_the_next_payload(self):
        status, _, state = self.request("/api/admin/alerts", "PUT",
                                        body(warning_percent=75, critical_percent=92))
        self.assertEqual(status, 200)
        self.assertEqual(state["warning_percent"], 75)
        self.assertEqual(self.storage.alerts_settings(),
                         {"warning_percent": 75, "critical_percent": 92})
        payload = self.manager.status_payload()
        self.assertEqual(payload["alerts"]["sound"], True)
        self.assertEqual(payload["alerts"]["pop_to_top"], True)
        self.assertEqual(payload["alerts"]["quiet"], False)

    def test_the_sound_and_pop_to_top_switches_reach_the_payload(self):
        self.request("/api/admin/alerts", "PUT", body(sound=False, pop_to_top=False))
        payload = self.manager.status_payload()
        self.assertEqual(payload["alerts"], {"quiet": False, "sound": False,
                                              "pop_to_top": False})

    def test_the_quiet_flag_reaches_the_payload(self):
        self.request("/api/admin/alerts", "PUT",
                     body(quiet_hours=True, quiet_start="00:00", quiet_end="23:59"))
        payload = self.manager.status_payload()
        self.assertTrue(payload["alerts"]["quiet"])

    def test_putting_a_value_back_removes_the_stored_one(self):
        """A setting the user put back to the file's value stops overriding it."""
        self.request("/api/admin/alerts", "PUT", body(warning_percent=75))
        self.assertEqual(self.storage.alerts_settings(), {"warning_percent": 75})
        self.request("/api/admin/alerts", "PUT", body(warning_percent=80))
        self.assertEqual(self.storage.alerts_settings(), {})

    def test_the_thresholds_reach_the_status_of_a_source(self):
        """The mapping uses the settings the admin page stored."""
        from datetime import datetime, timedelta, timezone
        from presto_ai_dash.models import SourceSnapshot
        from presto_ai_dash.quotas import quota_report, quota_window

        # A reading that is fresh now: the daemon ages it against its clock.
        now = datetime.now(timezone.utc)
        self.disable_fake()
        saved = self.manager.save({"type": "codex", "name": "Account", "enabled": True,
                                   "config": {}})
        window = quota_window("w", "Weekly", 85, now + timedelta(days=3),
                               now - timedelta(seconds=30), 10080)
        report = quota_report("openai", "Codex", [window], 900)
        self.storage.save_snapshot(SourceSnapshot(saved["id"], "ok", now, {"quota": report}))
        payload = self.manager.status_payload()
        self.assertEqual(payload["sources"][0]["status"], "near_limit")
        self.request("/api/admin/alerts", "PUT", body(warning_percent=90))
        payload = self.manager.status_payload()
        self.assertEqual(payload["sources"][0]["status"], "ok")

    def test_the_admin_page_shows_the_same_status_as_the_device(self):
        """The daemon decides the user-facing status, on the page too."""
        from datetime import datetime, timedelta, timezone
        from presto_ai_dash.models import SourceSnapshot
        from presto_ai_dash.quotas import quota_report, quota_window

        now = datetime.now(timezone.utc)
        self.disable_fake()
        saved = self.manager.save({"type": "codex", "name": "Account", "enabled": True,
                                   "config": {}})
        window = quota_window("w", "Weekly", 85, now + timedelta(days=3),
                               now - timedelta(seconds=30), 10080)
        report = quota_report("openai", "Codex", [window], 900)
        self.storage.save_snapshot(SourceSnapshot(saved["id"], "ok", now, {"quota": report}))
        listing = {row["id"]: row for row in self.manager.list_sources()["sources"]}
        self.assertEqual(listing[saved["id"]]["status"], "near_limit")
        self.request("/api/admin/alerts", "PUT", body(warning_percent=90))
        listing = {row["id"]: row for row in self.manager.list_sources()["sources"]}
        self.assertEqual(listing[saved["id"]]["status"], "ok")

    def test_the_effective_configuration_names_the_origin(self):
        status, _, body_json = self.request("/api/admin/config")
        self.assertEqual(status, 200)
        rows = {entry["name"]: entry for entry in body_json["settings"]}
        self.assertEqual(rows["Alert warning percent"]["value"], 80)
        self.assertEqual(rows["Alert warning percent"]["origin"], "default")
        self.assertEqual(rows["Alert sound"]["value"], "on")
        self.assertEqual(rows["Quiet hours"]["value"], "off")
        self.assertEqual(rows["Pop to top on alarm"]["value"], "on")
        self.request("/api/admin/alerts", "PUT", body(warning_percent=75))
        _, _, body_json = self.request("/api/admin/config")
        rows = {entry["name"]: entry for entry in body_json["settings"]}
        self.assertEqual(rows["Alert warning percent"]["value"], 75)
        self.assertEqual(rows["Alert warning percent"]["origin"], "database")
        # The other settings keep their own origin.
        self.assertEqual(rows["Alert critical percent"]["origin"], "default")

    def test_the_page_wires_the_controls(self):
        status, _, html = self.request("/")
        self.assertEqual(status, 200)
        for marker in (b"alert-warning", b"alert-critical", b"alert-sound",
                       b"alert-quiet", b"alert-pop", b"Alerts"):
            self.assertIn(marker, html)
        self.assertIn(b"Applies to all services", html)
        _, _, script = self.request("/admin.js")
        self.assertIn(b"/api/admin/alerts", script)


if __name__ == "__main__":
    unittest.main()
