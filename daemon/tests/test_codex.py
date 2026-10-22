"""Read-only Codex protocol, process cleanup, quota fidelity and managed accounts."""

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from presto_ai_dash.quotas import present_quota
from presto_ai_dash.sources.codex import CodexSource
from presto_ai_dash.sources._codex_rpc import (
    CodexReportingError, find_executable, read_subscription)
from presto_ai_dash.management import ManagedCollector, SourceConfiguration
from presto_ai_dash.sources import source_catalog
from presto_ai_dash.storage import Storage
from test_management import ManagementBase

NOW = datetime(2026, 10, 11, tzinfo=timezone.utc)
RESET = int((NOW + timedelta(days=7)).timestamp())


def response(percent=14):
    return {"rateLimitsByLimitId": {"codex": {"primary": {
        "usedPercent": percent, "windowDurationMins": 10080, "resetsAt": RESET},
        "secondary": None}}}


class CodexParsingTests(unittest.TestCase):
    def test_actual_weekly_primary_and_absent_secondary(self):
        report, limited = CodexSource().parse(response(), NOW)
        self.assertFalse(limited)
        first, missing = report["windows"]
        self.assertEqual((first["label"], first["period_minutes"], first["used_percent"]),
                         ("Weekly", 10080, 14))
        self.assertEqual(first["resets_at"], (NOW + timedelta(days=7)).isoformat())
        self.assertEqual(missing["state"], "unavailable")
        self.assertIsNone(missing["used_percent"])

    def test_map_takes_precedence_and_multiple_buckets_are_independent(self):
        data = response()
        data["rateLimits"] = {"primary": {"usedPercent": 99}}
        data["rateLimitsByLimitId"]["other"] = {"primary": {
            "usedPercent": 31, "windowDurationMins": 300, "resetsAt": RESET},
            "secondary": {"usedPercent": 10, "windowDurationMins": 1440, "resetsAt": RESET}}
        report, _ = CodexSource().parse(data, NOW)
        self.assertEqual([w["used_percent"] for w in report["windows"]], [14, None, 31, 10])
        self.assertEqual([w["id"] for w in report["windows"]],
                         ["codex:primary", "codex:secondary", "other:primary", "other:secondary"])
        self.assertEqual(report["windows"][2]["label"], "other 5h")

    def test_legacy_and_nonstandard_periods_are_not_called_daily(self):
        bucket = response()["rateLimitsByLimitId"]["codex"]
        bucket["primary"]["windowDurationMins"] = 90
        report, _ = CodexSource().parse({"rateLimits": bucket}, NOW)
        self.assertEqual(report["windows"][0]["label"], "90m")
        bucket["primary"].pop("windowDurationMins")
        report, _ = CodexSource().parse({"rateLimitsByLimitId": None, "rateLimits": bucket}, NOW)
        self.assertEqual(report["windows"][0]["label"], "Reported window")

    def test_invalid_schema_percent_duration_and_reset_fail_closed(self):
        invalid = [None, {}, {"rateLimitsByLimitId": []}, {"rateLimitsByLimitId": {}},
                   {"rateLimitsByLimitId": {"codex": []}}]
        for key, value in (("usedPercent", -1), ("usedPercent", True),
                           ("usedPercent", float("nan")), ("windowDurationMins", 0),
                           ("windowDurationMins", "300"), ("resetsAt", "tomorrow")):
            data = response()
            data["rateLimitsByLimitId"]["codex"]["primary"][key] = value
            invalid.append(data)
        for data in invalid:
            with self.subTest(data=data), self.assertRaises((ValueError, TypeError)):
                CodexSource().parse(data, NOW)

    def test_limited_reset_aging_and_no_private_payload_storage(self):
        data = response(102)
        data["email"] = "private@example.test"
        data["rateLimitsByLimitId"]["codex"]["token"] = "private-token"
        report, limited = CodexSource().parse(data, NOW)
        self.assertTrue(limited)
        self.assertEqual(report["windows"][0]["used_percent"], 102)
        self.assertNotIn("private", json.dumps(report))
        aged = present_quota(report, NOW + timedelta(days=8))
        self.assertEqual(aged["windows"][0]["state"], "stale")
        self.assertEqual(aged["windows"][0]["used_percent"], 102)

    def test_failure_retains_observation_then_success_recovers(self):
        source = CodexSource()
        with patch("presto_ai_dash.sources.codex.find_executable", return_value="fixture"), \
             patch("presto_ai_dash.sources.codex.read_subscription", side_effect=[
                 response(), CodexReportingError("Codex reporting timed out"), response(20)]), \
             patch("presto_ai_dash.sources.codex.time.monotonic", side_effect=[0, 1, 61, 122]):
            first = source.collect()
            self.assertEqual(source.collect().updated_at, first.updated_at)
            failed = source.collect()
            recovered = source.collect()
        self.assertEqual(failed.status, "offline")
        self.assertEqual(failed.metrics["quota"]["windows"], first.metrics["quota"]["windows"])
        self.assertEqual(present_quota(failed.metrics["quota"])["windows"][0]["state"], "stale")
        self.assertEqual(recovered.status, "ok")
        self.assertIsNone(recovered.error)
        self.assertEqual(recovered.metrics["quota"]["windows"][0]["used_percent"], 20)


FIXTURE = r'''
import json, os, sys, time
mode = sys.argv[1]
for line in sys.stdin:
    msg = json.loads(line)
    with open(sys.argv[2], "a") as log:
        log.write(json.dumps({"method": msg["method"], "params": msg.get("params"),
                              "home": os.environ.get("CODEX_HOME")}) + "\n")
    if "id" not in msg: continue
    if mode == "timeout": time.sleep(30)
    if mode == "exit": sys.exit(1)
    if mode == "oversize": print("x" * (1024 * 1024 + 1), flush=True); continue
    if mode == "malformed": print("not json", flush=True); continue
    if mode == "error":
        print(json.dumps({"id": msg["id"], "error": {"message": "private-auth-token"}}), flush=True)
        continue
    if msg["method"] == "account/read":
        result = {"account": None if mode == "signedout" else {"type": "apiKey" if mode == "apikey" else "chatgpt",
                                                              "email": "private@example.test"}}
    elif msg["method"] == "account/rateLimits/read": result = {"rateLimits": {"primary": None}}
    else: result = {}
    print(json.dumps({"method": "account/updated", "params": {"token": "private-auth-token"}}), flush=True)
    print(json.dumps({"id": msg["id"], "result": result}), flush=True)
'''


class CodexProtocolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.fixture = self.directory / "server.py"
        self.fixture.write_text(FIXTURE, encoding="utf-8")
        self.log = self.directory / "methods.jsonl"
        self.processes = []

    def read(self, mode="ok", home="", timeout=2):
        popen = subprocess.Popen
        def launch(command, **kwargs):
            self.assertEqual(command, ["fixture", "app-server", "--listen", "stdio://"])
            process = popen([sys.executable, str(self.fixture), mode, str(self.log)], **kwargs)
            self.processes.append(process)
            return process
        with patch("presto_ai_dash.sources._codex_rpc.subprocess.Popen", side_effect=launch):
            try:
                return read_subscription("fixture", home, timeout)
            finally:
                self.assertTrue(all(p.poll() is not None for p in self.processes))

    def test_only_reporting_methods_and_profile_isolation(self):
        profiles = [str(self.directory / "one"), str(self.directory / "two")]
        for profile in profiles:
            self.read(home=profile)
        rows = [json.loads(line) for line in self.log.read_text().splitlines()]
        self.assertEqual([r["method"] for r in rows],
                         ["initialize", "initialized", "account/read", "account/rateLimits/read"] * 2)
        self.assertEqual([r["home"] for r in rows], [profiles[0]] * 4 + [profiles[1]] * 4)
        self.assertEqual(rows[2]["params"], {"refreshToken": False})

    def test_no_subscription_login_and_api_key_login_are_rejected(self):
        for mode in ("signedout", "apikey"):
            with self.subTest(mode=mode), self.assertRaisesRegex(CodexReportingError, "ChatGPT"):
                self.read(mode)
        self.assertNotIn("account/rateLimits/read", self.log.read_text())

    def test_server_failures_never_reflect_tokens_and_reap_process(self):
        for mode in ("error", "malformed", "exit", "oversize"):
            with self.subTest(mode=mode), self.assertRaises(CodexReportingError) as caught:
                self.read(mode)
            self.assertNotIn("private", str(caught.exception))

    def test_timeout_is_bounded_and_reaps_process(self):
        start = time.monotonic()
        with self.assertRaisesRegex(CodexReportingError, "timed out"):
            self.read("timeout", timeout=0.15)
        self.assertLess(time.monotonic() - start, 3)

    def test_relative_profile_rejected_before_launch(self):
        with self.assertRaisesRegex(CodexReportingError, "absolute path"):
            self.read(home="relative")
        self.assertEqual(self.processes, [])

    def test_executable_discovery_explicit_path_and_missing_install(self):
        with patch("presto_ai_dash.sources._codex_rpc.shutil.which", return_value="/installed/codex"):
            self.assertEqual(find_executable(), "/installed/codex")
            self.assertEqual(find_executable("custom"), "/installed/codex")
        with patch("presto_ai_dash.sources._codex_rpc.shutil.which", return_value=None), \
             patch("presto_ai_dash.sources._codex_rpc.Path.home", return_value=self.directory):
            with self.assertRaises(CodexReportingError):
                find_executable()
            with self.assertRaises(CodexReportingError):
                find_executable("missing")


class CodexManagedTests(ManagementBase):
    def test_ui_catalog_database_restart_and_independent_account_cards(self):
        self.disable_fake()
        description = next(t for t in source_catalog() if t["id"] == "codex")
        self.assertFalse(description["has_secret"])
        rows = [self.manager.save({"type": "codex", "name": "Codex " + profile,
                                  "enabled": True, "config": {"profile_home": str(Path(self.temp.name) / profile)}})
                for profile in ("personal", "work")]
        with patch("presto_ai_dash.sources.codex.find_executable", return_value="fixture"), \
             patch("presto_ai_dash.sources.codex.read_subscription", side_effect=lambda exe, home, timeout:
                   response(10 if home.endswith("personal") else 50)):
            ManagedCollector(self.manager, self.config, threading.Event()).collect_once()
        payload = self.manager.status_payload()
        self.assertEqual([c["title"] for c in payload["cards"]], ["AI Dash", "Codex personal", "Codex work"])
        self.assertEqual([r["quota"]["windows"][0]["used_percent"] for r in payload["sources"]], [10, 50])
        self.storage.close()
        self.storage = Storage(self.database)
        self.addCleanup(self.storage.close)
        restored = SourceConfiguration(self.storage, self.config)
        configs = {r["id"]: r["config"] for r in restored.list_sources()["sources"]}
        self.assertEqual(configs[rows[0]["id"]]["profile_home"], str(Path(self.temp.name) / "personal"))
        self.assertEqual(len(restored.status_payload()["cards"]), 3)


if __name__ == "__main__":
    unittest.main()
