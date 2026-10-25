"""M3 history, migration, retention, and lifecycle contracts."""

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, build_opener, ProxyHandler

SRC = Path(__file__).resolve().parents[2] / "src"
sys.path.insert(0, str(SRC))

from presto_ai_dash.api import ApiServer
from presto_ai_dash.app import Daemon
from presto_ai_dash.config import ConfigError, load_config
from presto_ai_dash.models import SourceSnapshot
from presto_ai_dash.storage import Storage


UTC = timezone.utc


class StorageM3Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "history.db"
        self.storage = Storage(self.path)
        self.addCleanup(lambda: self.storage.close())

    def snapshot(self, status="ok", moment=None, alerts=None, source="svc"):
        return SourceSnapshot(source, status, moment or datetime(2026, 1, 1, tzinfo=UTC),
                              metrics={"count": 1}, alerts=alerts or [])

    def test_utc_normalization_orders_offsets_and_queries_boundaries(self):
        earlier = self.snapshot("warning", datetime.fromisoformat("2026-01-01T12:00:00+02:00"))
        later = self.snapshot("active", datetime.fromisoformat("2026-01-01T11:00:00+00:00"))
        self.storage.save_snapshot(earlier)
        self.storage.save_snapshot(later)
        latest = self.storage.latest_snapshots()["svc"]
        self.assertEqual(latest.status, "active")
        self.assertEqual(latest.updated_at, datetime(2026, 1, 1, 11, tzinfo=UTC))
        rows = self.storage.snapshot_history("svc", datetime(2026, 1, 1, 10, tzinfo=UTC),
                                             datetime(2026, 1, 1, 11, tzinfo=UTC))
        self.assertEqual([row.status for row in rows], ["warning"])
        self.assertEqual(self.storage.snapshot_history("missing", datetime(2026, 1, 1, tzinfo=UTC)), [])

    def test_latest_updated_at_is_the_newest_time_a_source_has_saved(self):
        self.assertIsNone(self.storage.latest_updated_at("svc"))
        self.storage.save_snapshot(self.snapshot("ok", datetime(2026, 1, 1, 12, tzinfo=UTC)))
        self.storage.save_snapshot(self.snapshot("ok", datetime(2026, 1, 1, 11, tzinfo=UTC)))
        self.assertEqual(self.storage.latest_updated_at("svc"), datetime(2026, 1, 1, 12, tzinfo=UTC))
        self.assertIsNone(self.storage.latest_updated_at("other"))

    def test_history_source_isolation_newest_limit_and_ties(self):
        moment = datetime(2026, 1, 1, tzinfo=UTC)
        for status in ("ok", "active", "warning"):
            self.storage.save_snapshot(self.snapshot(status, moment))
        self.storage.save_snapshot(self.snapshot("critical", moment, source="other"))
        rows = self.storage.snapshot_history("svc", moment, limit=2)
        self.assertEqual([row.status for row in rows], ["active", "warning"])
        for bad in (0, 1001, True, 1.0, "2"):
            with self.subTest(limit=bad), self.assertRaises(ValueError):
                self.storage.snapshot_history("svc", moment, limit=bad)
        with self.assertRaises(ValueError):
            self.storage.snapshot_history("svc", moment, moment)

    def test_alert_observations_dedupe_per_snapshot_and_reopen(self):
        moment = datetime(2026, 1, 1, tzinfo=UTC)
        items = [{"severity": "warning", "message": "Elevated"},
                 {"severity": "warning", "message": "Elevated"}]
        snapshot_id = self.storage.save_snapshot(self.snapshot("warning", moment, items))
        self.storage.save_snapshot(self.snapshot("warning", moment + timedelta(minutes=1), items))
        observations = self.storage.alert_history("svc", moment, limit=10)
        self.assertEqual(len(observations), 2)
        self.assertEqual(observations[0]["snapshot_id"], snapshot_id)
        self.assertEqual(observations[0]["observed_at"], moment)
        self.storage.close()
        self.storage = Storage(self.path)
        self.assertEqual(len(self.storage.alert_history("svc", moment)), 2)

    def test_invalid_alert_and_database_failure_roll_back_snapshot(self):
        invalid = self.snapshot("warning", alerts=[{"severity": "bogus", "message": "x"}])
        with self.assertRaises(ValueError):
            self.storage.save_snapshot(invalid)
        self.storage._conn.execute("""CREATE TRIGGER fail_alert BEFORE INSERT ON alerts
            BEGIN SELECT RAISE(ABORT, 'forced'); END""")
        with self.assertRaises(sqlite3.IntegrityError):
            self.storage.save_snapshot(self.snapshot("warning", alerts=[
                {"severity": "warning", "message": "x"}]))
        self.assertEqual(self.storage.snapshot_history("svc", datetime(2020, 1, 1, tzinfo=UTC)), [])

    def test_history_reads_writes_and_pruning_can_overlap(self):
        start = datetime(2026, 1, 1, tzinfo=UTC)
        gate = threading.Barrier(4)
        errors = []

        def writer():
            try:
                gate.wait()
                for index in range(30):
                    self.storage.save_snapshot(self.snapshot(
                        "warning", start + timedelta(seconds=index), [
                            {"severity": "warning", "message": "load"}]))
            except Exception as exc:
                errors.append(exc)

        def reader():
            try:
                gate.wait()
                for _ in range(30):
                    self.storage.latest_snapshots()
                    self.storage.snapshot_history("svc", start)
                    self.storage.alert_history("svc", start)
            except Exception as exc:
                errors.append(exc)

        def cleaner():
            try:
                gate.wait()
                for _ in range(30):
                    self.storage.prune(start + timedelta(days=1), 72)
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=writer), threading.Thread(target=reader),
                   threading.Thread(target=cleaner)]
        for thread in threads:
            thread.start()
        gate.wait()
        for thread in threads:
            thread.join(5)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])

    def test_migration_normalizes_legacy_rows_and_reopen_is_idempotent(self):
        self.storage.close()
        connection = sqlite3.connect(self.path)
        connection.execute("DROP TABLE alerts")
        connection.execute("DROP TABLE snapshots")
        connection.execute("""CREATE TABLE snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT, source_id TEXT NOT NULL,
            status TEXT NOT NULL, updated_at TEXT NOT NULL, metrics TEXT NOT NULL,
            alerts TEXT NOT NULL, error TEXT)""")
        connection.execute("INSERT INTO snapshots VALUES (7, 'legacy', 'warning', ?, '{}', ?, NULL)",
                           ("2026-01-01T12:00:00+02:00", json.dumps([
                               {"severity": "warning", "message": "Elevated", "created_at": "old"}])))
        connection.execute("PRAGMA user_version = 0")
        connection.commit()
        connection.close()
        self.storage = Storage(self.path)
        row = self.storage._conn.execute("SELECT updated_at FROM snapshots WHERE id=7").fetchone()
        self.assertEqual(row[0], "2026-01-01T10:00:00.000000+00:00")
        restored = self.storage.latest_snapshots()["legacy"]
        self.assertEqual(restored.status, "warning")
        self.assertEqual(restored.metrics, {})
        self.assertEqual(self.storage._conn.execute("SELECT id FROM snapshots").fetchone()[0], 7)
        self.assertEqual(self.storage.alert_history("legacy", datetime(2026, 1, 1, tzinfo=UTC))[0]["observed_at"],
                         datetime(2026, 1, 1, 10, tzinfo=UTC))
        self.storage.close()
        self.storage = Storage(self.path)
        self.assertEqual(self.storage._conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0], 1)
        self.assertEqual(self.storage._conn.execute("PRAGMA user_version").fetchone()[0],
                         Storage.SCHEMA_VERSION)

    def test_a_saved_demo_source_is_removed_by_the_migration(self):
        """A database from before the demo source was removed keeps working."""
        self.storage.close()
        connection = sqlite3.connect(self.path)
        connection.execute(
            "INSERT INTO source_instances (id, type, name, enabled, config, credential,"
            " revision, position) VALUES ('fake', 'fake', 'Fake', 1, '{}', NULL, 1, 0)")
        connection.execute(
            "INSERT INTO snapshots (source_id, status, updated_at, metrics, alerts)"
            " VALUES ('fake', 'warning', '2026-01-01T12:00:00+00:00', '{}', '[]')")
        connection.execute("PRAGMA user_version = 8")
        connection.commit()
        connection.close()
        self.storage = Storage(self.path)
        self.assertEqual(self.storage.source_instances(), [])
        self.assertEqual(self.storage.latest_snapshots(), {})
        self.assertEqual(self.storage.schema_version(), Storage.SCHEMA_VERSION)
        self.storage.close()
        config = load_config()
        config["daemon"].update(host="127.0.0.1", port=0, database=str(self.path),
                                shared_token="device-token")
        daemon = Daemon(config).setup().start()
        try:
            request = Request("http://127.0.0.1:%d/api/v1/status" % daemon.api.actual_port,
                              headers={"Authorization": "Bearer device-token"})
            with build_opener(ProxyHandler({})).open(request, timeout=3) as response:
                payload = json.load(response)
            # The upgraded daemon serves the empty Home, not sample data.
            self.assertEqual([card["id"] for card in payload["cards"]], ["dash"])
            self.assertEqual(payload["sources"], [])
        finally:
            daemon.stop()

    def test_bad_legacy_data_rolls_back_and_newer_schema_is_rejected(self):
        self.storage.close()
        connection = sqlite3.connect(self.path)
        connection.execute("DROP TABLE alerts")
        connection.execute("DROP INDEX IF EXISTS idx_snapshots_source_time")
        connection.execute("DROP INDEX IF EXISTS idx_snapshots_time")
        connection.execute("INSERT INTO snapshots(source_id,status,updated_at,metrics,alerts) "
                           "VALUES('legacy','ok','2026-01-01T12:00:00+02:00','{}','[]')")
        connection.execute("INSERT INTO snapshots(source_id,status,updated_at,metrics,alerts) "
                           "VALUES('legacy','ok','not-a-date','{}','[]')")
        connection.execute("PRAGMA user_version = 0")
        connection.commit()
        connection.close()
        with self.assertRaises(ValueError):
            Storage(self.path)
        connection = sqlite3.connect(self.path)
        self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 0)
        self.assertEqual([row[0] for row in connection.execute(
            "SELECT updated_at FROM snapshots ORDER BY id")],
            ["2026-01-01T12:00:00+02:00", "not-a-date"])
        self.assertIsNone(connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='alerts'").fetchone())
        connection.execute("UPDATE snapshots SET updated_at='2026-01-01T13:00:00Z', alerts='null' "
                           "WHERE id=(SELECT MAX(id) FROM snapshots)")
        connection.commit()
        connection.close()
        with self.assertRaises(ValueError):
            Storage(self.path)
        connection = sqlite3.connect(self.path)
        self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 0)
        self.assertEqual([row[0] for row in connection.execute(
            "SELECT updated_at FROM snapshots ORDER BY id")],
            ["2026-01-01T12:00:00+02:00", "2026-01-01T13:00:00Z"])
        self.assertIsNone(connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='alerts'").fetchone())
        connection.execute("PRAGMA user_version=99")
        connection.commit()
        connection.close()
        with self.assertRaisesRegex(RuntimeError, "newer than supported"):
            Storage(self.path)

    def test_prune_cutoff_latest_expiration_and_alert_cascade(self):
        now = datetime(2026, 1, 10, tzinfo=UTC)
        cutoff = now - timedelta(hours=72)
        self.storage.save_snapshot(self.snapshot("warning", cutoff - timedelta(microseconds=1), [
            {"severity": "warning", "message": "old"}]))
        self.storage.save_snapshot(self.snapshot("warning", cutoff, [
            {"severity": "warning", "message": "boundary"}]))
        self.assertEqual(self.storage.prune(now, 72), {"snapshots": 1, "alerts": 1})
        self.assertEqual(len(self.storage.snapshot_history("svc", cutoff)), 1)
        self.assertEqual(len(self.storage.alert_history("svc", cutoff)), 1)
        self.assertEqual(self.storage.prune(now, 72), {"snapshots": 0, "alerts": 0})
        self.assertEqual(self.storage.prune(now + timedelta(hours=72, microseconds=1), 72),
                         {"snapshots": 1, "alerts": 1})
        self.assertEqual(self.storage.latest_snapshots(), {})
        for retention in (0, -1, True, 1.5):
            with self.subTest(retention=retention), self.assertRaises(ValueError):
                self.storage.prune(now, retention)


class ConfigM3Tests(unittest.TestCase):
    def test_retention_defaults_old_config_and_validation(self):
        config = load_config()
        self.assertEqual(config["daemon"]["retention_hours"], 72)
        self.assertEqual(config["daemon"]["cleanup_interval"], 300)
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "old.toml"
            path.write_text('[daemon]\nshared_token="token-0123456789abc"\n', encoding="utf-8")
            self.assertEqual(load_config(path)["daemon"]["retention_hours"], 72)
            for key, value in (("retention_hours", "true"), ("retention_hours", "0"),
                               ("cleanup_interval", "false"), ("cleanup_interval", "-1")):
                path.write_text("[daemon]\n%s=%s\n" % (key, value), encoding="utf-8")
                with self.subTest(key=key, value=value), self.assertRaises(ConfigError):
                    load_config(path)


class ApiShutdownM3Tests(unittest.TestCase):
    def test_stop_waits_for_active_storage_request(self):
        storage = Storage(":memory:")
        entered, release, stopped = threading.Event(), threading.Event(), threading.Event()
        original = storage.latest_snapshots

        def paused_read():
            entered.set()
            release.wait(5)
            return original()

        with patch.object(storage, "latest_snapshots", side_effect=paused_read):
            api = ApiServer("127.0.0.1", 0, storage, "token")
            api.start()
            request_thread = threading.Thread(target=lambda: self._request(api.actual_port))
            request_thread.start()
            self.assertTrue(entered.wait(2))
            stop_thread = threading.Thread(target=lambda: (api.stop(), stopped.set()))
            stop_thread.start()
            self.assertFalse(stopped.wait(0.1))
            release.set()
            self.assertTrue(stopped.wait(2))
            request_thread.join(2)
            stop_thread.join(2)
        storage.close()

    def test_partial_header_client_does_not_block_shutdown(self):
        storage = Storage(":memory:")
        api = ApiServer("127.0.0.1", 0, storage, "token")
        api.start()
        client = socket.create_connection(("127.0.0.1", api.actual_port), timeout=1)
        client.sendall(b"GET /api/v1/status HTTP/1.1\r\nHost: local\r\n")
        started = time.monotonic()
        api.stop()
        self.assertLess(time.monotonic() - started, 4.0)
        client.close()
        storage.close()

    @staticmethod
    def _request(port):
        opener = build_opener(ProxyHandler({}))
        request = Request("http://127.0.0.1:%d/api/v1/status" % port,
                          headers={"Authorization": "Bearer token"})
        try:
            opener.open(request, timeout=4).read()
        except Exception:
            pass


class DaemonM3Tests(unittest.TestCase):
    def test_startup_cleanup_failure_prevents_api_readiness_and_closes_storage(self):
        with tempfile.TemporaryDirectory() as temp:
            config = load_config()
            config["daemon"].update(host="127.0.0.1", port=0,
                                    database=str(Path(temp) / "startup-failure.db"),
                                    shared_token="token")
            daemon = Daemon(config).setup()
            with patch.object(daemon.storage, "prune", side_effect=RuntimeError("cleanup failed")):
                with self.assertRaisesRegex(RuntimeError, "cleanup failed"):
                    daemon.start()
            self.assertTrue(daemon.storage._closed)
            self.assertTrue(daemon.api._stopped)
            self.assertIsNone(daemon.api._thread)

    def test_periodic_cleanup_failure_is_logged_and_retried(self):
        with tempfile.TemporaryDirectory() as temp:
            config = load_config()
            config["daemon"].update(host="127.0.0.1", port=0,
                                    database=str(Path(temp) / "periodic.db"),
                                    shared_token="token", cleanup_interval=1)
            daemon = Daemon(config).setup()
            calls = 0
            recovered = threading.Event()

            def prune(now, retention):
                nonlocal calls
                calls += 1
                if calls == 1:
                    return {"snapshots": 0, "alerts": 0}  # startup sweep
                if calls == 2:
                    raise RuntimeError("temporary sweep failure")
                recovered.set()
                return {"snapshots": 0, "alerts": 0}

            with patch.object(daemon.storage, "prune", side_effect=prune):
                with self.assertLogs("presto_ai_dash.app", level="ERROR"):
                    daemon.config["daemon"]["cleanup_interval"] = 0.01
                    daemon.start()
                    try:
                        self.assertTrue(recovered.wait(2))
                        self.assertTrue(daemon.maintenance_thread.is_alive())
                    finally:
                        daemon.stop()

    def test_startup_cleanup_status_null_and_workers_stop(self):
        with tempfile.TemporaryDirectory() as temp:
            db = Path(temp) / "daemon.db"
            old = Storage(db)
            old.save_snapshot(SourceSnapshot("old", "ok", datetime.now(UTC) - timedelta(days=5)))
            old.close()
            config = load_config()
            config["daemon"].update(host="127.0.0.1", port=0, database=str(db),
                                    shared_token="token", poll_interval=100,
                                    retention_hours=72, cleanup_interval=100)
            daemon = Daemon(config).setup().start()
            try:
                self.assertEqual(daemon.storage.latest_snapshots(), {})
                self.assertEqual(daemon.storage._conn.execute(
                    "SELECT COUNT(*) FROM snapshots").fetchone()[0], 0)
                opener = build_opener(ProxyHandler({}))
                request = Request("http://127.0.0.1:%d/api/v1/status" % daemon.api.actual_port,
                                  headers={"Authorization": "Bearer token"})
                import json
                payload = json.loads(opener.open(request, timeout=2).read())
                self.assertIsNone(payload["updated_at"])
            finally:
                daemon.stop()
            self.assertFalse(daemon.maintenance_thread.is_alive())

    def test_separate_process_auth_clean_stop_and_database_restart(self):
        with tempfile.TemporaryDirectory() as temp:
            config_path = Path(temp) / "daemon.toml"
            database = Path(temp) / "daemon.db"
            reservation = socket.socket()
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
            reservation.close()
            config_path.write_text(
                '[daemon]\nhost="127.0.0.1"\nport=%d\nshared_token="smoke-token-0123456789"\n'
                'database=%s\npoll_interval=60\nretention_hours=72\ncleanup_interval=60\n'
                % (port, json.dumps(str(database))), encoding="utf-8")
            child = (
                "import sys; from presto_ai_dash.config import load_config; "
                "from presto_ai_dash.app import Daemon; "
                "d=Daemon(load_config(sys.argv[1])).setup().start(); "
                "print(d.api.actual_port, flush=True); "
                "sys.stdin.read(); d.stop()"
            )
            env = {**os.environ, "PYTHONPATH": str(SRC)}
            for _ in range(2):
                process = subprocess.Popen(
                    [sys.executable, "-c", child, str(config_path)],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    text=True, env=env)
                try:
                    port_line = process.stdout.readline().strip()
                    if not port_line.isdigit():
                        self.fail(process.stderr.read())
                    port = int(port_line)
                    opener = build_opener(ProxyHandler({}))
                    url = "http://127.0.0.1:%d/api/v1/status" % port
                    with self.assertRaises(HTTPError) as response:
                        opener.open(url, timeout=3)
                    self.assertEqual(response.exception.code, 401)
                    response.exception.close()
                    request = Request(url, headers={"Authorization": "Bearer smoke-token-0123456789"})
                    with opener.open(request, timeout=3) as response:
                        payload = json.loads(response.read())
                    self.assertEqual([card["id"] for card in payload["cards"]],
                                     ["dash"])
                    # The TOML enables no source, so nothing is invented.
                    self.assertEqual(payload["sources"], [])
                    self.assertIsNone(payload["updated_at"])
                finally:
                    if process.stdin:
                        process.stdin.close()
                    return_code = process.wait(timeout=5)
                    if process.stdout:
                        process.stdout.close()
                    if process.stderr:
                        process.stderr.close()
                    self.assertEqual(return_code, 0)


if __name__ == "__main__":
    unittest.main()
