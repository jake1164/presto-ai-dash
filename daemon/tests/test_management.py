"""Encrypted source CRUD, admin access controls, and runtime/restart behavior."""

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, build_opener, ProxyHandler

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from cryptography.fernet import Fernet
from presto_ai_dash.admin import AdminServer
from presto_ai_dash.app import Daemon
from presto_ai_dash.config import ConfigError, load_config
from presto_ai_dash.credentials import load_cipher
from presto_ai_dash.management import ManagedCollector, SourceConfiguration
from presto_ai_dash.models import SourceSnapshot
from presto_ai_dash.sources.litellm import LiteLLMSource
from presto_ai_dash.sources.ollama import OllamaSource
from presto_ai_dash.storage import Storage, ConfigurationConflict
import threading


class ManagementBase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.database = Path(self.temp.name) / "dash.db"
        self.master = Fernet.generate_key().decode("ascii")
        env = patch.dict(os.environ, {"DASH_ENCRYPTION_KEY": self.master}, clear=True)
        env.start()
        self.addCleanup(env.stop)
        self.config = load_config()
        self.config["daemon"].update(database=str(self.database), host="127.0.0.1", port=0,
                                     shared_token="device-token", poll_interval=1)
        self.config["admin"].update(enabled=True, port=0, token="admin-token")
        self.storage = Storage(self.database)
        self.addCleanup(self.storage.close)
        self.manager = SourceConfiguration(self.storage, self.config)

    def disable_fake(self):
        row = next(row for row in self.manager.list_sources()["sources"] if row["type"] == "fake")
        return self.manager.save({"enabled": False, "revision": row["revision"]}, row["id"])

    def add_litellm(self, name="Book app gateway", key="test-provider-key", enabled=False):
        return self.manager.save({"type": "litellm", "name": name, "enabled": enabled,
                                  "api_key": key, "config": {"base_url": "http://localhost:4000"}})


class EncryptedConfigurationTests(ManagementBase):
    def test_ciphertext_database_public_output_and_restart(self):
        row = self.add_litellm()
        raw = next(record for record in self.storage.source_instances() if record["id"] == row["id"])
        self.assertNotIn("test-provider-key", json.dumps(row))
        self.assertTrue(row["has_key"])
        self.assertNotIn(b"test-provider-key", bytes(raw["credential"]))
        self.assertEqual(self.manager.cipher.decrypt(row["id"], raw["credential"]), "test-provider-key")
        self.assertNotIn(b"test-provider-key", self.database.read_bytes())
        self.storage.close()
        self.storage = Storage(self.database)
        self.addCleanup(self.storage.close)
        restarted = SourceConfiguration(self.storage, self.config)
        stored = next(record for record in self.storage.source_instances() if record["id"] == row["id"])
        self.assertEqual(restarted.cipher.decrypt(row["id"], stored["credential"]), "test-provider-key")

    def test_replace_preserve_clear_key_and_keep_history(self):
        row = self.add_litellm()
        instance_id = row["id"]
        self.storage.save_snapshot(SourceSnapshot(instance_id, "ok", metrics={"requests_today": 12}))
        unchanged = self.manager.save({"revision": row["revision"], "api_key": "",
                                       "name": "Renamed gateway"}, instance_id)
        raw = next(record for record in self.storage.source_instances() if record["id"] == instance_id)
        self.assertEqual(self.manager.cipher.decrypt(instance_id, raw["credential"]), "test-provider-key")
        replaced = self.manager.save({"revision": unchanged["revision"],
                                      "api_key": "replacement-provider-key"}, instance_id)
        raw = next(record for record in self.storage.source_instances() if record["id"] == instance_id)
        self.assertEqual(self.manager.cipher.decrypt(instance_id, raw["credential"]), "replacement-provider-key")
        cleared = self.manager.save({"revision": replaced["revision"], "clear_key": True}, instance_id)
        self.assertFalse(cleared["has_key"])
        self.manager.delete(instance_id, cleared["revision"])
        self.assertEqual(self.storage.latest_snapshots()[instance_id].metrics["requests_today"], 12)

    def test_wrong_lost_and_swapped_keys_fail_closed(self):
        first, second = self.add_litellm("One"), self.add_litellm("Two")
        with patch.dict(os.environ, {"DASH_ENCRYPTION_KEY": Fernet.generate_key().decode("ascii")}):
            with self.assertRaisesRegex(ValueError, "cannot be decrypted"):
                SourceConfiguration(self.storage, self.config)
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ValueError, "missing"):
                SourceConfiguration(self.storage, self.config)
        ciphertext = next(row["credential"] for row in self.storage.source_instances() if row["id"] == first["id"])
        with self.storage._conn:
            self.storage._conn.execute("UPDATE source_instances SET credential=? WHERE id=?",
                                       (ciphertext, second["id"]))
        with self.assertRaisesRegex(ValueError, "cannot be decrypted"):
            SourceConfiguration(self.storage, self.config)

        with self.storage._conn:
            self.storage._conn.execute("DELETE FROM configuration_meta")
        with self.assertRaisesRegex(ValueError, "verifier is missing"):
            SourceConfiguration(self.storage, self.config)
        self.assertEqual(len(self.storage.source_instances()), 5)

    def test_key_file_round_trip_and_no_silent_replacement(self):
        with patch.dict(os.environ, {}, clear=True):
            key_file = Path(self.temp.name) / "separate.key"
            first = load_cipher(str(self.database), str(key_file))
            blob = first.encrypt("source-one", "value")
            second = load_cipher(str(self.database), str(key_file), existing=True)
            self.assertEqual(second.decrypt("source-one", blob), "value")
            self.assertTrue(key_file.read_bytes().startswith(b"DPAPI1\n" if os.name == "nt" else b"FERNET1\n"))
            key_file.unlink()
            with self.assertRaisesRegex(ValueError, "missing"):
                load_cipher(str(self.database), str(key_file), existing=True)
            self.assertFalse(key_file.exists())

    def test_deleted_configuration_is_not_reseeded_from_toml(self):
        for row in self.manager.list_sources()["sources"]:
            self.manager.delete(row["id"], row["revision"])
        SourceConfiguration(self.storage, self.config)
        self.assertEqual(self.storage.source_instances(), [])
        payload = self.manager.status_payload()
        self.assertEqual(payload["sources"], [])
        self.assertTrue(all(card["primary"] == "Not configured" for card in payload["cards"][1:]))
        self.assertNotIn("fake source", json.dumps(payload))

    def test_environment_key_is_imported_once_and_never_published(self):
        storage = Storage(":memory:")
        self.addCleanup(storage.close)
        with patch.dict(os.environ, {"LITELLM_API_KEY": "bootstrap-secret"}):
            config = load_config()
            config["daemon"]["database"] = ":memory:"
            manager = SourceConfiguration(storage, config)
        record = next(row for row in storage.source_instances() if row["type"] == "litellm")
        self.assertEqual(manager.cipher.decrypt(record["id"], record["credential"]), "bootstrap-secret")
        with patch.dict(os.environ, {"LITELLM_API_KEY": "changed-environment-secret"}):
            config = load_config()
            config["daemon"]["database"] = ":memory:"
            restarted = SourceConfiguration(storage, config)
        record = next(row for row in storage.source_instances() if row["type"] == "litellm")
        self.assertEqual(restarted.cipher.decrypt(record["id"], record["credential"]), "bootstrap-secret")
        self.assertNotIn("bootstrap-secret", json.dumps(restarted.list_sources()))

    def test_version_one_migrates_without_losing_history(self):
        path = Path(self.temp.name) / "v1.db"
        storage = Storage(path)
        storage.save_snapshot(SourceSnapshot("ollama", "active", metrics={"loaded_model": "previous"}))
        with storage._conn:
            storage._conn.execute("DROP TABLE source_instances")
            storage._conn.execute("DROP TABLE configuration_meta")
            storage._conn.execute("PRAGMA user_version = 1")
        storage.close()
        migrated = Storage(path)
        self.addCleanup(migrated.close)
        self.assertEqual(migrated.latest_snapshots()["ollama"].metrics["loaded_model"], "previous")
        self.assertIsNone(migrated.configuration_marker())

    def test_provider_exceptions_do_not_publish_or_log_keys(self):
        source = LiteLLMSource({"api_key": "secret-to-redact"})
        with patch.object(source, "collect", side_effect=RuntimeError("secret-to-redact")), \
             self.assertLogs("presto_ai_dash.sources", level="ERROR") as logs:
            snapshot = source.collect_safely()
        self.assertNotIn("secret-to-redact", repr(snapshot.to_dict()))
        self.assertNotIn("secret-to-redact", repr(logs.output))

    def test_conflicts_and_settings_validation_do_not_overwrite(self):
        row = self.add_litellm()
        changed = self.manager.save({"revision": row["revision"], "name": "Changed"}, row["id"])
        with self.assertRaises(ConfigurationConflict):
            self.manager.save({"revision": row["revision"], "api_key": "ignored"}, row["id"])
        with self.assertRaises(ConfigurationConflict):
            self.manager.delete(row["id"], row["revision"])
        self.assertEqual(next(record for record in self.manager.list_sources()["sources"]
                              if record["id"] == row["id"])["revision"], changed["revision"])
        cases = [{"type": "unknown"}, {"type": "ollama", "api_key": "not-supported"},
                 {"type": "litellm", "config": {"api_key": "plaintext"}},
                 {"type": "litellm", "config": {"base_url": "http://user:secret@host"}},
                 {"type": "litellm", "config": {"timeout": True}},
                 {"type": "litellm", "config": {"timeout": 1000}},
                 {"type": "litellm", "api_key": "bad key"},
                 {"type": "litellm", "api_key": "key", "clear_key": True}]
        for data in cases:
            with self.subTest(data=data), self.assertRaises(ValueError):
                self.manager.save(data)

    def test_multiple_instances_and_fake_exclusivity(self):
        with self.assertRaisesRegex(ValueError, "disable demo"):
            self.add_litellm(enabled=True)
        self.disable_fake()
        first = self.add_litellm("One", enabled=True)
        second = self.add_litellm("Two", enabled=True)
        self.assertNotEqual(first["id"], second["id"])
        self.storage.save_snapshot(SourceSnapshot(first["id"], "ok", metrics={"requests_today": 4}))
        self.storage.save_snapshot(SourceSnapshot(second["id"], "warning", metrics={"requests_today": 8}))
        self.storage.save_snapshot(SourceSnapshot("fake", "critical", metrics={"tokens_today": 999}))
        payload = self.manager.status_payload()
        self.assertEqual(payload["overall_status"], "warning")
        self.assertEqual(len(payload["sources"]), 2)
        self.assertEqual(payload["cards"][2]["primary"], "2 configured sources")
        self.assertIn("not summed", payload["cards"][2]["secondary"])

    def test_runtime_reloads_keys_and_rejects_stale_observations(self):
        self.disable_fake()
        public = self.add_litellm(enabled=True)
        runtime = ManagedCollector(self.manager, self.config, threading.Event())
        with patch.object(LiteLLMSource, "collect",
                          new=lambda source: source.snapshot("ok", metrics={"echo": source.api_key})):
            runtime.collect_once()
            self.assertEqual(self.storage.latest_snapshots()[public["id"]].metrics["echo"], "[redacted]")
            old = next(row for row in self.storage.source_instances() if row["id"] == public["id"])
            new = self.manager.save({"revision": public["revision"], "api_key": "new-key"}, public["id"])
            runtime.collect_once()
            self.assertEqual(runtime._cache[public["id"]][1].api_key, "new-key")
            self.manager.delete(public["id"], new["revision"])
            self.manager.save_observation(old, SourceSnapshot(public["id"], "critical"))
        self.assertEqual(self.storage.latest_snapshots()[public["id"]].status, "ok")

    def test_admin_token_validation_is_separate(self):
        for text in ('[admin]\nenabled="yes"', '[admin]\nport=0',
                     '[admin]\ntoken="bad token"',
                     '[daemon]\nshared_token="same"\n[admin]\ntoken="same"'):
            path = Path(self.temp.name) / "invalid.toml"
            path.write_text(text, encoding="utf-8")
            with self.assertRaises(ConfigError):
                load_config(path)


class AdminHttpTests(ManagementBase):
    def setUp(self):
        super().setUp()
        self.admin = AdminServer(0, self.manager, "unit-secret-admin-token")
        self.admin.start()
        self.addCleanup(self.admin.stop)
        self.base_url = "http://127.0.0.1:%d" % self.admin.actual_port
        self.opener = build_opener(ProxyHandler({}))

    def request(self, path="/api/admin/sources", method="GET", data=None,
                token="unit-secret-admin-token", headers=None, raw=None):
        extra = {"Authorization": "Bearer " + token} if token else {}
        if data is not None:
            extra["Content-Type"] = "application/json"
            raw = json.dumps(data).encode("utf-8")
        extra.update(headers or {})
        request = Request(self.base_url + path, data=raw, headers=extra, method=method)
        try:
            response = self.opener.open(request, timeout=3)
        except HTTPError as exc:
            response = exc
        with response:
            body = response.read()
            return response.status, dict(response.headers), (
                json.loads(body) if response.headers["Content-Type"] == "application/json" else body)

    def test_auth_host_origin_and_security_headers(self):
        for token in (None, "wrong", "device-token"):
            self.assertEqual(self.request(token=token)[0], 401)
        self.assertEqual(self.request(headers={"Origin": "https://hostile.example"})[0], 403)
        self.assertEqual(self.request(headers={"Host": "hostile.example"})[0], 403)
        status, headers, data = self.request()
        self.assertEqual(status, 200)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        self.assertNotIn("credential", json.dumps(data))
        self.assertEqual(self.admin._server.server_address[0], "127.0.0.1")

    def test_crud_masks_credentials_preserves_blank_and_rotates(self):
        status, _, row = self.request(method="POST", data={
            "type": "litellm", "name": "One", "api_key": "test-secret"})
        self.assertEqual(status, 201)
        self.assertNotIn("test-secret", json.dumps(row))
        path = "/api/admin/sources/" + row["id"]
        status, _, changed = self.request(path, "PUT", {
            "name": "Two", "revision": row["revision"], "api_key": ""})
        self.assertEqual(status, 200)
        self.assertTrue(changed["has_key"])
        self.assertEqual(self.request(path, "PUT", {"revision": row["revision"]})[0], 409)
        status, _, updated = self.request(path, "PUT", {
            "revision": changed["revision"], "api_key": "new-secret"})
        self.assertEqual(status, 200)
        self.assertNotIn("new-secret", json.dumps(updated))
        self.assertEqual(self.request(path, "DELETE", {"revision": updated["revision"]})[0], 200)
        self.assertEqual(self.request(path, "PUT", {"revision": updated["revision"]})[0], 404)

    def test_body_limits_errors_and_no_secret_echo(self):
        self.assertEqual(self.request(method="POST", raw=b"x=1",
                                      headers={"Content-Type": "application/x-www-form-urlencoded"})[0], 415)
        self.assertEqual(self.request(method="POST", raw=b"x" * 65537,
                                      headers={"Content-Type": "application/json"})[0], 413)
        self.assertEqual(self.request(method="POST", raw=b"{oops",
                                      headers={"Content-Type": "application/json"})[0], 400)
        status, _, body = self.request(method="POST", data={
            "type": "litellm", "config": {"api_key": "do-not-echo"}, "api_key": "secret"})
        self.assertEqual(status, 400)
        self.assertNotIn("do-not-echo", json.dumps(body))
        self.assertNotIn("secret", json.dumps(body))
        self.assertEqual(self.request(method="POST", data={},
                                      headers={"Origin": "null"})[0], 403)

    def test_assets_are_public_and_contain_no_tokens(self):
        for path in ("/", "/admin.js", "/admin.css"):
            status, _, body = self.request(path, token=None)
            self.assertEqual(status, 200)
            self.assertNotIn(b"unit-secret-admin-token", body)
        self.assertEqual(self.request("/../config.py")[0], 404)


class ManagedDaemonTests(ManagementBase):
    def test_daemon_applies_edits_and_reopens_database_without_admin(self):
        self.storage.close()
        self.config["daemon"]["poll_interval"] = 60
        with patch.object(OllamaSource, "collect",
                          new=lambda source: source.snapshot("active", metrics={"loaded_model": "fixture"})):
            daemon = Daemon(self.config).setup().start()
            try:
                manager = daemon.source_configuration
                fake = next(row for row in manager.list_sources()["sources"] if row["type"] == "fake")
                manager.save({"enabled": False, "revision": fake["revision"]}, fake["id"])
                ollama = next(row for row in manager.list_sources()["sources"] if row["type"] == "ollama")
                manager.save({"enabled": True, "revision": ollama["revision"]}, ollama["id"])
                deadline = time.monotonic() + 3
                while "ollama" not in daemon.storage.latest_snapshots():
                    self.assertLess(time.monotonic(), deadline, "live configuration was not applied")
                    time.sleep(0.05)
                request = Request("http://127.0.0.1:%d/api/v1/status" % daemon.api.actual_port,
                                  headers={"Authorization": "Bearer device-token"})
                with build_opener(ProxyHandler({})).open(request, timeout=3) as response:
                    payload = json.load(response)
                self.assertEqual(payload["sources"][0]["type"], "ollama")
                self.assertEqual(payload["cards"][1]["primary"], "fixture")
            finally:
                daemon.stop()
            self.assertFalse(daemon.collectors[0].thread.is_alive())
            self.assertFalse(daemon.admin._thread.is_alive())
            self.config["admin"]["enabled"] = False
            restarted = Daemon(self.config).setup().start()
            try:
                self.assertIsNone(restarted.admin)
                self.assertEqual(restarted.source_configuration.status_payload()["sources"][0]["type"], "ollama")
            finally:
                restarted.stop()


if __name__ == "__main__":
    unittest.main()
