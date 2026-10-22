"""M2 contracts, including real localhost HTTP and SQLite round trips."""

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from envutil import clean_env
from urllib.error import HTTPError
from urllib.request import Request, build_opener, ProxyHandler

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from presto_ai_dash.api import ApiServer
from presto_ai_dash.app import Daemon
from presto_ai_dash.config import ConfigError, load_config, resolve_shared_token
from presto_ai_dash.dashboard import build_status_payload
from presto_ai_dash.models import SourceSnapshot
from presto_ai_dash.sources import Source, available_sources, build_sources
from presto_ai_dash.sources.fake import FakeSource, SCENARIOS, SCENARIO_METRICS
from presto_ai_dash.storage import Storage


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.env = clean_env()
        self.env.start()
        self.addCleanup(self.env.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def load(self, text):
        path = Path(self.temp.name) / "config.toml"
        path.write_text(text, encoding="utf-8")
        return load_config(path)

    def test_defaults_are_independent(self):
        first = load_config()
        first["sources"]["fake"]["enabled"] = False
        self.assertTrue(load_config()["sources"]["fake"]["enabled"])
        self.assertEqual(load_config()["daemon"]["port"], 8787)

    def test_toml_and_env_precedence(self):
        with patch.dict(os.environ, {"DASH_PORT": "9001", "DASH_SHARED_TOKEN": "env-token-0123456789"}):
            config = self.load('[daemon]\nport = 9000\nshared_token = "file-token-0123456789"\n')
        self.assertEqual(config["daemon"]["port"], 9001)
        self.assertEqual(config["daemon"]["shared_token"], "env-token-0123456789")

    def test_example_loads(self):
        config = load_config(SRC.parents[1] / "examples" / "config.example.toml")
        self.assertEqual([source.id for source in build_sources(config)], ["fake"])

    def test_invalid_config(self):
        cases = ['daemon = 1', '[daemon]\nport = 0', '[daemon]\nport = 65536',
                 '[daemon]\npoll_interval = 0', '[daemon]\nport = true',
                 '[daemon]\nport = 1.5', '[daemon]\nshared_token = 42',
                 '[daemon]\nshared_token = "bad token"', '[daemon]\ndatabase = ""',
                 '[logging]\nlevel = "nope"', '[sources.fake]\nenabled = "false"',
                 '[sources.fake]\nscenario_interval = 0', '[sources]\nfake = false',
                 '[sources.ollama]\nbase_url = "localhost:11434"',
                 '[sources.ollama]\nbase_url = "http://[invalid"',
                 '[sources.litellm]\napi_key_env = "bad=name"']
        for text in cases:
            with self.subTest(text=text), self.assertRaises(ConfigError):
                self.load(text)

    def test_bad_file_and_env_errors(self):
        with self.assertRaises(ConfigError):
            load_config(Path(self.temp.name) / "missing.toml")
        with self.assertRaises(ConfigError):
            self.load("[not valid")
        with patch.dict(os.environ, {"DASH_PORT": "oops"}), self.assertRaises(ConfigError):
            load_config()

    def test_live_sources_require_fake_to_be_disabled(self):
        with self.assertRaisesRegex(ConfigError, "disable sources.fake"):
            self.load('[sources.ollama]\nenabled = true')
        config = self.load('[sources.fake]\nenabled = false\n'
                           '[sources.ollama]\nenabled = true\n'
                           '[sources.litellm]\nenabled = true')
        self.assertEqual([source.id for source in build_sources(config)],
                         ["ollama", "litellm"])

    def test_litellm_key_environment_override(self):
        with patch.dict(os.environ, {"LITELLM_API_KEY": "env-key"}):
            config = self.load('[sources.fake]\nenabled = false\n'
                               '[sources.litellm]\nenabled = true\n'
                               'api_key = "file-key"')
        self.assertEqual(config["sources"]["litellm"]["api_key"], "env-key")

    def test_generated_token_is_reused(self):
        config = load_config()
        with self.assertLogs("presto_ai_dash.config", level="WARNING"):
            token = resolve_shared_token(config)
        self.assertGreaterEqual(len(token), 24)
        self.assertEqual(resolve_shared_token(config), token)

    def test_configured_token_is_preserved(self):
        config = self.load('[daemon]\nshared_token = "test-token-0123456789"')
        self.assertEqual(resolve_shared_token(config), "test-token-0123456789")


class FakeTests(unittest.TestCase):
    def test_all_scenarios_and_wrap(self):
        source = FakeSource({"scenario_interval": 10})
        for index, scenario in enumerate(SCENARIOS):
            self.assertEqual(source.scenario_at(index * 10), scenario)
            self.assertEqual(source.scenario_at(index * 10 + 9.9), scenario)
        self.assertEqual(source.scenario_at(70), "ok")

    def test_collect_is_deterministic_and_independent(self):
        source = FakeSource()
        with patch.object(source, "_elapsed", return_value=0):
            first = source.collect_safely()
            first.metrics["tokens_today"] = -1
            second = source.collect_safely()
        self.assertEqual(second.metrics, SCENARIO_METRICS["ok"])
        self.assertEqual(second.source_id, "fake")
        self.assertEqual(second.updated_at.tzinfo, timezone.utc)

    def test_offline_values_and_warning_alert(self):
        source = FakeSource()
        with patch.object(source, "current_scenario", return_value="offline"):
            snapshot = source.collect()
        self.assertTrue(all(value is None for value in snapshot.metrics.values()))
        with patch.object(source, "current_scenario", return_value="warning"):
            self.assertEqual(source.collect().alerts[0]["severity"], "warning")

    def test_sources_are_registered_and_disabled_sources_do_not_probe(self):
        self.assertEqual(set(available_sources()),
                         {"fake", "ollama", "litellm", "opencode_go", "ollama_cloud", "codex", "claude"})
        config = load_config()
        sources = build_sources(config)
        self.assertEqual([source.id for source in sources], ["fake"])

    def test_source_failure_becomes_offline(self):
        source = Source()
        with patch.object(source, "collect", side_effect=RuntimeError("test failure")):
            with self.assertLogs("presto_ai_dash.sources", level="ERROR"):
                snapshot = source.collect_safely()
        self.assertEqual(snapshot.status, "offline")
        self.assertIn("test failure", snapshot.error)


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "snapshots.db"
        self.storage = Storage(self.path)
        self.addCleanup(lambda: self.storage.close())

    def test_round_trip_and_reopen(self):
        snapshot = SourceSnapshot("fake", "warning", metrics={"tokens_today": 123},
                                  alerts=[{"severity": "warning", "message": "test"}],
                                  error="test error")
        self.storage.save_snapshot(snapshot)
        self.storage.close()
        self.storage = Storage(self.path)
        restored = self.storage.latest_snapshots()["fake"]
        self.assertEqual(restored.to_dict(), snapshot.to_dict())
        self.assertIsInstance(restored.updated_at, datetime)
        self.assertEqual(restored.updated_at.tzinfo, timezone.utc)
        self.assertEqual(build_status_payload({"fake": restored})["overall_status"], "warning")

    def test_latest_per_source_and_timestamp_tie(self):
        moment = datetime(2026, 1, 1, tzinfo=timezone.utc)
        for status in ("ok", "warning"):
            self.storage.save_snapshot(SourceSnapshot("fake", status, moment))
        self.storage.save_snapshot(SourceSnapshot("fake", "offline", moment - timedelta(days=1)))
        self.storage.save_snapshot(SourceSnapshot("other", "active", moment))
        latest = self.storage.latest_snapshots()
        self.assertEqual(latest["fake"].status, "warning")
        self.assertEqual(latest["other"].status, "active")

    def test_empty_storage(self):
        self.assertEqual(self.storage.latest_snapshots(), {})

    def test_concurrent_writes_and_reads(self):
        errors = []
        def worker():
            try:
                for _ in range(10):
                    self.storage.save_snapshot(FakeSource().collect())
                    self.storage.latest_snapshots()
            except Exception as exc:
                errors.append(exc)
        threads = [threading.Thread(target=worker) for _ in range(3)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(self.storage.latest_snapshots()["fake"].status, "ok")


class DashboardTests(unittest.TestCase):
    def test_response_shape_and_fake_data(self):
        snapshot = FakeSource().collect()
        payload = build_status_payload({"fake": snapshot})
        self.assertEqual(set(payload), {"app", "version", "updated_at", "overall_status", "cards"})
        self.assertEqual(payload["app"], "presto-ai-dash")
        self.assertEqual(datetime.fromisoformat(payload["updated_at"]), snapshot.updated_at)
        self.assertEqual([card["id"] for card in payload["cards"]], ["dash", "ollama", "litellm"])
        self.assertEqual(payload["overall_status"], "ok")
        for card in payload["cards"]:
            self.assertEqual(set(card), {"id", "title", "status", "primary", "secondary",
                                         "detail", "accent", "alert"})
            self.assertIn("fake source", card["secondary"])
            self.assertTrue(all(isinstance(line, str) for line in card["detail"]))
            self.assertIsNone(card["alert"])
        self.assertIn("128000", payload["cards"][0]["primary"])
        json.dumps(payload)
        import presto_path  # noqa: F401
        from cards import parse_cards
        parsed, error = parse_cards(payload)
        self.assertIsNone(error)
        self.assertEqual([card.id for card in parsed], ["dash", "ollama", "litellm"])

    def test_empty_response_is_offline(self):
        payload = build_status_payload({})
        self.assertEqual(payload["overall_status"], "offline")
        self.assertIsNone(payload["updated_at"])
        self.assertTrue(all(card["status"] == "offline" for card in payload["cards"]))
        self.assertIn("unavailable", payload["cards"][0]["primary"])

    def test_each_scenario_rolls_up_and_alerts_are_visible(self):
        source = FakeSource()
        for scenario in SCENARIOS:
            with self.subTest(scenario=scenario):
                with patch.object(source, "current_scenario", return_value=scenario):
                    snapshot = source.collect()
                payload = build_status_payload({"fake": snapshot})
                self.assertEqual(payload["overall_status"], scenario)
                self.assertTrue(all(card["status"] == scenario for card in payload["cards"]))
                if snapshot.alerts:
                    self.assertEqual(payload["cards"][0]["alert"]["severity"], scenario)


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.storage = Storage(":memory:")
        self.addCleanup(self.storage.close)
        self.storage.save_snapshot(FakeSource().collect())
        self.api = ApiServer("127.0.0.1", 0, self.storage, "test-token")
        self.api.start()
        self.addCleanup(self.api.stop)
        self.url = "http://127.0.0.1:%d" % self.api.actual_port
        self.opener = build_opener(ProxyHandler({}))

    def request(self, auth=None, path="/api/v1/status"):
        request = Request(self.url + path, headers={"Authorization": auth} if auth else {})
        try:
            response = self.opener.open(request, timeout=3)
        except HTTPError as exc:
            response = exc
        with response:
            return response.status, dict(response.headers), json.load(response)

    def test_valid_auth_and_response(self):
        status, headers, payload = self.request("Bearer test-token")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "application/json")
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(payload["overall_status"], "ok")
        self.assertEqual(len(payload["cards"]), 3)

    def test_missing_wrong_and_malformed_auth(self):
        for auth in (None, "Bearer wrong", "Basic test-token", "Bearer", "Bearer test-token extra"):
            with self.subTest(auth=auth):
                status, headers, payload = self.request(auth)
                self.assertEqual(status, 401)
                self.assertEqual(headers["WWW-Authenticate"], "Bearer")
                self.assertEqual(payload, {"error": "unauthorised"})

    def test_scheme_is_case_insensitive(self):
        self.assertEqual(self.request("bearer test-token")[0], 200)

    def test_unknown_route(self):
        self.assertEqual(self.request("Bearer test-token", "/missing")[0], 404)

    def test_no_empty_token(self):
        with self.assertRaises(ValueError):
            ApiServer("127.0.0.1", 0, self.storage, "")

    def test_second_server_cannot_share_listening_port(self):
        with self.assertRaises(OSError):
            duplicate = ApiServer("127.0.0.1", self.api.actual_port, self.storage, "other-token")
            self.addCleanup(duplicate.stop)

    def test_storage_failure_returns_generic_error(self):
        with patch.object(self.storage, "latest_snapshots", side_effect=RuntimeError("private")):
            with self.assertLogs("presto_ai_dash.api", level="ERROR"):
                status, _, payload = self.request("Bearer test-token")
        self.assertEqual(status, 500)
        self.assertEqual(payload, {"error": "internal_error"})

    def test_api_filters_history_to_configured_sources(self):
        self.storage.save_snapshot(SourceSnapshot("ollama", "active", metrics={
            "loaded_model": "llama3"}))
        api = ApiServer("127.0.0.1", 0, self.storage, "test-token",
                        source_ids=["ollama"])
        api.start()
        try:
            url = "http://127.0.0.1:%d/api/v1/status" % api.actual_port
            request = Request(url, headers={"Authorization": "Bearer test-token"})
            with self.opener.open(request, timeout=3) as response:
                payload = json.load(response)
            self.assertEqual(payload["overall_status"], "active")
            self.assertEqual(payload["cards"][1]["primary"], "llama3")
        finally:
            api.stop()


class DaemonTests(unittest.TestCase):
    def test_daemon_collects_serves_and_stops(self):
        with clean_env():
            config = load_config()
        config["daemon"].update(host="127.0.0.1", port=0, database=":memory:",
                                shared_token="test-token", poll_interval=1)
        config["sources"]["fake"]["scenario_interval"] = 1
        daemon = Daemon(config).setup().start()
        try:
            url = "http://127.0.0.1:%d/api/v1/status" % daemon.api.actual_port
            opener = build_opener(ProxyHandler({}))
            request = Request(url, headers={"Authorization": "Bearer test-token"})
            with opener.open(request, timeout=3) as response:
                self.assertEqual(json.load(response)["overall_status"], "ok")
            deadline = time.monotonic() + 3
            while daemon.storage.latest_snapshots()["fake"].status == "ok":
                self.assertLess(time.monotonic(), deadline, "collector did not update")
                time.sleep(0.05)
        finally:
            daemon.stop()
        self.assertFalse(daemon.api._thread.is_alive())
        self.assertTrue(all(not collector.thread.is_alive() for collector in daemon.collectors))
        daemon.wait()  # already stopped: must return immediately

    def test_cleanup_before_server_start_does_not_hang(self):
        with clean_env():
            config = load_config()
        config["daemon"].update(host="127.0.0.1", port=0, database=":memory:",
                                shared_token="test-token")
        daemon = Daemon(config).setup()
        daemon.stop()
        self.assertTrue(daemon.stop_event.is_set())

    def test_cli_check_version_and_invalid_config(self):
        env = {**os.environ, "PYTHONPATH": str(SRC)}
        command = [sys.executable, "-m", "presto_ai_dash.app"]
        for args, expected in ((["--check"], 0), (["--version"], 0),
                               (["--config", "missing-config.toml"], 2)):
            with self.subTest(args=args):
                result = subprocess.run(command + args, env=env, capture_output=True,
                                        text=True, timeout=5)
                self.assertEqual(result.returncode, expected, result.stderr)


if __name__ == "__main__":
    unittest.main()
