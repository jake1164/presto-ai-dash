"""Per-service alert settings, the master switch, and how they are resolved.

The settings live in their own rows, keyed by the source instance id: they
are not source configuration fields, because editing a source rebuilds it
and rescans its logs, and a toggle must never do that.

Synthetic fixtures only: a temporary database, a temporary config file.
"""

import os
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import presto_path  # noqa: F401  (puts the device modules on the path)
from presto_ai_dash.alert_policy import resolve_alert_settings
from envutil import clean_env
from test_management import AdminServerBase, ManagementBase

GLOBAL = {"enabled": True, "warning_percent": 80, "critical_percent": 95}


class ResolutionTests(unittest.TestCase):
    """The pure resolution: master, service switch, inherited thresholds."""

    def test_a_service_without_settings_inherits_the_global_values(self):
        result = resolve_alert_settings(GLOBAL, None)
        self.assertTrue(result["enabled"])
        self.assertEqual(result["warning_percent"], 80)
        self.assertEqual(result["critical_percent"], 95)
        self.assertFalse(result["override_ignored"])

    def test_the_service_own_thresholds_win(self):
        result = resolve_alert_settings(GLOBAL, {"warning_percent": 50,
                                                 "critical_percent": 60})
        self.assertEqual(result["warning_percent"], 50)
        self.assertEqual(result["critical_percent"], 60)
        self.assertFalse(result["override_ignored"])

    def test_one_inherited_threshold_and_one_own(self):
        result = resolve_alert_settings(GLOBAL, {"warning_percent": 50})
        self.assertEqual(result["warning_percent"], 50)
        self.assertEqual(result["critical_percent"], 95)

    def test_the_master_switch_governs_every_service(self):
        result = resolve_alert_settings(dict(GLOBAL, enabled=False), None)
        self.assertFalse(result["enabled"])
        self.assertFalse(result["master_enabled"])
        # A service that is on itself still does not alert.
        result = resolve_alert_settings(dict(GLOBAL, enabled=False),
                                        {"enabled": True})
        self.assertFalse(result["enabled"])

    def test_a_service_that_is_off_does_not_alert(self):
        result = resolve_alert_settings(GLOBAL, {"enabled": False})
        self.assertFalse(result["enabled"])
        self.assertFalse(result["service_enabled"])

    def test_an_unusable_pair_falls_back_to_the_global_one(self):
        # The service set a warning that is not below the critical one.
        result = resolve_alert_settings(GLOBAL, {"warning_percent": 95})
        self.assertEqual(result["warning_percent"], 80)
        self.assertEqual(result["critical_percent"], 95)
        self.assertTrue(result["override_ignored"])

    def test_a_global_change_can_make_a_service_override_unusable(self):
        global_settings = dict(GLOBAL, warning_percent=90, critical_percent=95)
        # The service set a warning of 95, which the global critical
        # percent now forbids.
        result = resolve_alert_settings(global_settings, {"warning_percent": 95})
        self.assertEqual(result["warning_percent"], 90)
        self.assertEqual(result["critical_percent"], 95)
        self.assertTrue(result["override_ignored"])

    def test_junk_is_treated_as_the_default(self):
        result = resolve_alert_settings({"enabled": "yes", "warning_percent": "80",
                                         "critical_percent": None}, {"enabled": 1})
        self.assertTrue(result["enabled"])
        self.assertEqual(result["warning_percent"], 80)
        self.assertEqual(result["critical_percent"], 95)


class ServiceAlertStorageTests(ManagementBase):
    """The settings are stored apart from the source configuration."""

    def test_a_setting_round_trips(self):
        row = self.add_litellm()
        self.assertIsNone(self.storage.service_alert_settings(row["id"]))
        self.storage.save_service_alert_settings(
            row["id"], {"enabled": False, "warning_percent": 50})
        self.assertEqual(self.storage.service_alert_settings(row["id"]),
                         {"enabled": False, "warning_percent": 50})

    def test_a_switch_alone_keeps_the_thresholds(self):
        row = self.add_litellm()
        self.storage.save_service_alert_settings(
            row["id"], {"enabled": False, "warning_percent": 50,
                        "critical_percent": 60})
        self.storage.save_service_alert_settings(row["id"], {"enabled": True})
        self.assertEqual(self.storage.service_alert_settings(row["id"]),
                         {"enabled": True, "warning_percent": 50,
                          "critical_percent": 60})

    def test_the_settings_go_when_the_service_is_deleted(self):
        row = self.add_litellm()
        self.storage.save_service_alert_settings(row["id"], {"enabled": False})
        self.assertIsNotNone(self.storage.service_alert_settings(row["id"]))
        self.manager.delete(row["id"], row["revision"])
        self.assertIsNone(self.storage.service_alert_settings(row["id"]))
        self.assertNotIn(row["id"], self.storage.service_alert_settings_all())

    def test_the_settings_survive_an_edit_and_a_rename(self):
        row = self.add_litellm("Before")
        self.storage.save_service_alert_settings(row["id"], {"enabled": False,
                                                             "warning_percent": 42})
        edited = self.manager.save({"revision": row["revision"], "name": "After"},
                                    row["id"])
        self.assertEqual(self.storage.service_alert_settings(row["id"]),
                         {"enabled": False, "warning_percent": 42})
        self.assertEqual(edited["name"], "After")

    def test_a_switch_does_not_rebuild_the_source(self):
        """A toggle must never rebuild the source or reset its readers."""
        row = self.add_litellm()
        self.manager.changed.clear()
        self.manager.set_service_alert_enabled(row["id"], False)
        self.assertFalse(self.manager.changed.is_set(),
                         "the collector was woken up for an alert switch")
        current = next(item for item in self.storage.source_instances()
                       if item["id"] == row["id"])
        self.assertEqual(current["revision"], row["revision"])
        self.assertEqual(current["config"], row["config"])

    def test_a_service_that_does_not_exist_has_no_settings(self):
        self.assertIsNone(self.storage.service_alert_settings("nope"))
        self.assertEqual(self.storage.service_alert_settings_all(), {})

    def test_a_broken_stored_value_is_ignored(self):
        row = self.add_litellm()
        self.storage.save_service_alert_settings(row["id"], {"enabled": False})
        self.storage._conn.execute(
            "UPDATE configuration_meta SET value = ? WHERE name = ?",
            (b"not json", "alerts.service." + row["id"]))
        self.assertIsNone(self.storage.service_alert_settings(row["id"]))


class ServiceAlertAdminTests(AdminServerBase):
    """The admin page's per-service alert controls."""

    def add_service(self):
        self.disable_fake()
        return self.add_litellm("Gateway", enabled=True)

    def test_the_state_is_read(self):
        row = self.add_service()
        status, _, state = self.request("/api/admin/sources/%s/alerts" % row["id"])
        self.assertEqual(status, 200)
        self.assertEqual(state["enabled"], True)
        self.assertIsNone(state["warning_percent"])
        self.assertIsNone(state["critical_percent"])
        self.assertEqual(state["effective"]["warning_percent"], 80)
        self.assertEqual(state["effective"]["critical_percent"], 95)
        self.assertFalse(state["override_ignored"])
        self.assertFalse(state["master_off"])

    def test_the_control_needs_authorization(self):
        row = self.add_service()
        path = "/api/admin/sources/%s/alerts" % row["id"]
        self.assertEqual(self.request(path, token=None)[0], 401)
        self.assertEqual(self.request(path, token="wrong")[0], 401)

    def test_a_browser_session_may_not_write_without_the_csrf_token(self):
        row = self.add_service()
        path = "/api/admin/sources/%s/alerts" % row["id"]
        cookie, session, _ = self.login()
        headers = {"Cookie": cookie, "Origin": self.base_url}
        self.assertEqual(self.request(path, "PUT", {"enabled": False},
                                      token=None, headers=headers)[0], 403)
        headers["X-CSRF-Token"] = session["csrf_token"]
        status, _, state = self.request(path, "PUT", {"enabled": False},
                                        token=None, headers=headers)
        self.assertEqual(status, 200)
        self.assertFalse(state["enabled"])

    def test_an_unknown_service_is_not_found(self):
        self.assertEqual(self.request("/api/admin/sources/nope/alerts")[0], 404)
        self.assertEqual(self.request("/api/admin/sources/nope/alerts", "PUT",
                                      {"enabled": False})[0], 404)

    def test_invalid_values_are_refused(self):
        row = self.add_service()
        path = "/api/admin/sources/%s/alerts" % row["id"]
        for data in ({"enabled": "yes"}, {"enabled": 1}, {"warning_percent": 0},
                     {"warning_percent": 101}, {"warning_percent": "80"},
                     {"critical_percent": 0}, {"critical_percent": 101},
                     {"warning": 50}, {"enabled": False, "warning_percent": 95},
                     {"enabled": False, "critical_percent": 80},
                     {"enabled": False, "warning_percent": 95,
                      "critical_percent": 90}):
            with self.subTest(data=data):
                self.assertEqual(self.request(path, "PUT", data)[0], 400)
        self.assertIsNone(self.storage.service_alert_settings(row["id"]))

    def test_a_threshold_pair_is_checked_against_the_global_one(self):
        """A service that inherits the critical percent must stay below it."""
        row = self.add_service()
        path = "/api/admin/sources/%s/alerts" % row["id"]
        # The global critical percent is 95, so a warning of 95 is unusable.
        self.assertEqual(self.request(path, "PUT",
                                      {"warning_percent": 95})[0], 400)
        self.assertEqual(self.request(path, "PUT",
                                      {"warning_percent": 90,
                                       "critical_percent": 99})[0], 200)

    def test_the_settings_are_stored_and_reach_the_payload(self):
        row = self.add_service()
        path = "/api/admin/sources/%s/alerts" % row["id"]
        status, _, state = self.request(path, "PUT",
                                        {"enabled": False, "warning_percent": 50})
        self.assertEqual(status, 200)
        self.assertFalse(state["enabled"])
        self.assertEqual(state["warning_percent"], 50)
        payload = self.manager.status_payload()
        source = next(s for s in payload["sources"] if s["id"] == row["id"])
        self.assertFalse(source["alerts"])

    def test_a_service_with_its_alerts_off_does_not_count_towards_the_overall_status(self):
        """It is excluded from the overall accent, but its own card is unchanged."""
        from datetime import datetime, timedelta, timezone

        from presto_ai_dash.models import SourceSnapshot
        from presto_ai_dash.quotas import quota_report, quota_window

        now = datetime.now(timezone.utc)
        row = self.add_service()
        window = quota_window("w", "Weekly", 85, now + timedelta(days=3),
                               now - timedelta(seconds=30), 10080)
        report = quota_report("openai", "Codex", [window], 900)
        self.storage.save_snapshot(SourceSnapshot(row["id"], "ok", now,
                                                  {"quota": report}))
        payload = self.manager.status_payload()
        self.assertEqual(payload["overall_status"], "near_limit")
        # The service's own card keeps its true status.
        card = next(card for card in payload["cards"] if card["id"] == row["id"])
        self.assertEqual(card["status"], "near_limit")

        self.manager.set_service_alert_enabled(row["id"], False)
        payload = self.manager.status_payload()
        self.assertEqual(payload["overall_status"], "ok")
        card = next(card for card in payload["cards"] if card["id"] == row["id"])
        self.assertEqual(card["status"], "near_limit",
                         "turning the alerts off never hides a number")
        source = next(s for s in payload["sources"] if s["id"] == row["id"])
        self.assertFalse(source["alerts"])

    def test_the_master_switch_is_off_for_everybody(self):
        row = self.add_service()
        path = "/api/admin/sources/%s/alerts" % row["id"]
        self.request("/api/admin/alerts", "PUT", self.alerts_body(enabled=False))
        _status, _, state = self.request(path)
        self.assertTrue(state["master_off"])
        self.assertFalse(state["effective"]["enabled"])
        payload = self.manager.status_payload()
        self.assertFalse(payload["alerts"]["enabled"])
        source = next(s for s in payload["sources"] if s["id"] == row["id"])
        self.assertFalse(source["alerts"])

    def test_the_global_settings_gain_the_master_switch(self):
        status, _, state = self.request("/api/admin/alerts")
        self.assertEqual(status, 200)
        self.assertTrue(state["enabled"])
        status, _, state = self.request("/api/admin/alerts", "PUT",
                                        self.alerts_body(enabled=False))
        self.assertEqual(status, 200)
        self.assertFalse(state["enabled"])
        self.assertEqual(self.storage.alerts_settings(), {"enabled": False})

    def alerts_body(self, **changes):
        body = {"enabled": True, "warning_percent": 80, "critical_percent": 95,
                "sound": True, "quiet_hours": False, "quiet_start": "22:00",
                "quiet_end": "07:00", "pop_to_top": True}
        body.update(changes)
        return body

    def test_the_effective_configuration_names_the_master(self):
        status, _, body = self.request("/api/admin/config")
        self.assertEqual(status, 200)
        rows = {entry["name"]: entry for entry in body["settings"]}
        self.assertEqual(rows["Alerts master"]["value"], "on")
        self.assertEqual(rows["Alerts master"]["origin"], "default")
        self.request("/api/admin/alerts", "PUT", self.alerts_body(enabled=False))
        _status, _, body = self.request("/api/admin/config")
        rows = {entry["name"]: entry for entry in body["settings"]}
        self.assertEqual(rows["Alerts master"]["value"], "off")
        self.assertEqual(rows["Alerts master"]["origin"], "database")


if __name__ == "__main__":
    unittest.main()
