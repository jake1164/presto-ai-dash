"""Storage v6 observations and the generic ingest path (plan Phase 1)."""

from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from presto_ai_dash.ingest import ingest
from presto_ai_dash.storage import Storage
from admin.test_management import ManagementBase

NOW = datetime(2026, 10, 18, 14, 0, 0, tzinfo=timezone.utc)


def envelope(now=None, percent=12.0, progress="a" * 64, host="laptop"):
    """A valid envelope with one measured and one unavailable window.

    The five_hour evidence hash derives from the reading, like the real
    collector's signal fingerprint, so a changed percent is new evidence.
    """
    now = now or datetime.now(timezone.utc)
    digest = hashlib.sha256(("five_hour:%r" % percent).encode("utf-8")).hexdigest()
    return {
        "schema": "presto-ai-dash.observation/1",
        "collector": {"name": "claude-statusline", "version": "1", "host": host},
        "client_time": now.isoformat(),
        "windows": [
            {"id": "five_hour", "label": "5h", "period_minutes": 300,
             "used_percent": percent, "resets_at": (now + timedelta(hours=5)).isoformat(),
             "state": "available"},
            {"id": "seven_day", "label": "Weekly", "period_minutes": 10080,
             "used_percent": None, "resets_at": None, "state": "unavailable"},
        ],
        "evidence": {"five_hour": digest, "seven_day": "b" * 64},
        "progress": progress,
    }


def heartbeat(now=None):
    """A heartbeat envelope: the collector ran but had no quota."""
    now = now or datetime.now(timezone.utc)
    return {"schema": "presto-ai-dash.observation/1",
            "collector": {"name": "claude-statusline", "version": "1", "host": "laptop"},
            "client_time": now.isoformat()}


class MigrationTests(unittest.TestCase):
    def test_v5_database_migrates_claude_export_rows(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        path = Path(temp.name) / "dash.db"
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
        report = {"provider": "anthropic", "product": "Claude", "windows": [
            {"id": "five_hour", "label": "5h", "period_minutes": 300, "used_percent": 23.5,
             "resets_at": "2026-10-18T19:00:00+00:00",
             "observed_at": "2026-10-18T14:00:00+00:00", "state": "available"}],
            "stale_after_seconds": 900, "error": None}
        evidence = {"five_hour": "c" * 64}
        connection.execute(
            "INSERT INTO source_instances (id, type, name, enabled, config)"
            " VALUES ('claude-1', 'claude', 'Claude', 1, '{}')")
        connection.execute("INSERT INTO claude_exports VALUES ('claude-1', ?, ?)",
                           (json.dumps(report), json.dumps(evidence)))
        connection.commit()
        connection.close()
        storage = Storage(path)
        self.addCleanup(storage.close)
        self.assertEqual(storage._conn.execute("PRAGMA user_version").fetchone()[0], Storage.SCHEMA_VERSION)
        observation = storage.observation("claude-1")
        self.assertEqual(observation["report"], report)
        self.assertEqual(observation["evidence"], evidence)
        with self.assertRaises(sqlite3.OperationalError):
            storage._conn.execute("SELECT * FROM claude_exports").fetchone()


class IngestObservationTests(ManagementBase):
    def setUp(self):
        super().setUp()
        self.disable_fake()
        self.row = self.manager.save({"type": "claude", "name": "Claude personal",
                                      "enabled": True,
                                      "config": {"settings_path": str(Path(self.temp.name) / "settings.json")}})

    def test_unknown_or_disabled_instance_is_rejected(self):
        self.assertFalse(ingest(self.storage, "missing-instance", envelope(), NOW))
        self.assertIsNone(self.storage.observation("missing-instance"))
        disabled = self.manager.save({"enabled": False, "revision": self.row["revision"]}, self.row["id"])
        self.assertFalse(ingest(self.storage, self.row["id"], envelope(), NOW))
        self.assertIsNone(self.storage.observation(self.row["id"]))

    def test_non_push_instance_is_rejected(self):
        row = self.manager.save({"type": "ollama", "name": "Local models", "enabled": True})
        self.assertFalse(ingest(self.storage, row["id"], envelope(), NOW))
        self.assertIsNone(self.storage.observation(row["id"]))

    def test_deleting_the_instance_deletes_its_observation(self):
        self.assertTrue(ingest(self.storage, self.row["id"], envelope(), NOW))
        self.assertIsNotNone(self.storage.observation(self.row["id"]))
        self.manager.delete(self.row["id"], self.row["revision"])
        self.assertIsNone(self.storage.observation(self.row["id"]))

    def test_heartbeat_never_changes_quota(self):
        self.assertTrue(ingest(self.storage, self.row["id"], envelope(), NOW))
        before = self.storage.observation(self.row["id"])
        moment = NOW + timedelta(seconds=30)
        self.assertTrue(ingest(self.storage, self.row["id"], heartbeat(moment), moment))
        after = self.storage.observation(self.row["id"])
        self.assertEqual(after["report"], before["report"])
        self.assertEqual(after["evidence"], before["evidence"])
        self.assertEqual(after["last_progress_at"], before["last_progress_at"])
        self.assertEqual(datetime.fromisoformat(after["collector_seen_at"]), moment)

    def test_unchanged_evidence_keeps_the_old_observed_at(self):
        self.assertTrue(ingest(self.storage, self.row["id"], envelope(), NOW))
        first = self.storage.observation(self.row["id"])["report"]
        self.assertEqual(datetime.fromisoformat(first["windows"][0]["observed_at"]), NOW)
        unchanged = envelope()
        self.assertTrue(ingest(self.storage, self.row["id"], unchanged, NOW + timedelta(minutes=10)))
        current = self.storage.observation(self.row["id"])["report"]
        self.assertEqual(datetime.fromisoformat(current["windows"][0]["observed_at"]), NOW)
        changed = envelope(percent=55.0)
        self.assertTrue(ingest(self.storage, self.row["id"], changed, NOW + timedelta(minutes=20)))
        current = self.storage.observation(self.row["id"])["report"]
        self.assertEqual(datetime.fromisoformat(current["windows"][0]["observed_at"]),
                         NOW + timedelta(minutes=20))

    def test_changed_progress_sets_last_progress_at(self):
        self.assertTrue(ingest(self.storage, self.row["id"], envelope(progress="a" * 64), NOW))
        row = self.storage.observation(self.row["id"])
        self.assertEqual(datetime.fromisoformat(row["last_progress_at"]), NOW)
        unchanged = envelope(progress="a" * 64)
        self.assertTrue(ingest(self.storage, self.row["id"], unchanged, NOW + timedelta(minutes=5)))
        row = self.storage.observation(self.row["id"])
        self.assertEqual(datetime.fromisoformat(row["last_progress_at"]), NOW)
        changed = envelope(progress="b" * 64)
        self.assertTrue(ingest(self.storage, self.row["id"], changed, NOW + timedelta(minutes=10)))
        row = self.storage.observation(self.row["id"])
        self.assertEqual(datetime.fromisoformat(row["last_progress_at"]), NOW + timedelta(minutes=10))

    def test_newest_reading_wins(self):
        self.assertTrue(ingest(self.storage, self.row["id"], envelope(percent=10.0), NOW))
        self.assertTrue(ingest(self.storage, self.row["id"], envelope(percent=20.0),
                               NOW + timedelta(minutes=5)))
        report = self.storage.observation(self.row["id"])["report"]
        self.assertEqual(report["windows"][0]["used_percent"], 20.0)
        self.assertEqual(datetime.fromisoformat(report["windows"][0]["observed_at"]),
                         NOW + timedelta(minutes=5))

    def test_observation_row_records_collector_metadata(self):
        sent = envelope(host="laptop")  # client_time is the real clock; NOW is the daemon's
        self.assertTrue(ingest(self.storage, self.row["id"], sent, NOW))
        row = self.storage.observation(self.row["id"])
        self.assertEqual(row["collector_host"], "laptop")
        self.assertEqual(row["collector_version"], "1")
        self.assertEqual(datetime.fromisoformat(row["collector_seen_at"]), NOW)


if __name__ == "__main__":
    unittest.main()
