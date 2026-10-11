"""Configurable source daemon with SQLite history and an authenticated API."""

import argparse
import logging
import signal
import sqlite3
import sys
import threading

from . import __version__
from .api import ApiServer
from .admin import AdminServer
from .config import (ConfigError, configure_logging, load_config,
                     resolve_shared_token, resolve_admin_token)
from .management import SourceConfiguration, ManagedCollector
from .models import now_utc
from .sources import build_sources
from .storage import Storage

log = logging.getLogger(__name__)


class Collector:
    def __init__(self, source, storage, config, stop_event):
        self.source = source
        self.source.attach_storage(storage)
        self.storage = storage
        self.interval = config["daemon"]["poll_interval"]
        self.stop_event = stop_event
        self.thread = None

    def collect_once(self):
        snapshot = self.source.collect_safely()
        self.storage.save_snapshot(snapshot)
        return snapshot

    def _run(self):
        while not self.stop_event.wait(self.interval):
            try:
                self.collect_once()
            except Exception:
                log.exception("collection failed for %s", self.source.id)

    def start(self):
        self.thread = threading.Thread(target=self._run, name="collect-" + self.source.id,
                                       daemon=True)
        self.thread.start()

    def join(self):
        if self.thread is not None:
            self.thread.join()


class Daemon:
    def __init__(self, config):
        self.config = config
        self.shared_token = resolve_shared_token(config)
        self.storage = Storage(config["daemon"]["database"])
        self.stop_event = threading.Event()
        self.collectors = []
        self.api = None
        self.admin = None
        self.source_configuration = None
        self.maintenance_thread = None
        self._stop_lock = threading.Lock()
        self._stopped = False

    def setup(self):
        try:
            managed = (self.config["admin"]["enabled"] or
                       self.storage.configuration_marker() is not None or
                       bool(self.storage.source_instances()))
            if managed:
                self.source_configuration = SourceConfiguration(self.storage, self.config)
                self.collectors = [ManagedCollector(self.source_configuration,
                                                    self.config, self.stop_event)]
            else:
                self.collectors = [Collector(source, self.storage, self.config, self.stop_event)
                                   for source in build_sources(self.config)]
            self.api = ApiServer(self.config["daemon"]["host"], self.config["daemon"]["port"],
                                 self.storage, self.shared_token,
                                 source_ids=([collector.source.id for collector in self.collectors]
                                             if not managed else None),
                                 status_provider=(self.source_configuration.status_payload
                                                  if managed else None))
            if self.config["admin"]["enabled"]:
                token = resolve_admin_token(self.config, self.shared_token)
                self.admin = AdminServer(self.config["admin"]["port"],
                                         self.source_configuration, token)
            return self
        except Exception:
            self.stop()
            raise

    def _maintenance(self):
        interval = self.config["daemon"]["cleanup_interval"]
        retention = self.config["daemon"]["retention_hours"]
        while not self.stop_event.wait(interval):
            try:
                counts = self.storage.prune(now_utc(), retention)
                log.info("retention cleanup removed %d snapshots and %d alert observations",
                         counts["snapshots"], counts["alerts"])
            except Exception:
                log.exception("periodic retention cleanup failed")

    def start(self):
        try:
            # Collect once, then expire old history before reporting readiness.
            for collector in self.collectors:
                collector.collect_once()
            counts = self.storage.prune(now_utc(), self.config["daemon"]["retention_hours"])
            log.info("startup retention cleanup removed %d snapshots and %d alert observations",
                     counts["snapshots"], counts["alerts"])
            for collector in self.collectors:
                collector.start()
            self.maintenance_thread = threading.Thread(
                target=self._maintenance, name="retention-cleanup", daemon=True)
            self.maintenance_thread.start()
            self.api.start()
            if self.admin is not None:
                self.admin.start()
                log.info("configuration page: http://127.0.0.1:%d", self.admin.actual_port)
            return self
        except Exception:
            self.stop()
            raise

    def stop(self):
        with self._stop_lock:
            if self._stopped:
                return
            self._stopped = True
            self.stop_event.set()
            try:
                try:
                    if self.admin is not None:
                        self.admin.stop()
                finally:
                    if self.api is not None:
                        self.api.stop()
            finally:
                for collector in self.collectors:
                    if collector.thread is not None and collector.thread.is_alive():
                        collector.join()
                if (self.maintenance_thread is not None and
                        self.maintenance_thread.is_alive()):
                    self.maintenance_thread.join()
                self.storage.close()

    def wait(self):
        # Bounded waits also let Windows deliver console signals promptly.
        while not self.stop_event.wait(0.5):
            pass


def main(argv=None):
    parser = argparse.ArgumentParser(prog="presto-ai-dash")
    parser.add_argument("--config", "-c", help="TOML config path (otherwise defaults)")
    parser.add_argument("--version", action="version", version="presto-ai-dash " + __version__)
    parser.add_argument("--check", action="store_true", help="validate config and exit")
    parser.add_argument("--admin", action="store_true", help="enable local configuration page")
    parser.add_argument("--generate-key", action="store_true",
                        help="print a new key for DASH_ENCRYPTION_KEY and exit")
    args = parser.parse_args(argv)
    if args.generate_key:
        from cryptography.fernet import Fernet
        print(Fernet.generate_key().decode("ascii"))
        return 0
    try:
        config = load_config(args.config)
        if args.admin:
            config["admin"]["enabled"] = True
    except ConfigError as exc:
        print("configuration error: %s" % exc, file=sys.stderr)
        return 2
    configure_logging(config)
    if args.check:
        enabled = [name for name, section in config["sources"].items()
                   if section.get("enabled", False)]
        print("configuration OK (sources: %s)" % (", ".join(enabled) or "none"))
        return 0

    daemon = None
    try:
        daemon = Daemon(config)
        daemon.setup()
        for signum in (signal.SIGINT, signal.SIGTERM):
            signal.signal(signum, lambda *_: daemon.stop_event.set())
        daemon.start()
        log.info("presto-ai-dash %s ready", __version__)
        daemon.wait()
    except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
        log.error("daemon startup failed: %s", exc)
        return 1
    finally:
        if daemon is not None:
            daemon.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
