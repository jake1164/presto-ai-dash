"""Minimal SQLite snapshot storage; retention and alert history belong to M3."""

import json
import sqlite3
import threading
from datetime import datetime

from .models import SourceSnapshot, iso


class Storage:
    def __init__(self, path):
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("""CREATE TABLE IF NOT EXISTS snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_id TEXT NOT NULL, status TEXT NOT NULL,
            updated_at TEXT NOT NULL, metrics TEXT NOT NULL,
            alerts TEXT NOT NULL, error TEXT
        )""")
        self._conn.commit()

    def close(self):
        with self._lock:
            self._conn.close()

    def save_snapshot(self, snapshot):
        with self._lock:
            cursor = self._conn.execute(
                "INSERT INTO snapshots (source_id, status, updated_at, metrics, alerts, error) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (snapshot.source_id, snapshot.status, iso(snapshot.updated_at),
                 json.dumps(snapshot.metrics), json.dumps(snapshot.alerts), snapshot.error))
            self._conn.commit()
            return cursor.lastrowid

    def latest_snapshots(self):
        # Row id breaks timestamp ties deterministically; one row per source.
        with self._lock:
            rows = self._conn.execute(
                "SELECT s.* FROM snapshots s WHERE s.id = "
                "(SELECT id FROM snapshots WHERE source_id = s.source_id "
                "ORDER BY updated_at DESC, id DESC LIMIT 1)").fetchall()
        return {row["source_id"]: self._row_to_snapshot(row) for row in rows}

    @staticmethod
    def _row_to_snapshot(row):
        return SourceSnapshot(
            source_id=row["source_id"], status=row["status"],
            updated_at=datetime.fromisoformat(row["updated_at"]),
            metrics=json.loads(row["metrics"]), alerts=json.loads(row["alerts"]),
            error=row["error"])
