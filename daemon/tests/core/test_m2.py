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

SRC = Path(__file__).resolve().parents[2] / "src"
sys.path.insert(0, str(SRC))

from presto_ai_dash.api import ApiServer
from presto_ai_dash.app import Daemon
from presto_ai_dash.config import ConfigError, load_config, resolve_shared_token
from presto_ai_dash.dashboard import build_status_payload
from presto_ai_dash.models import SourceSnapshot
from presto_ai_dash.sources import Source, available_sources, build_sources
from presto_ai_dash.sources.ollama import OllamaSource
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
        first["sources"]["ollama"]["enabled"] = True
        self.assertFalse(load_config()["sources"]["ollama"]["enabled"])
        self.assertEqual(load_config()["daemon"]["port"], 8787)

    def test_toml_and_env_precedence(self):
        with patch.dict(os.environ, {"DASH_PORT": "9001", "DASH_SHARED_TOKEN": "env-token-0123456789"}):
            config = self.load('[daemon]\nport = 9000\nshared_token = "file-token-0123456789"\n')
        self.assertEqual(config["daemon"]["port"], 9001)
        self.assertEqual(config["daemon"]["shared_token"], "env-token-0123456789")

    def test_example_loads(self):
        config = load_config(SRC.parents[1] / "examples" / "config.example.toml")
        # The example enables no source: a fresh copy probes nothing.
        self.assertEqual(build_sources(config), [])

    def test_invalid_config(self):
        cases = ['daemon = 1', '[daemon]\nport = 0', '[daemon]\nport = 65536',
                 '[daemon]\npoll_interval = 0', '[daemon]\nport = true',
                 '[daemon]\nport = 1.5', '[daemon]\nshared_token = 42',
                 '[daemon]\nshared_token = "bad token"', '[daemon]\ndatabase = ""',
                 '[logging]\nlevel = "nope"',
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

    def test_a_leftover_demo_section_is_ignored_with_a_warning(self):
        """An install that still has [sources.fake] keeps working."""
        for text in ('[sources.fake]\nenabled = true\n[sources.ollama]\nenabled = true',
                     '[sources.fake]\nenabled = "not even a boolean"',
                     '[sources.fake]\nscenario_interval = 0'):
            with self.subTest(text=text):
                with self.assertLogs("presto_ai_dash.config", level="WARNING"):
                    config = self.load(text)
                self.assertNotIn("fake", config["sources"])
        with self.assertLogs("presto_ai_dash.config", level="WARNING"):
            config = self.load('[sources.fake]\nenabled = true\n'
                               '[sources.ollama]\nenabled = true')
        self.assertEqual([source.id for source in build_sources(config)], ["ollama"])

    def test_litellm_key_environment_override(self):
        with patch.dict(os.environ, {"LITELLM_API_KEY": "env-key"}):
            config = self.load('[sources.litellm]\nenabled = true\n'
                               'api_key = "file-key"')
        self.assertEqual(config["sources"]["litellm"]["api_key"], "env-key")

    def test_generated_token_is_reused(self):
        config = load_config()
        token = resolve_shared_token(config)
        self.assertTrue(config["daemon"]["shared_token_generated"])
        self.assertGreaterEqual(len(token), 24)
        self.assertEqual(resolve_shared_token(config), token)

    def test_configured_token_is_preserved(self):
        config = self.load('[daemon]\nshared_token = "test-token-0123456789"')
        self.assertEqual(resolve_shared_token(config), "test-token-0123456789")


class SourceRegistryTests(unittest.TestCase):
    def test_sources_are_registered_and_disabled_sources_do_not_probe(self):
        self.assertEqual(set(available_sources()),
                         {"ollama", "litellm", "opencode_go", "ollama_cloud", "codex", "claude"})
        # No source section is enabled in the defaults, so nothing is probed.
        self.assertEqual(build_sources(load_config()), [])

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
        snapshot = SourceSnapshot("svc", "warning", metrics={"tokens_today": 123},
                                  alerts=[{"severity": "warning", "message": "test"}],
                                  error="test error")
        self.storage.save_snapshot(snapshot)
        self.storage.close()
        self.storage = Storage(self.path)
        restored = self.storage.latest_snapshots()["svc"]
        self.assertEqual(restored.to_dict(), snapshot.to_dict())
        self.assertIsInstance(restored.updated_at, datetime)
        self.assertEqual(restored.updated_at.tzinfo, timezone.utc)
        self.assertEqual(build_status_payload({"svc": restored})["overall_status"], "warning")

    def test_latest_per_source_and_timestamp_tie(self):
        moment = datetime(2026, 1, 1, tzinfo=timezone.utc)
        for status in ("ok", "warning"):
            self.storage.save_snapshot(SourceSnapshot("svc", status, moment))
        self.storage.save_snapshot(SourceSnapshot("svc", "offline", moment - timedelta(days=1)))
        self.storage.save_snapshot(SourceSnapshot("other", "active", moment))
        latest = self.storage.latest_snapshots()
        self.assertEqual(latest["svc"].status, "warning")
        self.assertEqual(latest["other"].status, "active")

    def test_empty_storage(self):
        self.assertEqual(self.storage.latest_snapshots(), {})

    def test_concurrent_writes_and_reads(self):
        errors = []
        def worker():
            try:
                for _ in range(10):
                    self.storage.save_snapshot(SourceSnapshot("svc", "ok"))
                    self.storage.latest_snapshots()
            except Exception as exc:
                errors.append(exc)
        threads = [threading.Thread(target=worker) for _ in range(3)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(self.storage.latest_snapshots()["svc"].status, "ok")


class DashboardTests(unittest.TestCase):
    def test_response_shape_of_the_local_sources(self):
        """The unmanaged path still renders its three-card summary."""
        snapshot = SourceSnapshot("litellm", "ok", metrics={
            "requests_today": 42, "tokens_today": 128000, "spend_today": 0.5,
            "top_model": "gpt-4o", "errors_today": 0})
        payload = build_status_payload({"litellm": snapshot})
        self.assertEqual(set(payload), {"app", "version", "updated_at", "overall_status", "cards"})
        self.assertEqual(payload["app"], "presto-ai-dash")
        self.assertEqual(datetime.fromisoformat(payload["updated_at"]), snapshot.updated_at)
        self.assertEqual([card["id"] for card in payload["cards"]], ["dash", "ollama", "litellm"])
        self.assertEqual(payload["overall_status"], "ok")
        for card in payload["cards"]:
            self.assertEqual(set(card), {"id", "title", "status", "primary", "secondary",
                                         "detail", "accent", "alert"})
            self.assertTrue(all(isinstance(line, str) for line in card["detail"]))
            self.assertIsNone(card["alert"])
        self.assertEqual(payload["cards"][2]["primary"], "42 requests today")
        json.dumps(payload)
        import presto_path  # noqa: F401
        from cards import parse_cards
        parsed, error = parse_cards(payload)
        self.assertIsNone(error)
        self.assertEqual([card.id for card in parsed], ["dash", "ollama", "litellm"])

    def test_nothing_configured_sends_an_empty_home_and_no_sample_reading(self):
        """No source reported: the device draws its first-run screen."""
        payload = build_status_payload({})
        self.assertEqual(payload["sources"], [])
        self.assertEqual([card["id"] for card in payload["cards"]], ["dash"])
        self.assertIsNone(payload["updated_at"])
        # The daemon's own status vocabulary is unchanged: the empty screen
        # is the device's reading of "no rows", not a new status here.
        self.assertEqual(payload["overall_status"], "ok")


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.storage = Storage(":memory:")
        self.addCleanup(self.storage.close)
        self.storage.save_snapshot(SourceSnapshot("litellm", "ok", metrics={
            "requests_today": 42, "tokens_today": 128000, "spend_today": 0.5,
            "top_model": "gpt-4o", "errors_today": 0}))
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
        config["sources"]["ollama"].update(enabled=True, base_url="http://127.0.0.1:1",
                                            timeout=1)
        def collect(source):
            return source.snapshot("active", metrics={"loaded_model": "fixture"})
        with patch.object(OllamaSource, "collect", new=collect):
            daemon = Daemon(config).setup().start()
        try:
            url = "http://127.0.0.1:%d/api/v1/status" % daemon.api.actual_port
            opener = build_opener(ProxyHandler({}))
            request = Request(url, headers={"Authorization": "Bearer test-token"})
            with opener.open(request, timeout=3) as response:
                self.assertEqual(json.load(response)["overall_status"], "active")
            deadline = time.monotonic() + 3
            while "ollama" not in daemon.storage.latest_snapshots():
                self.assertLess(time.monotonic(), deadline, "collector did not report")
                time.sleep(0.05)
            self.assertEqual(daemon.storage.latest_snapshots()["ollama"].status, "active")
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
