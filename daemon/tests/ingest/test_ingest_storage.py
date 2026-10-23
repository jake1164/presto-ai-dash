"""Ingest tokens, the persisted switch, and schema v7 (plan Phase 3)."""

from pathlib import Path
import hashlib
import json
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from presto_ai_dash.config import ConfigError, ingest_enabled, load_config
from presto_ai_dash.storage import Storage
from envutil import clean_env
from admin.test_management import ManagementBase


def v5_database(path):
    """A schema v5 database with one already-collected Claude export row."""
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA user_version = 5")
    connection.execute("""CREATE TABLE source_instances (
        id TEXT PRIMARY KEY, type TEXT NOT NULL, name TEXT NOT NULL,
        enabled INTEGER NOT NULL, config TEXT NOT NULL,
        credential BLOB, revision INTEGER NOT NULL DEFAULT 1,
        position INTEGER NOT NULL DEFAULT 0)""")
    connection.execute("""CREATE TABLE claude_exports (
        instance_id TEXT PRIMARY KEY REFERENCES source_instances(id) ON DELETE CASCADE,
        report TEXT NOT NULL, evidence TEXT NOT NULL)""")
    connection.execute(
        "INSERT INTO source_instances (id, type, name, enabled, config)"
        " VALUES ('claude-1', 'claude', 'Claude', 1, '{}')")
    connection.execute("INSERT INTO claude_exports VALUES ('claude-1', ?, ?)",
                       (json.dumps({"windows": []}), json.dumps({"five_hour": "a" * 64})))
    connection.commit()
    connection.close()


class MigrationTests(unittest.TestCase):
    def test_v5_database_migrates_to_v7_and_keeps_every_row(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        path = Path(temp.name) / "dash.db"
        v5_database(path)
        storage = Storage(path)
        self.addCleanup(storage.close)
        self.assertEqual(storage._conn.execute("PRAGMA user_version").fetchone()[0], Storage.SCHEMA_VERSION)
        observation = storage.observation("claude-1")
        self.assertIsNotNone(observation)
        self.assertEqual(observation["evidence"], {"five_hour": "a" * 64})
        with self.assertRaises(sqlite3.OperationalError):
            storage._conn.execute("SELECT * FROM claude_exports").fetchone()
        columns = [row["name"] for row in
                   storage._conn.execute("PRAGMA table_info(ingest_tokens)")]
        self.assertEqual(columns, ["instance_id", "token_hash", "created_at"])

    def test_v6_database_migrates_to_v7_and_keeps_every_row(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        path = Path(temp.name) / "dash.db"
        v5_database(path)
        storage = Storage(path)
        storage.close()
        connection = sqlite3.connect(path)
        try:
            # Manufacture the documented v6 state: v6 schema, no ingest_tokens yet.
            connection.execute("DROP TABLE ingest_tokens")
            connection.execute("PRAGMA user_version = 6")
        finally:
            connection.close()
        storage = Storage(path)
        self.addCleanup(storage.close)
        self.assertEqual(storage._conn.execute("PRAGMA user_version").fetchone()[0], Storage.SCHEMA_VERSION)
        self.assertIsNotNone(storage.observation("claude-1"))
        storage.create_ingest_token("claude-1")  # The new table is usable again.
        self.assertTrue(storage.ingest_token_info("claude-1")["exists"])


class TokenTests(ManagementBase):
    def setUp(self):
        super().setUp()
        self.disable_fake()
        self.row = self.manager.save({"type": "claude", "name": "Claude personal",
                                      "enabled": True,
                                      "config": {"settings_path": str(Path(self.temp.name) / "settings.json")}})

    def raw_hashes(self):
        connection = sqlite3.connect(self.database)
        try:
            connection.row_factory = sqlite3.Row
            return [dict(row) for row in
                    connection.execute("SELECT * FROM ingest_tokens").fetchall()]
        finally:
            connection.close()

    def test_create_returns_plaintext_once_and_stores_only_the_hash(self):
        token = self.storage.create_ingest_token(self.row["id"])
        self.assertGreater(len(token), 40)
        stored = self.raw_hashes()
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0]["instance_id"], self.row["id"])
        self.assertNotEqual(stored[0]["token_hash"], token)
        self.assertEqual(stored[0]["token_hash"],
                         hashlib.sha256(token.encode("ascii")).hexdigest())
        # The info view is the only read path and must never expose the digest.
        info = self.storage.ingest_token_info(self.row["id"])
        self.assertEqual(info, {"exists": True, "created_at": stored[0]["created_at"]})
        self.assertNotIn("token_hash", json.dumps(info))
        self.assertNotIn(token, json.dumps(info))


    def test_rotation_replaces_the_hash_and_the_old_token_stops_working(self):
        first = self.storage.create_ingest_token(self.row["id"])
        second = self.storage.create_ingest_token(self.row["id"])
        self.assertNotEqual(first, second)
        self.assertEqual(len(self.raw_hashes()), 1)
        self.assertIsNone(self.storage.instance_for_ingest_token(first))
        self.assertEqual(self.storage.instance_for_ingest_token(second), self.row["id"])

    def test_revoke_removes_the_token(self):
        token = self.storage.create_ingest_token(self.row["id"])
        self.storage.revoke_ingest_token(self.row["id"])
        self.assertEqual(self.storage.ingest_token_info(self.row["id"]),
                         {"exists": False, "created_at": None})
        self.assertIsNone(self.storage.instance_for_ingest_token(token))
        self.assertEqual(self.raw_hashes(), [])

    def test_lookup_of_unknown_empty_or_odd_tokens_returns_none(self):
        for token in ("no-such-token", "", None, 5):
            with self.subTest(token=repr(token)):
                self.assertIsNone(self.storage.instance_for_ingest_token(token))

    def test_non_push_or_disabled_instance_refuses_a_token(self):
        fake = next(row for row in self.manager.list_sources()["sources"]
                    if row["type"] == "fake")  # already disabled, must also refuse
        with self.assertRaises(ValueError):
            self.storage.create_ingest_token(fake["id"])
        self.assertEqual(self.raw_hashes(), [])
        self.manager.save({"type": "litellm", "name": "Gateway", "enabled": True,
                           "config": {"base_url": "http://localhost:4000"}})
        live = next(row for row in self.storage.source_instances() if row["type"] == "litellm")
        with self.assertRaises(ValueError):
            self.storage.create_ingest_token(live["id"])
        disabled = self.manager.save({"enabled": False, "revision": self.row["revision"]},
                                     self.row["id"])
        with self.assertRaises(ValueError):
            self.storage.create_ingest_token(disabled["id"])

    def test_deleting_the_instance_deletes_its_token(self):
        token = self.storage.create_ingest_token(self.row["id"])
        latest = next(row for row in self.manager.list_sources()["sources"]
                      if row["id"] == self.row["id"])
        self.manager.delete(latest["id"], latest["revision"])
        self.assertEqual(self.raw_hashes(), [])
        self.assertIsNone(self.storage.instance_for_ingest_token(token))

    def test_source_instance_type_resolves_known_and_missing_rows(self):
        self.assertEqual(self.storage.source_instance_type(self.row["id"]), "claude")
        self.assertIsNone(self.storage.source_instance_type("no-such-instance"))


class IngestSettingTests(ManagementBase):
    def test_setting_defaults_to_unset_then_round_trips(self):
        self.assertIsNone(self.storage.ingest_enabled_setting())
        self.storage.save_ingest_enabled(True)
        self.assertIs(self.storage.ingest_enabled_setting(), True)
        connection = sqlite3.connect(self.database)
        try:
            stored = connection.execute(
                "SELECT value FROM configuration_meta WHERE name = 'ingest_enabled'"
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertIs(stored == b"1", self.storage.ingest_enabled_setting())
        self.storage.save_ingest_enabled(False)
        self.assertIs(self.storage.ingest_enabled_setting(), False)


class ConfigFlagTests(ManagementBase):
    def test_toml_default_false_and_bad_values_are_rejected(self):
        config = load_config()
        self.assertIs(config["daemon"]["ingest_enabled"], False)
        for text in ('[daemon]\ningest_enabled="maybe"', '[daemon]\ningest_enabled=1',
                     '[daemon]\ningest_enabled="yes"'):
            path = Path(self.temp.name) / "bad.toml"
            path.write_text(text, encoding="utf-8")
            with clean_env(), self.assertRaises(ConfigError):
                load_config(path)

    def test_environment_accepts_boolean_words_and_rejects_the_rest(self):
        for value, expected in (("1", True), ("true", True), ("YES", True), ("On", True),
                                ("0", False), ("false", False), ("No", False), ("OFF", False)):
            with clean_env(DASH_INGEST_ENABLED=value):
                self.assertIs(load_config()["daemon"]["ingest_enabled"], expected)
        for value in ("maybe", "2", "enabled", ""):
            with clean_env(DASH_INGEST_ENABLED=value), self.assertRaises(ConfigError):
                load_config()

    def test_environment_beats_storage_beats_toml(self):
        config = load_config()
        config["daemon"]["ingest_enabled"] = True  # as a TOML file could set it
        # No environment, nothing stored: the TOML value holds.
        self.assertIs(ingest_enabled(config), True)
        # A stored admin setting beats TOML.
        self.assertIs(ingest_enabled(config, False), False)
        # The environment beats both, re-read on every call.
        with patch.dict("os.environ", {"DASH_INGEST_ENABLED": "0"}):
            self.assertIs(ingest_enabled(config, True), False)
            self.assertIs(ingest_enabled(config, False, {"DASH_INGEST_ENABLED": "yes"}), True)


if __name__ == "__main__":
    unittest.main()
