"""UTC-normalized SQLite snapshot and alert-observation history."""

import hashlib
import json
from copy import deepcopy
import secrets
import sqlite3
import threading
from datetime import datetime, timedelta, timezone

from . import private_files
from .models import STATUS_ORDER, SourceSnapshot, now_utc
from .sources import source_type

# The alert settings the admin page can store. Each one is stored under
# its own name in configuration_meta.
ALERT_SETTING_KEYS = ("enabled", "warning_percent", "critical_percent", "sound",
                      "quiet_hours", "quiet_start", "quiet_end", "pop_to_top",
                      "mute_hours")

# The per-service alert settings live in configuration_meta too, under
# "alerts.service.<instance id>". They are not source configuration fields:
# editing a source rebuilds it and rescans its logs, and a toggle must
# never do that.
SERVICE_ALERT_PREFIX = "alerts.service."


def _service_alert_name(instance_id):
    """The configuration_meta name one service's alert settings use."""
    if not isinstance(instance_id, str) or not instance_id:
        raise ValueError("instance id must be a nonempty string")
    if any(char not in
           "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-"
           for char in instance_id):
        raise ValueError("instance id has unusable characters")
    return SERVICE_ALERT_PREFIX + instance_id


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


def _token_digest(token):
    """The SHA-256 lookup key of a presented ingest token, or None for junk.

    Tokens are stored and compared only as digests; presentation is equality
    on the unique hash column, so the plaintext never reaches the database.
    """
    if not isinstance(token, str) or not token:
        return None
    try:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()
    except UnicodeError:
        return None


class Storage:
    SCHEMA_VERSION = 10
    # Live browser sessions the admin page keeps; the oldest go first.
    ADMIN_SESSION_LIMIT = 25
    MAX_HISTORY_LIMIT = 1000

    def __init__(self, path):
        self._lock = threading.RLock()
        # A new database is born owner-only, not made so after the fact.
        private_files.create_private(path)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._closed = False
        try:
            self._conn.execute("PRAGMA foreign_keys = ON")
            self._migrate()
            # One made by an earlier version took its permissions from its folder.
            private_files.protect(path)
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
            if version < 6:
                # v6: provider-neutral observations replace the Claude-only export
                # table. last_activity_at is created now (unused until Phase 5) so
                # activity support will not need another migration.
                self._conn.execute("""CREATE TABLE IF NOT EXISTS observations (
                    instance_id TEXT PRIMARY KEY REFERENCES source_instances(id) ON DELETE CASCADE,
                    report TEXT, evidence TEXT,
                    collector_seen_at TEXT, collector_host TEXT, collector_version TEXT,
                    last_progress_at TEXT, progress TEXT, last_activity_at TEXT
                )""")
                exports = self._conn.execute(
                    "SELECT instance_id, report, evidence FROM claude_exports").fetchall()
                for export in exports:
                    self._conn.execute(
                        "INSERT INTO observations (instance_id, report, evidence) VALUES (?, ?, ?)",
                        (export["instance_id"], export["report"], export["evidence"]))
                self._conn.execute("DROP TABLE claude_exports")
                self._conn.execute("PRAGMA user_version = 6")
            # v7: per-instance remote ingest tokens. Only the SHA-256 hash is
            # stored; the plaintext is shown once by create_ingest_token.
            self._conn.execute("""CREATE TABLE IF NOT EXISTS ingest_tokens (
                instance_id TEXT PRIMARY KEY REFERENCES source_instances(id) ON DELETE CASCADE,
                token_hash TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL
            )""")
            self._conn.execute("PRAGMA user_version = 7")
            # v8: a browser session is bound to the admin token that made it, so
            # rotating a configured token ends the sessions made under the old
            # one. Sessions from before this version have no binding, and so end.
            columns = {row["name"] for row in self._conn.execute("PRAGMA table_info(admin_sessions)")}
            if "binding" not in columns:
                self._conn.execute(
                    "ALTER TABLE admin_sessions ADD COLUMN binding TEXT NOT NULL DEFAULT ''")
            self._conn.execute("PRAGMA user_version = 8")
            # v9: the demo source is gone. An upgrade drops its saved instance
            # and its readings (the alerts cascade with the snapshots), so no
            # row remains that the daemon can no longer serve. Demo mode is a
            # switch now, not a source.
            self._conn.execute("DELETE FROM source_instances WHERE type = 'fake'")
            self._conn.execute("DELETE FROM snapshots WHERE source_id = 'fake'")
            self._conn.execute("PRAGMA user_version = 9")
            # v10: durable device identity and update state, so an unresolved
            # problem survives a daemon or container restart. Only what the
            # protocol needs is stored: no diagnostics, no addresses, no
            # credentials, no provider data.
            self._conn.execute("""CREATE TABLE IF NOT EXISTS devices (
                device_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                scheme TEXT,
                revision TEXT,
                bootstrap_version INTEGER,
                capabilities_json TEXT NOT NULL,
                slot TEXT,
                boot_confirmed INTEGER NOT NULL CHECK (boot_confirmed IN (0, 1)),
                boot INTEGER NOT NULL CHECK (boot >= 1),
                seq INTEGER NOT NULL CHECK (seq >= 1),
                report_json TEXT NOT NULL,
                update_state TEXT NOT NULL,
                reason TEXT,
                attention_latched INTEGER NOT NULL DEFAULT 0
                    CHECK (attention_latched IN (0, 1)),
                active_generation_id TEXT,
                active_attempt_id TEXT,
                state_revision INTEGER NOT NULL DEFAULT 1 CHECK (state_revision >= 1),
                recovery_request_id TEXT,
                recovery_requested_at TEXT,
                recovery_after_boot INTEGER,
                recovery_after_seq INTEGER,
                recovery_target_scheme TEXT,
                recovery_target_revision TEXT,
                last_recovery_at TEXT,
                last_recovery_kind TEXT
            )""")
            self._conn.execute("""CREATE TABLE IF NOT EXISTS device_update_budgets (
                generation_id TEXT PRIMARY KEY,
                device_id TEXT NOT NULL REFERENCES devices(device_id) ON DELETE CASCADE,
                target_scheme TEXT NOT NULL,
                target_revision TEXT NOT NULL,
                attempts_started INTEGER NOT NULL DEFAULT 0
                    CHECK (attempts_started BETWEEN 0 AND 3),
                status TEXT NOT NULL
                    CHECK (status IN ('active', 'exhausted', 'succeeded', 'superseded')),
                next_attempt_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )""")
            self._conn.execute("""CREATE TABLE IF NOT EXISTS device_update_attempts (
                attempt_id TEXT PRIMARY KEY,
                generation_id TEXT NOT NULL
                    REFERENCES device_update_budgets(generation_id) ON DELETE CASCADE,
                command_id TEXT NOT NULL UNIQUE,
                attempt_no INTEGER NOT NULL CHECK (attempt_no BETWEEN 1 AND 3),
                status TEXT NOT NULL CHECK (status IN (
                    'queued', 'started', 'confirming', 'succeeded', 'failed',
                    'blocked', 'unconfirmed', 'cancelled')),
                issued_at TEXT NOT NULL,
                started_at TEXT,
                attempt_deadline TEXT,
                confirmation_deadline TEXT,
                confirmation_acked_at TEXT,
                finished_at TEXT,
                last_phase TEXT,
                error_code TEXT,
                retryable INTEGER NOT NULL DEFAULT 0 CHECK (retryable IN (0, 1)),
                recovery TEXT,
                UNIQUE(generation_id, attempt_no)
            )""")
            self._conn.execute("""CREATE TABLE IF NOT EXISTS device_admin_requests (
                device_id TEXT NOT NULL REFERENCES devices(device_id) ON DELETE CASCADE,
                request_key TEXT NOT NULL,
                body_sha256 TEXT NOT NULL,
                response_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (device_id, request_key)
            )""")
            self._conn.execute("""CREATE TABLE IF NOT EXISTS daemon_release_cache (
                channel TEXT PRIMARY KEY,
                running TEXT NOT NULL,
                latest TEXT,
                checked_at TEXT,
                last_attempt_at TEXT,
                state TEXT NOT NULL,
                error_code TEXT,
                source_url TEXT
            )""")
            self._conn.execute("CREATE INDEX IF NOT EXISTS idx_devices_last_seen "
                               "ON devices(last_seen_at)")
            self._conn.execute("CREATE INDEX IF NOT EXISTS idx_device_budgets_device "
                               "ON device_update_budgets(device_id, status)")
            # One live automatic budget per device, whatever else is retained.
            self._conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_device_budgets_active "
                               "ON device_update_budgets(device_id) WHERE status = 'active'")
            self._conn.execute("CREATE INDEX IF NOT EXISTS idx_device_budgets_retry "
                               "ON device_update_budgets(status, next_attempt_at)")
            self._conn.execute("CREATE INDEX IF NOT EXISTS idx_device_attempts_generation "
                               "ON device_update_attempts(generation_id, attempt_no)")
            self._conn.execute("CREATE INDEX IF NOT EXISTS idx_device_admin_requests_created "
                               "ON device_admin_requests(device_id, created_at)")
            self._conn.execute("PRAGMA user_version = 10")
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

    def observation(self, instance_id):
        """The newest pushed observation for an instance, or None if it never reported."""
        with self._lock:
            row = self._conn.execute("SELECT * FROM observations WHERE instance_id = ?",
                                     (instance_id,)).fetchone()
        if row is None:
            return None
        return {"report": json.loads(row["report"]) if row["report"] is not None else None,
                "evidence": json.loads(row["evidence"]) if row["evidence"] is not None else {},
                "collector_seen_at": row["collector_seen_at"],
                "collector_host": row["collector_host"],
                "collector_version": row["collector_version"],
                "last_progress_at": row["last_progress_at"],
                "progress": row["progress"],
                "last_activity_at": row["last_activity_at"]}

    # Readings a collector pushes beside the quota windows. A later push of the
    # windows alone (a status line after a response) must not erase them.
    PUSHED_KEYS = ("pushed_activity", "pushed_usage_cache")

    def save_observation(self, instance_id, report, evidence, collector_host=None,
                         collector_version=None, progress=None, now=None, extras=None,
                         base_report=None):
        """Atomic account routing and freshness preservation across helper processes.

        The daemon owns time: a window's observed_at becomes this receive time
        unless its evidence hash matches the stored one, which keeps the old
        timestamp, and collector_seen_at is this receive time too — never the
        client's clock. A changed progress hash updates last_progress_at. A
        None report is a heartbeat: collector connectivity only, quota
        untouched. A heartbeat may carry ``extras``, readings that belong in the
        stored report (``base_report``, with every window unavailable, stands in
        when nothing has been stored yet); the quota windows stay as they are.
        """
        result = deepcopy(report)
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                account = self._conn.execute("SELECT type, enabled FROM source_instances WHERE id = ?",
                                             (instance_id,)).fetchone()
                if not account or not account["enabled"]:
                    self._conn.rollback()
                    return False
                previous = self._conn.execute(
                    "SELECT report, evidence, progress, last_progress_at FROM observations"
                    " WHERE instance_id = ?", (instance_id,)).fetchone()
                observed = _stored_time(now) if now is not None else _stored_time(now_utc())
                if result is None and extras:
                    # Readings with no new quota: merged into the report as it is.
                    if previous is not None and previous["report"]:
                        merged = json.loads(previous["report"])
                    else:
                        merged = deepcopy(base_report)
                        for window in merged["windows"]:
                            window["observed_at"] = observed
                    merged.update(extras)
                    if previous is None:
                        self._conn.execute(
                            "INSERT INTO observations (instance_id, report, evidence,"
                            " collector_seen_at, collector_host, collector_version)"
                            " VALUES (?, ?, ?, ?, ?, ?)",
                            (instance_id, json.dumps(merged), "{}", observed,
                             collector_host, collector_version))
                    else:
                        self._conn.execute(
                            "UPDATE observations SET report = ?, collector_seen_at = ?,"
                            " collector_host = ?, collector_version = ? WHERE instance_id = ?",
                            (json.dumps(merged), observed, collector_host,
                             collector_version, instance_id))
                    self._conn.commit()
                    return True
                if result is None:
                    # Heartbeat: the collector is alive; quota is untouched.
                    if previous is None:
                        self._conn.execute(
                            "INSERT INTO observations (instance_id, collector_seen_at,"
                            " collector_host, collector_version) VALUES (?, ?, ?, ?)",
                            (instance_id, observed, collector_host, collector_version))
                    else:
                        self._conn.execute(
                            "UPDATE observations SET collector_seen_at = ?, collector_host = ?,"
                            " collector_version = ? WHERE instance_id = ?",
                            (observed, collector_host, collector_version, instance_id))
                    self._conn.commit()
                    return True
                old_evidence = json.loads(previous["evidence"]) if previous and previous["evidence"] else {}
                old_windows = ({window["id"]: window
                                for window in json.loads(previous["report"])["windows"]}
                               if previous and previous["report"] else {})
                for window in result["windows"]:
                    key = window["id"]
                    if key in old_windows and old_evidence.get(key) == (evidence or {}).get(key):
                        window["observed_at"] = old_windows[key]["observed_at"]
                    else:
                        window["observed_at"] = observed
                if previous and previous["report"]:
                    kept = json.loads(previous["report"])
                    for key in self.PUSHED_KEYS:
                        if key in kept and key not in result:
                            result[key] = kept[key]
                last_progress_at = previous["last_progress_at"] if previous else None
                if progress is not None and progress != (previous["progress"] if previous else None):
                    last_progress_at = observed
                self._conn.execute(
                    "INSERT INTO observations (instance_id, report, evidence, collector_seen_at,"
                    " collector_host, collector_version, last_progress_at, progress, last_activity_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)"
                    " ON CONFLICT(instance_id) DO UPDATE SET report=excluded.report,"
                    " evidence=excluded.evidence, collector_seen_at=excluded.collector_seen_at,"
                    " collector_host=excluded.collector_host,"
                    " collector_version=excluded.collector_version,"
                    " last_progress_at=excluded.last_progress_at, progress=excluded.progress",
                    (instance_id, json.dumps(result), json.dumps(evidence or {}),
                     observed, collector_host, collector_version, last_progress_at, progress))
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

    def create_ingest_token(self, instance_id):
        """Mint (or rotate) the push token of one source instance.

        Returns the plaintext exactly once and stores only its SHA-256 hash.
        Only enabled instances of push-capable source classes get a token.
        """
        token = secrets.token_urlsafe(32)
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                row = self._conn.execute(
                    "SELECT enabled, type FROM source_instances WHERE id = ?",
                    (instance_id,)).fetchone()
                if row is None:
                    raise KeyError(instance_id)
                if (not row["enabled"] or
                        not source_type(row["type"]).accepts_push):
                    raise ValueError("ingest tokens are only for enabled push-capable sources")
                self._conn.execute(
                    "INSERT INTO ingest_tokens (instance_id, token_hash, created_at)"
                    " VALUES (?, ?, ?) ON CONFLICT(instance_id) DO UPDATE SET"
                    " token_hash=excluded.token_hash, created_at=excluded.created_at",
                    (instance_id, _token_digest(token), _stored_time(now_utc())))
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
        return token

    def revoke_ingest_token(self, instance_id):
        """Remove one instance's token; a disabled account may still revoke."""
        with self._lock:
            try:
                row = self._conn.execute("SELECT type FROM source_instances WHERE id = ?",
                                         (instance_id,)).fetchone()
                if row is None:
                    raise KeyError(instance_id)
                if not source_type(row["type"]).accepts_push:
                    raise ValueError("ingest tokens belong to push-capable sources only")
                self._conn.execute("DELETE FROM ingest_tokens WHERE instance_id = ?",
                                   (instance_id,))
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def ingest_token_info(self, instance_id):
        """Token existence and creation time; never the hash."""
        with self._lock:
            row = self._conn.execute(
                "SELECT created_at FROM ingest_tokens WHERE instance_id = ?",
                (instance_id,)).fetchone()
        return {"exists": row is not None, "created_at": row["created_at"] if row else None}

    def instance_for_ingest_token(self, token):
        """The instance a presented bearer token belongs to, or None."""
        digest = _token_digest(token)
        if digest is None:
            return None
        with self._lock:
            row = self._conn.execute(
                "SELECT instance_id FROM ingest_tokens WHERE token_hash = ?",
                (digest,)).fetchone()
        return row["instance_id"] if row else None

    def source_instance_type(self, instance_id):
        with self._lock:
            row = self._conn.execute("SELECT type FROM source_instances WHERE id = ?",
                                     (instance_id,)).fetchone()
        return row["type"] if row else None

    def schema_version(self):
        """The schema version of the open database."""
        with self._lock:
            row = self._conn.execute("PRAGMA user_version").fetchone()
        return int(row[0]) if row else 0

    def ingest_enabled_setting(self):
        """The persisted admin switch, or None while it has never been set."""
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM configuration_meta WHERE name = 'ingest_enabled'").fetchone()
        return None if row is None else bytes(row["value"]) == b"1"

    def save_ingest_enabled(self, enabled):
        if type(enabled) is not bool:
            raise ValueError("ingest_enabled must be a boolean")
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO configuration_meta (name, value) VALUES ('ingest_enabled', ?)"
                " ON CONFLICT(name) DO UPDATE SET value=excluded.value",
                (b"1" if enabled else b"0",))

    def demo_enabled_setting(self):
        """The persisted demo switch, or None while it has never been set."""
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM configuration_meta WHERE name = 'demo_enabled'").fetchone()
        return None if row is None else bytes(row["value"]) == b"1"

    def save_demo_enabled(self, enabled):
        if type(enabled) is not bool:
            raise ValueError("demo_enabled must be a boolean")
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO configuration_meta (name, value) VALUES ('demo_enabled', ?)"
                " ON CONFLICT(name) DO UPDATE SET value=excluded.value",
                (b"1" if enabled else b"0",))

    def overflow_rows_setting(self):
        """The persisted strip-line maximum, or None while it was never set."""
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM configuration_meta WHERE name = 'overflow_rows'").fetchone()
        if row is None:
            return None
        try:
            return int(bytes(row["value"]).decode("ascii"))
        except (ValueError, UnicodeDecodeError):
            return None

    def save_overflow_rows(self, value):
        if type(value) is not int or value < 0 or value > 3:
            raise ValueError("overflow_rows must be an integer in range 0..3")
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO configuration_meta (name, value) VALUES ('overflow_rows', ?)"
                " ON CONFLICT(name) DO UPDATE SET value=excluded.value",
                (str(value).encode("ascii"),))

    def alerts_settings(self):
        """The admin-stored alert settings; only the ones that were set.

        Each value is stored under its own name, so one setting can come
        from the database while another still comes from dash.toml.
        """
        values = {}
        with self._lock:
            rows = self._conn.execute(
                "SELECT name, value FROM configuration_meta WHERE name LIKE 'alerts.%'"
            ).fetchall()
        for row in rows:
            key = row["name"][len("alerts."):]
            if key not in ALERT_SETTING_KEYS:
                continue
            try:
                text = bytes(row["value"]).decode("ascii")
            except UnicodeDecodeError:
                continue
            if key in ("warning_percent", "critical_percent"):
                try:
                    values[key] = int(text)
                except ValueError:
                    continue
            elif key == "mute_hours":
                try:
                    hours = int(text)
                except ValueError:
                    continue
                if hours < 0 or hours > 48:
                    continue
                values[key] = hours
            elif key in ("enabled", "sound", "quiet_hours", "pop_to_top"):
                if text not in ("1", "0"):
                    continue
                values[key] = text == "1"
            else:
                values[key] = text
        return values

    def save_alerts(self, values):
        """Store the admin's alert settings, and clear the ones left unset.

        A key that is not in ``values`` is cleared, so a setting the user
        put back to the file's value stops overriding the file.
        """
        if not isinstance(values, dict):
            raise ValueError("alert settings must be an object")
        for key in values:
            if key not in ALERT_SETTING_KEYS:
                raise ValueError("unknown alert setting: %s" % key)
        with self._lock, self._conn:
            for key in ALERT_SETTING_KEYS:
                name = "alerts." + key
                if key not in values:
                    self._conn.execute(
                        "DELETE FROM configuration_meta WHERE name = ?", (name,))
                    continue
                value = values[key]
                if isinstance(value, bool):
                    data = b"1" if value else b"0"
                elif isinstance(value, int):
                    data = str(value).encode("ascii")
                else:
                    data = str(value).encode("ascii")
                self._conn.execute(
                    "INSERT INTO configuration_meta (name, value) VALUES (?, ?)"
                    " ON CONFLICT(name) DO UPDATE SET value=excluded.value",
                    (name, data))

    # -- per-service alert settings ------------------------------------------

    @staticmethod
    def _validated_service_alerts(values):
        """The stored form of one service's alert settings."""
        if not isinstance(values, dict):
            raise ValueError("service alert settings must be an object")
        unknown = set(values) - {"enabled", "warning_percent", "critical_percent"}
        if unknown:
            raise ValueError("unknown service alert setting: %s" %
                             sorted(unknown)[0])
        result = {}
        if "enabled" in values:
            if type(values["enabled"]) is not bool:
                raise ValueError("enabled must be a boolean")
            result["enabled"] = values["enabled"]
        for key in ("warning_percent", "critical_percent"):
            if key not in values:
                continue
            value = values[key]
            if value is None:
                # None means "inherit the global value".
                result[key] = None
                continue
            if type(value) is not int or value < 1 or value > 100:
                raise ValueError("%s must be an integer in range 1..100" % key)
            result[key] = value
        return result

    def service_alert_settings(self, instance_id):
        """One service's stored alert settings, or None when it has none.

        The stored form keeps only what the user set: a threshold that is
        absent is inherited from the global setting, and an absent
        ``enabled`` means the service is on.
        """
        try:
            name = _service_alert_name(instance_id)
        except ValueError:
            return None
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM configuration_meta WHERE name = ?", (name,)).fetchone()
        if row is None:
            return None
        try:
            stored = json.loads(bytes(row["value"]).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None
        if not isinstance(stored, dict):
            return None
        try:
            return self._validated_service_alerts(stored)
        except ValueError:
            return None

    def service_alert_settings_all(self):
        """Every service's stored alert settings, by instance id."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT name, value FROM configuration_meta WHERE name LIKE ?",
                (SERVICE_ALERT_PREFIX + "%",)).fetchall()
        result = {}
        for row in rows:
            instance_id = row["name"][len(SERVICE_ALERT_PREFIX):]
            try:
                stored = json.loads(bytes(row["value"]).decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                continue
            if not isinstance(stored, dict):
                continue
            try:
                result[instance_id] = self._validated_service_alerts(stored)
            except ValueError:
                continue
        return result

    def save_service_alert_settings(self, instance_id, values):
        """Store one service's alert settings.

        A setting that is not in ``values`` keeps its stored value, so a
        write that only switches the service on or off never touches its
        thresholds.
        """
        name = _service_alert_name(instance_id)
        stored = self._validated_service_alerts(values)
        with self._lock, self._conn:
            current = self.service_alert_settings(instance_id) or {}
            current.update(stored)
            self._conn.execute(
                "INSERT INTO configuration_meta (name, value) VALUES (?, ?)"
                " ON CONFLICT(name) DO UPDATE SET value=excluded.value",
                (name, json.dumps(current, sort_keys=True).encode("utf-8")))
        return current

    def delete_service_alert_settings(self, instance_id):
        """Forget one service's alert settings; used when it is deleted."""
        try:
            name = _service_alert_name(instance_id)
        except ValueError:
            return
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM configuration_meta WHERE name = ?", (name,))


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

    def create_admin_session(self, token_hash, csrf_token, expires_at, now, binding="", limit=None):
        """Store only a digest of the browser's bearer credential.

        ``binding`` says which admin token made the session. A session whose
        binding is not the current one is dead, and is removed here and on every
        read. At most ``limit`` sessions are kept, the newest.
        """
        limit = self.ADMIN_SESSION_LIMIT if limit is None else limit
        with self._lock, self._conn:
            self._conn.execute(
                "DELETE FROM admin_sessions WHERE expires_at <= ? OR binding <> ?", (now, binding))
            self._conn.execute(
                "INSERT INTO admin_sessions (token_hash, csrf_token, expires_at, binding)"
                " VALUES (?, ?, ?, ?)", (token_hash, csrf_token, expires_at, binding))
            self._conn.execute(
                "DELETE FROM admin_sessions WHERE token_hash IN (SELECT token_hash FROM"
                " admin_sessions ORDER BY expires_at DESC, rowid DESC LIMIT -1 OFFSET ?)", (limit,))

    def admin_session(self, token_hash, now, binding=""):
        with self._lock, self._conn:
            self._conn.execute(
                "DELETE FROM admin_sessions WHERE expires_at <= ? OR binding <> ?", (now, binding))
            row = self._conn.execute(
                "SELECT csrf_token, expires_at FROM admin_sessions WHERE token_hash = ?",
                (token_hash,)).fetchone()
            return dict(row) if row else None

    def revoke_admin_session(self, token_hash):
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM admin_sessions WHERE token_hash = ?", (token_hash,))

    def revoke_all_admin_sessions(self):
        """End every browser session. Returns how many there were."""
        with self._lock, self._conn:
            return self._conn.execute("DELETE FROM admin_sessions").rowcount

    def admin_session_count(self, now, binding=""):
        with self._lock, self._conn:
            self._conn.execute(
                "DELETE FROM admin_sessions WHERE expires_at <= ? OR binding <> ?", (now, binding))
            return self._conn.execute("SELECT COUNT(*) FROM admin_sessions").fetchone()[0]

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
                # The service is gone: its alert settings go with it, so a
                # row never outlives the service it belongs to.
                self._conn.execute("DELETE FROM configuration_meta WHERE name = ?",
                                   (SERVICE_ALERT_PREFIX + instance_id,))
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

    def latest_updated_at(self, source_id):
        """The ``updated_at`` of a source's latest snapshot, or None when it has none.

        A source whose readings can be older than its own earlier polls (the
        Claude usage cache) keeps its next snapshot from going backwards with it.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT updated_at FROM snapshots WHERE source_id = ? "
                "ORDER BY updated_at DESC, id DESC LIMIT 1", (source_id,)).fetchone()
        return _read_time(row["updated_at"]) if row else None

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

    # -- device lifecycle --------------------------------------------------
    #
    # These rows hold only what the lifecycle protocol needs. Diagnostics,
    # addresses, provider data, the shared token and private configuration
    # never reach them: the in-memory client registry stays the place for
    # ordinary "who polled" information.

    @staticmethod
    def _write_row(table, key_columns, record):
        """Insert or update one row from a dict, inside the caller's lock.

        A plain ``INSERT OR REPLACE`` would delete the old row first, and the
        delete would cascade into budgets and attempts. So an existing row is
        updated in place, leaving its children alone.
        """
        names = list(record)
        updates = ", ".join("%s = excluded.%s" % (name, name) for name in names
                            if name not in key_columns)
        sql = "INSERT INTO %s (%s) VALUES (%s)" % (
            table, ", ".join(names), ", ".join("?" for _ in names))
        if updates:
            sql += " ON CONFLICT(%s) DO UPDATE SET %s" % (", ".join(key_columns), updates)
        else:  # pragma: no cover - a record of only key columns changes nothing
            sql += " ON CONFLICT(%s) DO NOTHING" % ", ".join(key_columns)
        return sql

    def device(self, device_id):
        """One durable device row, or None."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM devices WHERE device_id = ?", (device_id,)).fetchone()
        return dict(row) if row is not None else None

    def devices(self):
        """Every durable device, by ID: the order the admin page shows."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM devices ORDER BY device_id").fetchall()
        return [dict(row) for row in rows]

    def device_count(self):
        with self._lock:
            return self._conn.execute("SELECT COUNT(*) FROM devices").fetchone()[0]

    def save_device(self, record):
        """Write one device row, and nothing else, in one transaction."""
        with self._lock:
            try:
                self._conn.execute(
                    self._write_row("devices", ("device_id",), record),
                    [record[name] for name in record])
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def save_device_report(self, device_record, budget=None, attempt=None,
                           admin_request=None, expected_revision=None):
        """Write a device and its budget/attempt rows in one transaction.

        ``budget`` may be one row or a list. A list is written in order, which
        matters when an exhausted budget is superseded and its replacement is
        created in the same report: the unique active-budget index is checked
        per statement.
        """
        budgets = budget if isinstance(budget, list) else [budget]
        attempts = attempt if isinstance(attempt, list) else [attempt]
        with self._lock:
            try:
                if expected_revision is not None:
                    self._conn.execute("BEGIN IMMEDIATE")
                    current = self._conn.execute(
                        "SELECT state_revision FROM devices WHERE device_id = ?",
                        (device_record["device_id"],)).fetchone()
                    if current is None or current[0] != expected_revision:
                        raise ConfigurationConflict("device state changed")
                self._conn.execute(
                    self._write_row("devices", ("device_id",), device_record),
                    [device_record[name] for name in device_record])
                for row in budgets:
                    if row is None:
                        continue
                    self._conn.execute(
                        self._write_row("device_update_budgets", ("generation_id",), row),
                        [row[name] for name in row])
                for row in attempts:
                    if row is None:
                        continue
                    self._conn.execute(
                        self._write_row("device_update_attempts", ("attempt_id",), row),
                        [row[name] for name in row])
                if admin_request is not None:
                    names = ("device_id", "request_key", "body_sha256", "response_json", "created_at")
                    self._conn.execute(
                        "INSERT INTO device_admin_requests (" + ",".join(names) +
                        ") VALUES (?,?,?,?,?)", tuple(admin_request[name] for name in names))
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def active_budget(self, device_id):
        """The one live automatic budget for a device, or None."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM device_update_budgets WHERE device_id = ? AND status = 'active'",
                (device_id,)).fetchone()
        return dict(row) if row is not None else None

    def device_budgets(self, device_id):
        """Every retained budget for a device, newest first."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM device_update_budgets WHERE device_id = ? "
                "ORDER BY created_at DESC, rowid DESC", (device_id,)).fetchall()
        return [dict(row) for row in rows]

    def device_attempts(self, device_id, limit=32):
        """The newest attempts for a device, newest first."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT a.* FROM device_update_attempts a "
                "JOIN device_update_budgets b ON b.generation_id = a.generation_id "
                "WHERE b.device_id = ? ORDER BY a.issued_at DESC, a.rowid DESC LIMIT ?",
                (device_id, int(limit))).fetchall()
        return [dict(row) for row in rows]

    def device_attempt(self, attempt_id):
        """One attempt row, or None."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM device_update_attempts WHERE attempt_id = ?",
                (attempt_id,)).fetchone()
        return dict(row) if row is not None else None

    def save_device_budget(self, record):
        """Write one budget row in its own transaction."""
        with self._lock:
            try:
                self._conn.execute(
                    self._write_row("device_update_budgets", ("generation_id",), record),
                    [record[name] for name in record])
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def save_device_attempt(self, record):
        """Write one attempt row in its own transaction."""
        with self._lock:
            try:
                self._conn.execute(
                    self._write_row("device_update_attempts", ("attempt_id",), record),
                    [record[name] for name in record])
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def device_admin_request(self, device_id, request_key):
        """A saved admin mutation result, or None. Deduplicates a retried click."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM device_admin_requests WHERE device_id = ? AND request_key = ?",
                (device_id, request_key)).fetchone()
        return dict(row) if row is not None else None

    def save_device_admin_request(self, record):
        """Remember one admin mutation result; the first write wins."""
        with self._lock:
            try:
                self._conn.execute(
                    "INSERT OR IGNORE INTO device_admin_requests "
                    "(device_id, request_key, body_sha256, response_json, created_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (record["device_id"], record["request_key"], record["body_sha256"],
                     record["response_json"], record["created_at"]))
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def prune_device_admin_requests(self, cutoff):
        """Forget deduplication records older than a stored UTC timestamp."""
        with self._lock:
            try:
                removed = self._conn.execute(
                    "DELETE FROM device_admin_requests WHERE created_at < ?",
                    (_stored_time(cutoff),)).rowcount
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
        return removed

    def release_cache(self, channel):
        """The cached daemon-release result for one channel, or None."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM daemon_release_cache WHERE channel = ?", (channel,)).fetchone()
        return dict(row) if row is not None else None

    def save_release_cache(self, record):
        """Write the cached daemon-release result for its channel."""
        with self._lock:
            try:
                self._conn.execute(
                    self._write_row("daemon_release_cache", ("channel",), record),
                    [record[name] for name in record])
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def clear_release_checking(self):
        """A restart ends any check that was in flight when the process stopped.

        ``checking`` describes a running task; a row left in that state by a
        stopped process would claim a check that is not happening.
        """
        with self._lock:
            try:
                self._conn.execute(
                    "UPDATE daemon_release_cache SET state = 'unknown' WHERE state = 'checking'")
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise


class ConfigurationConflict(ValueError):
    """Optimistic edit conflict; the caller should reload the source record."""
