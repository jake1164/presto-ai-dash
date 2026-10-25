"""Claude quota normalization, conservative freshness, DB delivery and UI setup."""

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from presto_ai_dash.claude_export import build_envelope, normalized_export, helper_command
from presto_ai_dash.ingest import ingest
from presto_ai_dash.management import ManagedCollector, SourceConfiguration
from presto_ai_dash.models import SourceSnapshot
from presto_ai_dash.quotas import present_quota
from presto_ai_dash.sources import source_catalog
from presto_ai_dash.storage import Storage, ConfigurationConflict
from admin.test_management import ManagementBase
from admin import test_management

NOW = datetime(2026, 10, 11, tzinfo=timezone.utc)


def payload(percent=23.5, minutes=0):
    return {"session_id": "private-session-identity", "prompt_id": "private-prompt",
            "transcript_path": "/private/transcript", "version": "2.1.296",
            "cost": {"total_api_duration_ms": 300, "total_duration_ms": 1000},
            "context_window": {"total_input_tokens": 600, "total_output_tokens": 100},
            "rate_limits": {"five_hour": {"used_percentage": percent,
                "resets_at": int((NOW + timedelta(hours=5)).timestamp())},
                "seven_day": {"used_percentage": 41.2,
                "resets_at": int((NOW + timedelta(days=7)).timestamp())}}}


class ClaudeParsingTests(unittest.TestCase):
    def test_exact_windows_reset_epoch_and_private_fields_omitted(self):
        report, evidence = normalized_export(payload(), NOW)
        self.assertEqual([w["label"] for w in report["windows"]], ["5h", "Weekly"])
        self.assertEqual([w["used_percent"] for w in report["windows"]], [23.5, 41.2])
        self.assertEqual(report["windows"][0]["resets_at"], (NOW + timedelta(hours=5)).isoformat())
        self.assertEqual(report["collector_version"], "2.1.296")
        self.assertNotIn("private", json.dumps([report, evidence]))

    def test_missing_fields_context_and_spend_are_not_subscription_zeroes(self):
        self.assertIsNone(normalized_export({"context_window": {"used_percentage": 90}}, NOW))
        data = payload()
        data["rate_limits"].pop("seven_day")
        data["rate_limits"]["spend_limit"] = {"used_percentage": 80}
        report, _ = normalized_export(data, NOW)
        self.assertIsNone(report["windows"][1]["used_percent"])
        self.assertEqual(report["windows"][1]["state"], "unavailable")
        self.assertEqual(len(report["windows"]), 2)

    def test_bad_quota_schema_and_values_are_rejected(self):
        cases = [[], {"rate_limits": []}]
        for key, value in (("used_percentage", True), ("used_percentage", -1),
                           ("used_percentage", float("nan")), ("resets_at", "tomorrow"),
                           ("resets_at", None), ("resets_at", float("inf"))):
            data = payload(); data["rate_limits"]["five_hour"][key] = value; cases.append(data)
        for case in cases:
            with self.subTest(case=case), self.assertRaises((ValueError, TypeError)):
                normalized_export(case, NOW)

    def test_timer_wall_clock_prompt_changes_do_not_advance_evidence(self):
        first = payload(); second = payload()
        second["cost"]["total_duration_ms"] = 999999
        second["prompt_id"] = "new prompt before response"
        self.assertEqual(normalized_export(first, NOW)[1], normalized_export(second, NOW)[1])
        second["cost"]["total_api_duration_ms"] += 1
        self.assertNotEqual(normalized_export(first, NOW)[1], normalized_export(second, NOW)[1])


class ClaudeDatabaseTests(ManagementBase):
    def setUp(self):
        process_env = {key: value for key, value in os.environ.items() if not key.startswith("DASH_")}
        super().setUp()
        # Management fixtures clear env keys; real Windows shell launch needs these.
        process_patch = patch.dict(os.environ, process_env)
        process_patch.start(); self.addCleanup(process_patch.stop)
        self.settings = Path(self.temp.name) / "settings.json"
        self.settings.write_text(json.dumps({"other": "preserved"}), encoding="utf-8")
        self.row = self.manager.save({"type": "claude", "name": "Claude personal", "enabled": True,
            "config": {"settings_path": str(self.settings)}})

    def install(self, action="install", row=None):
        row = row or self.row
        return self.manager.configure_claude_export(row["id"], {"action": action, "revision": row["revision"]})

    def export(self, data=None, observed=NOW, row=None):
        envelope = build_envelope(data or payload(), "fixture-host", client_time=observed)
        return ingest(self.storage, (row or self.row)["id"], envelope, observed)

    def test_cached_redraw_preserves_observation_and_idle_reset_becomes_stale(self):
        self.export()
        self.export(observed=NOW + timedelta(minutes=10))
        report = self.storage.observation(self.row["id"])["report"]
        self.assertEqual(datetime.fromisoformat(report["windows"][0]["observed_at"]), NOW)
        self.assertEqual(present_quota(report, NOW + timedelta(minutes=16))["windows"][0]["state"], "stale")
        changed = payload(); changed["context_window"]["total_output_tokens"] += 1
        self.export(changed, NOW + timedelta(minutes=20))
        current = self.storage.observation(self.row["id"])["report"]
        self.assertEqual(datetime.fromisoformat(current["windows"][0]["observed_at"]),
                         NOW + timedelta(minutes=20))
        self.assertEqual(present_quota(current, NOW + timedelta(days=8))["windows"][0]["state"], "stale")

    def test_window_freshness_is_independent_without_response_markers(self):
        first = payload(); first.pop("cost"); first.pop("context_window")
        self.export(first)
        first["rate_limits"]["five_hour"]["used_percentage"] = 30
        self.export(first, NOW + timedelta(minutes=5))
        windows = self.storage.observation(self.row["id"])["report"]["windows"]
        self.assertEqual(datetime.fromisoformat(windows[0]["observed_at"]), NOW + timedelta(minutes=5))
        self.assertEqual(datetime.fromisoformat(windows[1]["observed_at"]), NOW)

    def test_export_and_collector_multiple_accounts_survive_restart(self):
        other = self.manager.save({"type": "claude", "name": "Claude work", "enabled": True,
                                  "config": {"settings_path": str(Path(self.temp.name) / "work/settings.json")}})
        self.export(payload(10)); self.export(payload(50), row=other)
        ManagedCollector(self.manager, self.config, threading.Event()).collect_once()
        self.storage.close(); self.storage = Storage(self.database); self.addCleanup(self.storage.close)
        restarted = SourceConfiguration(self.storage, self.config)
        with patch("presto_ai_dash.quotas.now_utc", return_value=NOW):
            result = restarted.status_payload()
        self.assertEqual([s["quota"]["windows"][0]["used_percent"] for s in result["sources"]], [10, 50])
        self.assertEqual([c["title"] for c in result["cards"]], ["AI Dash", "Claude personal", "Claude work"])
        self.assertNotIn("private", json.dumps(result))

    def test_disabled_deleted_and_wrong_type_reject_exports(self):
        local = next(r for r in self.manager.list_sources()["sources"] if r["type"] == "ollama")
        self.assertFalse(self.export(row=local))
        updated = self.manager.save({"enabled": False, "revision": self.row["revision"]}, self.row["id"])
        self.assertFalse(self.export())
        self.manager.delete(updated["id"], updated["revision"])
        self.assertFalse(self.export())

    def test_pending_source_warns_without_fabricated_percentage(self):
        ManagedCollector(self.manager, self.config, threading.Event()).collect_once()
        result = self.manager.status_payload()["sources"][0]
        self.assertEqual(result["status"], "warning")
        self.assertTrue(all(w["state"] == "unavailable" and w["used_percent"] is None for w in result["quota"]["windows"]))
        self.assertFalse(next(t for t in source_catalog() if t["id"] == "claude")["has_secret"])

    def test_setup_is_idempotent_remove_restores_existing_statusline_and_keys(self):
        previous = {"type": "command", "command": "echo old-private-command", "padding": 2, "refreshInterval": 10}
        self.settings.write_text(json.dumps({"statusLine": previous, "other": "preserved"}))
        self.install(); self.install()
        after = json.loads(self.settings.read_text())
        self.assertEqual(after["statusLine"]["refreshInterval"], 10)
        self.assertNotIn(b"old-private-command", self.database.read_bytes())
        self.install("remove")
        self.assertEqual(json.loads(self.settings.read_text()), {"statusLine": previous, "other": "preserved"})
        self.assertIsNone(self.storage.claude_bridge(self.row["id"]))

    def test_remove_and_delete_preserve_user_changed_settings(self):
        self.install()
        self.settings.write_text(json.dumps({"statusLine": {"type": "command", "command": "echo new"}}))
        self.manager.delete(self.row["id"], self.row["revision"])
        self.assertEqual(json.loads(self.settings.read_text())["statusLine"]["command"], "echo new")

    def test_failed_file_update_rolls_back_and_conflicting_revision_does_not_write(self):
        with patch("presto_ai_dash.claude_export._replace", side_effect=OSError("fixture failure")):
            with self.assertRaises(OSError): self.install()
        self.assertIsNone(self.storage.claude_bridge(self.row["id"]))
        with self.assertRaises(ConfigurationConflict):
            self.manager.configure_claude_export(self.row["id"], {"action": "install", "revision": 99})
        self.assertNotIn("statusLine", json.loads(self.settings.read_text()))

    def test_profile_cannot_be_changed_or_shared_while_export_installed(self):
        self.install()
        with self.assertRaisesRegex(ValueError, "Remove"):
            self.manager.save({"revision": self.row["revision"], "config": {"settings_path": "another"}}, self.row["id"])
        other = self.manager.save({"type": "claude", "config": {"settings_path": str(self.settings)}})
        with self.assertRaisesRegex(ValueError, "already"):
            self.install(row=other)

    def test_real_helper_command_handles_windows_unicode_and_writes_only_minimized_data(self):
        self.install()
        command = json.loads(self.settings.read_text())["statusLine"]["command"]
        argv = command.split() if os.name == "nt" else ["/bin/sh", "-c", command]
        data = payload(); data["ignored_secret"] = "private-token-\u00e9"
        result = subprocess.run(argv, input=json.dumps(data, ensure_ascii=False).encode("utf-8"),
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(b"5h 23.5%", result.stdout)
        self.assertIsNotNone(self.storage.observation(self.row["id"])["report"])
        self.assertNotIn("private", json.dumps(self.storage.observation(self.row["id"])["report"]))

    def test_prior_statusline_command_receives_original_input(self):
        prior_script = Path(self.temp.name) / "prior.py"
        prior_script.write_text("import sys,json; print('kept:'+json.load(sys.stdin)['version'])")
        prior = '"%s" "%s"' % (sys.executable.replace("\\", "/"), str(prior_script).replace("\\", "/"))
        self.settings.write_text(json.dumps({"statusLine": {"type": "command", "command": prior}}))
        self.install()
        command = json.loads(self.settings.read_text())["statusLine"]["command"]
        result = subprocess.run(command.split() if os.name == "nt" else ["/bin/sh", "-c", command],
                                input=json.dumps(payload()).encode(), stdout=subprocess.PIPE, timeout=10)
        self.assertEqual(result.stdout.strip(), b"kept:2.1.296")
        self.assertIsNotNone(self.storage.observation(self.row["id"])["report"])


class ClaudeAdminTests(unittest.TestCase):
    def test_setup_endpoint_uses_existing_auth_csrf_and_supports_remove(self):
        fixture = test_management.AdminHttpTests()
        fixture.setUp(); self.addCleanup(fixture.doCleanups)
        settings = Path(fixture.temp.name) / "settings.json"
        settings.write_text('{}')
        row = fixture.manager.save({"type": "claude", "config": {"settings_path": str(settings)}})
        path = "/api/admin/sources/" + row["id"] + "/claude-export"
        data = {"action": "install", "revision": row["revision"]}
        self.assertEqual(fixture.request(path, "POST", data, token="device-token")[0], 401)
        cookie, session, _ = fixture.login()
        headers = {"Cookie": cookie, "Origin": fixture.base_url, "X-CSRF-Token": session["csrf_token"]}
        self.assertEqual(fixture.request(path, "POST", data, token=None, headers={"Cookie": cookie})[0], 403)
        self.assertEqual(fixture.request(path, "POST", data, token=None, headers=headers)[0], 201)
        listed = fixture.request()[2]
        self.assertTrue(next(r for r in listed["sources"] if r["id"] == row["id"])["export_installed"])
        self.assertNotIn("previous", json.dumps(listed))
        self.assertEqual(fixture.request(path, "POST", {**data, "action": "remove"}, token=None, headers=headers)[0], 201)
        self.assertNotIn("statusLine", json.loads(settings.read_text()))


if __name__ == "__main__":
    unittest.main()
