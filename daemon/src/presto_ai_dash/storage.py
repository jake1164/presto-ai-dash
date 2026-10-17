"""UTC-normalized SQLite snapshot and alert-observation history."""

import json
from copy import deepcopy
import sqlite3
import threading
from datetime import datetime, timedelta, timezone

from .models import STATUS_ORDER, SourceSnapshot


def _utc(moment):
    if not isinstance(moment, datetime):
        raise ValueError("timestamp must be a datetime")
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def _stored_time(moment):
    return _utc(moment).isoformat(timespec="microseconds")


def _read_time(value):
    try:
        return _utc(datetime.fromisoformat(value))
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid stored timestamp: %r" % (value,)) from exc


def _validated_alerts(value):
    if not isinstance(value, list):
        raise ValueError("alerts must be a list")
    alerts = []
    seen = set()
    for alert in value:
        if not isinstance(alert, dict):
            raise ValueError("each alert must be an object")
        severity = alert.get("severity")
        message = alert.get("message")
        if severity not in STATUS_ORDER:
            raise ValueError("alert severity must be canonical")
        if not isinstance(message, str) or not message.strip():
            raise ValueError("alert message must be a nonempty string")
        pair = severity, message
        if pair not in seen:
            alerts.append(pair)
            seen.add(pair)
    return alerts


class Storage:
    SCHEMA_VERSION = 5
    MAX_HISTORY_LIMIT = 1000

    def __init__(self, path):
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._closed = False
        try:
            self._conn.execute("PRAGMA foreign_keys = ON")
            self._migrate()
        except Exception:
            self._conn.close()
            self._closed = True
            raise

    def _migrate(self):
        version = self._conn.execute("PRAGMA user_version").fetchone()[0]
        if version > self.SCHEMA_VERSION:
            raise RuntimeError("database schema version %d is newer than supported version %d" %
                               (version, self.SCHEMA_VERSION))
        if version == self.SCHEMA_VERSION:
            return
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            self._conn.execute("""CREATE TABLE IF NOT EXISTS snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_id TEXT NOT NULL, status TEXT NOT NULL,
                updated_at TEXT NOT NULL, metrics TEXT NOT NULL,
                alerts TEXT NOT NULL, error TEXT
            )""")
            self._conn.execute("""CREATE TABLE IF NOT EXISTS alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                snapshot_id INTEGER NOT NULL REFERENCES snapshots(id) ON DELETE CASCADE,
                severity TEXT NOT NULL, message TEXT NOT NULL,
                UNIQUE(snapshot_id, severity, message)
            )""")
            rows = (self._conn.execute(
                "SELECT id, updated_at, alerts FROM snapshots ORDER BY id").fetchall()
                    if version < 1 else [])
            for row in rows:
                updated_at = _stored_time(_read_time(row["updated_at"]))
                alerts = _validated_alerts(json.loads(row["alerts"]))
                self._conn.execute("UPDATE snapshots SET updated_at = ? WHERE id = ?",
                                   (updated_at, row["id"]))
                self._conn.executemany(
                    "INSERT OR IGNORE INTO alerts (snapshot_id, severity, message) VALUES (?, ?, ?)",
                    ((row["id"], severity, message) for severity, message in alerts))
            self._conn.execute("CREATE INDEX IF NOT EXISTS idx_snapshots_source_time "
                               "ON snapshots(source_id, updated_at, id)")
            self._conn.execute("CREATE INDEX IF NOT EXISTS idx_snapshots_time "
                               "ON snapshots(updated_at, id)")
            self._conn.execute("CREATE INDEX IF NOT EXISTS idx_alerts_snapshot "
                               "ON alerts(snapshot_id, id)")
            self._conn.execute("""CREATE TABLE IF NOT EXISTS source_instances (
                id TEXT PRIMARY KEY, type TEXT NOT NULL, name TEXT NOT NULL,
                enabled INTEGER NOT NULL, config TEXT NOT NULL,
                credential BLOB, revision INTEGER NOT NULL DEFAULT 1
            )""")
            self._conn.execute("""CREATE TABLE IF NOT EXISTS configuration_meta (
                name TEXT PRIMARY KEY, value BLOB NOT NULL
            )""")
            self._conn.execute("""CREATE TABLE IF NOT EXISTS admin_sessions (
                token_hash TEXT PRIMARY KEY, csrf_token TEXT NOT NULL,
                expires_at REAL NOT NULL
            )""")
            self._conn.execute("""CREATE TABLE IF NOT EXISTS claude_exports (
                instance_id TEXT PRIMARY KEY REFERENCES source_instances(id) ON DELETE CASCADE,
                report TEXT NOT NULL, evidence TEXT NOT NULL
            )""")
            self._conn.execute("""CREATE TABLE IF NOT EXISTS claude_bridges (
                instance_id TEXT PRIMARY KEY REFERENCES source_instances(id) ON DELETE CASCADE,
                settings_path TEXT NOT NULL UNIQUE, previous BLOB NOT NULL, command TEXT NOT NULL
            )""")
            columns = {row["name"] for row in self._conn.execute("PRAGMA table_info(source_instances)")}
            if "position" not in columns:
                self._conn.execute("ALTER TABLE source_instances ADD COLUMN position INTEGER NOT NULL DEFAULT 0")
                self._conn.execute("UPDATE source_instances SET position = rowid")
            self._conn.execute("PRAGMA user_version = 5")
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    def close(self):
        with self._lock:
            if not self._closed:
                self._conn.close()
                self._closed = True

    def configuration_marker(self):
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM configuration_meta WHERE name = 'vault_check'").fetchone()
            return bytes(row["value"]) if row else None

    def claude_export(self, instance_id):
        with self._lock:
            row = self._conn.execute("SELECT report FROM claude_exports WHERE instance_id = ?",
                                     (instance_id,)).fetchone()
        return json.loads(row["report"]) if row else None

    def save_claude_export(self, instance_id, report, evidence):
        """Atomic account routing and freshness preservation across helper processes."""
        result = deepcopy(report)
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                account = self._conn.execute("SELECT type, enabled FROM source_instances WHERE id = ?",
                                             (instance_id,)).fetchone()
                if not account or account["type"] != "claude" or not account["enabled"]:
                    self._conn.rollback()
                    return False
                previous = self._conn.execute("SELECT report, evidence FROM claude_exports WHERE instance_id = ?",
                                              (instance_id,)).fetchone()
                if previous:
                    old_evidence = json.loads(previous["evidence"])
                    old_windows = {w["id"]: w for w in json.loads(previous["report"])["windows"]}
                    for window in result["windows"]:
                        key = window["id"]
                        if key in old_windows and old_evidence.get(key) == evidence.get(key):
                            window["observed_at"] = old_windows[key]["observed_at"]
                self._conn.execute("INSERT INTO claude_exports VALUES (?, ?, ?) "
                    "ON CONFLICT(instance_id) DO UPDATE SET report=excluded.report, evidence=excluded.evidence",
                    (instance_id, json.dumps(result), json.dumps(evidence)))
                self._conn.commit()
                return True
            except Exception:
                self._conn.rollback()
                raise

    def claude_bridge(self, instance_id):
        with self._lock:
            row = self._conn.execute("SELECT * FROM claude_bridges WHERE instance_id = ?",
                                     (instance_id,)).fetchone()
        return dict(row) if row else None

    def save_claude_bridge(self, instance_id, settings_path, previous, command):
        try:
            with self._lock, self._conn:
                self._conn.execute("INSERT INTO claude_bridges VALUES (?, ?, ?, ?)",
                                   (instance_id, settings_path, previous, command))
        except sqlite3.IntegrityError:
            raise ValueError("This Claude settings profile already has an export account") from None

    def remove_claude_bridge(self, instance_id):
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM claude_bridges WHERE instance_id = ?", (instance_id,))

    def create_admin_session(self, token_hash, csrf_token, expires_at, now):
        """Store only a digest of the browser's bearer credential."""
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM admin_sessions WHERE expires_at <= ?", (now,))
            self._conn.execute(
                "INSERT INTO admin_sessions (token_hash, csrf_token, expires_at) VALUES (?, ?, ?)",
                (token_hash, csrf_token, expires_at))

    def admin_session(self, token_hash, now):
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM admin_sessions WHERE expires_at <= ?", (now,))
            row = self._conn.execute(
                "SELECT csrf_token, expires_at FROM admin_sessions WHERE token_hash = ?",
                (token_hash,)).fetchone()
            return dict(row) if row else None

    def revoke_admin_session(self, token_hash):
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM admin_sessions WHERE token_hash = ?", (token_hash,))

    def source_instances(self):
        """Internal records; callers must strip encrypted credential blobs for public output."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM source_instances ORDER BY position, rowid").fetchall()
        return [{**dict(row), "enabled": bool(row["enabled"]),
                 "config": json.loads(row["config"])} for row in rows]

    def initialize_configuration(self, records, vault_check):
        with self._lock:
            try:
                if self.configuration_marker() is not None:
                    return
                self._conn.executemany(
                    "INSERT INTO source_instances (id, type, name, enabled, config, credential, position) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    [(record["id"], record["type"], record["name"], record["enabled"],
                      json.dumps(record["config"]), record["credential"], position)
                     for position, record in enumerate(records)])
                self._conn.execute(
                    "INSERT INTO configuration_meta (name, value) VALUES ('vault_check', ?)",
                    (vault_check,))
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def save_source_instance(self, record, expected_revision):
        with self._lock:
            try:
                current = self._conn.execute(
                    "SELECT type, revision FROM source_instances WHERE id = ?",
                    (record["id"],)).fetchone()
                if expected_revision == 0 and current is not None:
                    raise ConfigurationConflict("source already exists")
                if expected_revision != 0 and (current is None or
                        current["revision"] != expected_revision):
                    raise ConfigurationConflict("source changed; reload before saving")
                if current and current["type"] != record["type"]:
                    raise ValueError("source type cannot change; create a new source")
                if record["enabled"]:
                    others = self._conn.execute(
                        "SELECT type FROM source_instances WHERE enabled = 1 AND id != ?",
                        (record["id"],)).fetchall()
                    if any(record["type"] == "fake" or row["type"] == "fake" for row in others):
                        raise ValueError("disable demo sources before enabling live sources")
                self._conn.execute(
                    "INSERT INTO source_instances (id, type, name, enabled, config, credential, revision, position) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, (SELECT COALESCE(MAX(position), -1) + 1 FROM source_instances)) "
                    "ON CONFLICT(id) DO UPDATE SET "
                    "name=excluded.name, enabled=excluded.enabled, config=excluded.config, "
                    "credential=excluded.credential, revision=excluded.revision",
                    (record["id"], record["type"], record["name"], record["enabled"],
                     json.dumps(record["config"]), record["credential"], expected_revision + 1))
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def reorder_source_instances(self, order, expected_order):
        """Save a complete sequence atomically, rejecting stale browser lists."""
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                current = [row["id"] for row in self._conn.execute(
                    "SELECT id FROM source_instances ORDER BY position, rowid")]
                if current != expected_order:
                    raise ConfigurationConflict("account list changed; reload before reordering")
                if len(order) != len(current) or set(order) != set(current):
                    raise ValueError("order must include every account exactly once")
                self._conn.executemany("UPDATE source_instances SET position = ? WHERE id = ?",
                                       enumerate(order))
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def delete_source_instance(self, instance_id, expected_revision):
        with self._lock:
            try:
                row = self._conn.execute(
                    "SELECT revision FROM source_instances WHERE id = ?", (instance_id,)).fetchone()
                if row is None:
                    raise KeyError(instance_id)
                if row["revision"] != expected_revision:
                    raise ConfigurationConflict("source changed; reload before deleting")
                self._conn.execute("DELETE FROM source_instances WHERE id = ?", (instance_id,))
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def save_snapshot(self, snapshot):
        timestamp = _stored_time(snapshot.updated_at)
        alerts = _validated_alerts(snapshot.alerts)
        metrics_json = json.dumps(snapshot.metrics)
        alerts_json = json.dumps(snapshot.alerts)
        with self._lock:
            try:
                cursor = self._conn.execute(
                    "INSERT INTO snapshots (source_id, status, updated_at, metrics, alerts, error) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (snapshot.source_id, snapshot.status, timestamp,
                     metrics_json, alerts_json, snapshot.error))
                snapshot_id = cursor.lastrowid
                self._conn.executemany(
                    "INSERT INTO alerts (snapshot_id, severity, message) VALUES (?, ?, ?)",
                    ((snapshot_id, severity, message) for severity, message in alerts))
                self._conn.commit()
                return snapshot_id
            except Exception:
                self._conn.rollback()
                raise

    def latest_snapshots(self):
        with self._lock:
            rows = self._conn.execute(
                "SELECT s.* FROM snapshots s WHERE s.id = "
                "(SELECT id FROM snapshots WHERE source_id = s.source_id "
                "ORDER BY updated_at DESC, id DESC LIMIT 1)").fetchall()
        return {row["source_id"]: self._row_to_snapshot(row) for row in rows}

    @staticmethod
    def _validate_range(since, until):
        start = _stored_time(since)
        end = _stored_time(until) if until is not None else None
        if end is not None and end <= start:
            raise ValueError("until must be later than since")
        return start, end

    @classmethod
    def _validate_limit(cls, limit):
        if type(limit) is not int or not 1 <= limit <= cls.MAX_HISTORY_LIMIT:
            raise ValueError("limit must be an integer from 1 through 1000")

    def snapshot_history(self, source_id, since, until=None, limit=1000):
        self._validate_limit(limit)
        start, end = self._validate_range(since, until)
        sql = "SELECT * FROM snapshots WHERE source_id = ? AND updated_at >= ?"
        params = [source_id, start]
        if end is not None:
            sql += " AND updated_at < ?"
            params.append(end)
        sql += " ORDER BY updated_at DESC, id DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [self._row_to_snapshot(row) for row in reversed(rows)]

    def alert_history(self, source_id, since, until=None, limit=1000):
        self._validate_limit(limit)
        start, end = self._validate_range(since, until)
        sql = ("SELECT a.id, a.snapshot_id, s.source_id, a.severity, a.message, s.updated_at "
               "FROM alerts a JOIN snapshots s ON s.id = a.snapshot_id "
               "WHERE s.source_id = ? AND s.updated_at >= ?")
        params = [source_id, start]
        if end is not None:
            sql += " AND s.updated_at < ?"
            params.append(end)
        sql += " ORDER BY s.updated_at DESC, a.id DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [{"id": row["id"], "snapshot_id": row["snapshot_id"],
                 "source_id": row["source_id"], "severity": row["severity"],
                 "message": row["message"], "observed_at": _read_time(row["updated_at"])}
                for row in reversed(rows)]

    def prune(self, now, retention_hours):
        if type(retention_hours) is not int or retention_hours <= 0:
            raise ValueError("retention_hours must be a positive integer")
        # Strictly-before deletion keeps records exactly on the cutoff.
        cutoff = _stored_time(_utc(now) - timedelta(hours=retention_hours))
        with self._lock:
            try:
                snapshots = self._conn.execute(
                    "SELECT COUNT(*) FROM snapshots WHERE updated_at < ?", (cutoff,)).fetchone()[0]
                alerts = self._conn.execute(
                    "SELECT COUNT(*) FROM alerts WHERE snapshot_id IN "
                    "(SELECT id FROM snapshots WHERE updated_at < ?)", (cutoff,)).fetchone()[0]
                self._conn.execute("DELETE FROM snapshots WHERE updated_at < ?", (cutoff,))
                self._conn.commit()
                return {"snapshots": snapshots, "alerts": alerts}
            except Exception:
                self._conn.rollback()
                raise

    @staticmethod
    def _row_to_snapshot(row):
        return SourceSnapshot(
            source_id=row["source_id"], status=row["status"],
            updated_at=_read_time(row["updated_at"]),
            metrics=json.loads(row["metrics"]), alerts=json.loads(row["alerts"]),
            error=row["error"])


class ConfigurationConflict(ValueError):
    """Optimistic edit conflict; the caller should reload the source record."""
