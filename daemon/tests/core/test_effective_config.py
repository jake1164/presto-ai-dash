"""The effective daemon configuration, and where each value came from.

Every value says whether it was decided by the environment, by ``dash.toml``
or by the database, so the admin page can explain a setting that needs a
restart without changing it.
"""

from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from envutil import clean_env
from presto_ai_dash.config import ConfigError, load_config
from presto_ai_dash.effective import (DATABASE, DEFAULT, ENVIRONMENT, FILE,
                                      effective_settings, uptime_label, uptime_seconds)
from admin.test_management import ManagementBase


class EffectiveSettingsTests(ManagementBase):
    """Environment, then the file, then the database, then the default."""

    def setUp(self):
        super().setUp()
        self.path = Path(self.temp.name) / "dash.toml"
        self.path.write_text("", encoding="utf-8")

    def settings(self, file_text="", environ=None):
        """The effective settings for a bootstrap file and an environment."""
        self.path.write_text(file_text, encoding="utf-8")
        with clean_env(**(environ or {})):
            try:
                config = load_config(str(self.path))
            except ConfigError:
                # An unreadable file leaves the built-in defaults in force.
                config = load_config()
        return {entry["name"]: entry
                for entry in effective_settings(config, self.storage,
                                                str(self.path), environ or {})}

    def test_the_poll_interval_and_the_retention_come_from_the_file(self):
        settings = self.settings("[daemon]\npoll_interval = 42\nretention_hours = 12\n")
        self.assertEqual(settings["Poll interval"]["value"], 42)
        self.assertEqual(settings["Poll interval"]["origin"], FILE)
        self.assertEqual(settings["Retention"]["value"], 12)
        self.assertEqual(settings["Retention"]["origin"], FILE)

    def test_a_value_nobody_set_has_the_default_origin(self):
        settings = self.settings()
        self.assertEqual(settings["Poll interval"]["origin"], DEFAULT)
        self.assertEqual(settings["Poll interval"]["value"], 15)

    def test_the_environment_beats_the_file(self):
        settings = self.settings("[daemon]\nport = 9000\n", {"DASH_PORT": "9100"})
        self.assertEqual(settings["Daemon port"]["value"], 9100)
        self.assertEqual(settings["Daemon port"]["origin"], ENVIRONMENT)

    def test_the_file_beats_the_default(self):
        settings = self.settings("[daemon]\nport = 9000\n")
        self.assertEqual(settings["Daemon port"]["value"], 9000)
        self.assertEqual(settings["Daemon port"]["origin"], FILE)

    def test_the_database_decides_the_stored_settings(self):
        self.storage.save_overflow_rows(3)
        self.storage.save_ingest_enabled(True)
        settings = self.settings()
        self.assertEqual(settings["Home overflow strip lines"]["value"], 3)
        self.assertEqual(settings["Home overflow strip lines"]["origin"], DATABASE)
        self.assertEqual(settings["Remote reports (ingest)"]["value"], "on")
        self.assertEqual(settings["Remote reports (ingest)"]["origin"], DATABASE)

    def test_the_environment_beats_the_database(self):
        self.storage.save_overflow_rows(3)
        settings = self.settings(environ={"DASH_OVERFLOW_ROWS": "1"})
        self.assertEqual(settings["Home overflow strip lines"]["value"], 1)
        self.assertEqual(settings["Home overflow strip lines"]["origin"], ENVIRONMENT)

    def test_the_timezone_is_reported_with_its_offset(self):
        settings = self.settings()
        self.assertIn("UTC", settings["Timezone"]["value"])
        self.assertEqual(settings["Timezone"]["origin"], ENVIRONMENT)

    def test_the_timezone_row_reflects_the_tz_variable(self):
        # A container runs in UTC unless TZ says otherwise, and the row has
        # to show the zone the quiet hours and the reset times use.
        settings = self.settings(environ={"TZ": "UTC+05:30"})
        self.assertIn("UTC+05:30", settings["Timezone"]["value"])
        self.assertEqual(settings["Timezone"]["origin"], ENVIRONMENT)
        settings = self.settings(environ={"TZ": "UTC"})
        self.assertIn("UTC", settings["Timezone"]["value"])

    def test_no_token_is_ever_published(self):
        settings = self.settings("[daemon]\nshared_token = \"secret-token\"\n")
        for entry in settings.values():
            self.assertNotIn("secret-token", str(entry["value"]))

    def test_an_unreadable_file_leaves_the_values_alone(self):
        settings = self.settings("this is not TOML at all")
        self.assertEqual(settings["Poll interval"]["origin"], DEFAULT)


class UptimeTests(unittest.TestCase):
    def test_the_uptime_is_readable(self):
        self.assertEqual(uptime_label(30), "30 s")
        self.assertEqual(uptime_label(120), "2 min")
        self.assertEqual(uptime_label(3700), "1 h 1 min")
        self.assertEqual(uptime_label(200000), "2 d 7 h")

    def test_a_future_start_counts_as_no_uptime(self):
        self.assertEqual(uptime_seconds(100.0, now=50.0), 0)


if __name__ == "__main__":
    unittest.main()


class AdminTokenRowTests(unittest.TestCase):
    """The page says whether the admin token lasts, and never shows it."""

    def rows(self, admin, environ=None):
        from presto_ai_dash.config import resolve_admin_token
        config = {"daemon": {}, "admin": dict(admin), "logging": {}}
        if admin.get("enabled"):
            resolve_admin_token(config, "shared")
        entries = effective_settings(config, None, None, environ or {})
        return {entry["name"]: entry for entry in entries}

    def test_a_generated_token_is_called_temporary_and_not_shown(self):
        from presto_ai_dash.config import resolve_admin_token
        config = {"daemon": {}, "admin": {"enabled": True, "token": ""}, "logging": {}}
        token = resolve_admin_token(config, "shared")
        entries = effective_settings(config, None, None, {})
        entry = next(item for item in entries if item["name"] == "Admin token")
        self.assertEqual(entry["origin"], "generated at start")
        self.assertIn("temporary", entry["value"])
        self.assertNotIn(token, repr(entries))

    def test_a_configured_token_says_set(self):
        entry = self.rows({"enabled": True, "token": "chosen-token"})["Admin token"]
        self.assertEqual((entry["value"], entry["origin"]), ("set", FILE))

    def test_the_environment_token_says_so(self):
        entry = self.rows({"enabled": True, "token": "x"},
                          {"DASH_ADMIN_TOKEN": "x"})["Admin token"]
        self.assertEqual(entry["origin"], ENVIRONMENT)

    def test_the_generated_token_is_flagged_not_logged_at_resolve_time(self):
        # The daemon announces it as the last startup lines instead.
        from presto_ai_dash.config import resolve_admin_token
        config = {"admin": {"token": ""}}
        with self.assertNoLogs("presto_ai_dash.config", level="DEBUG"):
            token = resolve_admin_token(config, "shared")
        self.assertTrue(config["admin"]["token_generated"])
        self.assertEqual(config["admin"]["token"], token)
