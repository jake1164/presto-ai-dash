"""Configurable source daemon with SQLite history and an authenticated API."""

import argparse
import logging
import os
import signal
import sqlite3
import sys
import threading
import time
from datetime import timedelta

from . import __version__, private_files
from . import demo as demo_data
from .api import ApiServer
from .admin import AdminServer
from .clients import ClientRegistry
from .config import (ConfigError, admin_bind_error, configure_logging,
                     demo_enabled, ingest_enabled, load_config, resolve_shared_token,
                     resolve_admin_token)
from .device_install import app_snapshot
from .device_app import DeviceApp
from .device_lifecycle import DeviceLifecycle
from .device_setup import SetupService
from .management import SourceConfiguration, ManagedCollector
from .models import now_utc
from .preview_host import start_preview
from .problems import ProblemBuffer
from .release import release_version
from .sources import build_sources, in_container
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
    def __init__(self, config, config_path=None):
        self.config = config
        # The bootstrap file, so the admin page can say where a value came
        # from, and the start time, so it can show the uptime.
        self.config_path = config_path
        self.started_at = time.time()
        self.shared_token = resolve_shared_token(config)
        self.storage = Storage(config["daemon"]["database"])
        self.stop_event = threading.Event()
        self.collectors = []
        self.api = None
        self.admin = None
        self.preview = None
        self.presto_setup = None
        self.source_configuration = None
        self.maintenance_thread = None
        self.clients = None
        self.lifecycle = None
        self.problems = None
        self._stop_lock = threading.Lock()
        self._stopped = False

    def ingest_allowed(self):
        """The route's live switch, so an admin toggle applies without a restart."""
        return ingest_enabled(self.config, self.storage.ingest_enabled_setting())

    def demo_allowed(self):
        """Whether the API serves sample data right now, re-read per request."""
        return demo_enabled(self.config, self.storage.demo_enabled_setting())

    def demo_second(self):
        """Where the demo loop is now: seconds since start, modulo the loop."""
        return (time.time() - self.started_at) % demo_data.LOOP_SECONDS

    def _check_private_files(self):
        """Warn when other accounts could replace the database or its key.

        After the problems list is attached, so the warning shows in the admin
        page as well as in the log.
        """
        database = self.config["daemon"]["database"]
        paths = [database]
        if not (os.environ.get("DASH_ENCRYPTION_KEY")
                or os.environ.get("DASH_ENCRYPTION_KEY_FILE")):
            paths.append(self.config["admin"].get("key_file") or (str(database) + ".key"))
        private_files.check_private_files(paths)

    def _attach_problems(self):
        """Keep the recent warnings and errors, in memory only."""
        logging.getLogger().addHandler(self.problems)
        if self.source_configuration is not None:
            logging.getLogger().addHandler(self.source_configuration.logs)

    def setup(self):
        try:
            # A non-loopback admin address needs an explicit token and the list
            # of names the page is reached by. Without them the admin page
            # does not start, and the daemon says why instead of listening.
            if self.config["admin"]["enabled"]:
                problem = admin_bind_error(self.config)
                if problem:
                    raise ConfigError(problem)
            managed = (self.config["admin"]["enabled"] or
                       self.storage.configuration_marker() is not None or
                       bool(self.storage.source_instances()))
            if managed:
                self.source_configuration = SourceConfiguration(
                    self.storage, self.config, config_path=self.config_path,
                    started_at=self.started_at)
                self.collectors = [ManagedCollector(self.source_configuration,
                                                    self.config, self.stop_event)]
                self.clients = self.source_configuration.clients
                self.problems = self.source_configuration.problems
            else:
                self.collectors = [Collector(source, self.storage, self.config, self.stop_event)
                                   for source in build_sources(self.config)]
                self.clients = ClientRegistry()
                self.problems = ProblemBuffer()
            # Device identity and update state are durable, so they work the
            # same whether or not the admin page is running. Operational OTA
            # stays off: this stage reports a mismatch and asks for USB.
            self.lifecycle = (self.source_configuration.lifecycle
                              if self.source_configuration is not None
                              else DeviceLifecycle(self.storage))
            # The exact slot bundle this running daemon may serve to a device.
            # It is the same bundle the USB tool installs, so there is still
            # only one inventory of what this release ships.
            self.device_app = (self.source_configuration.device_app
                               if self.source_configuration is not None
                               else DeviceApp())
            self._attach_problems()
            self._check_private_files()
            self.api = ApiServer(self.config["daemon"]["host"], self.config["daemon"]["port"],
                                 self.storage, self.shared_token,
                                 source_ids=([collector.source.id for collector in self.collectors]
                                             if not managed else None),
                                 status_provider=(self.source_configuration.status_payload
                                                  if managed else None),
                                 ingest_enabled=self.ingest_allowed,
                                 client_registry=self.clients,
                                 # The device's only write: one service's
                                 # alerts on or off. It is a setting of this
                                 # daemon, not a provider action.
                                 alert_writer=(self.source_configuration.set_service_alert_enabled
                                               if managed else None),
                                 # The lazy card detail: one card's tiles and
                                 # lines, fetched when the card is opened.
                                 card_provider=(self.source_configuration.card_detail
                                                if managed else None),
                                 # Demo mode: sample data instead of the real
                                 # accounts, for the device and the API.
                                 demo_enabled=self.demo_allowed,
                                 demo_second=self.demo_second,
                                 # The demo follows the alert settings the
                                 # person chose, sound included.
                                 demo_device_alerts=(self.source_configuration.device_alerts
                                                     if managed else None),
                                 # Durable device identity, revision comparison
                                 # and the release notice, per request.
                                 device_lifecycle=self.lifecycle,
                                 # The narrow, authenticated download routes a
                                 # device uses for an update it was permitted.
                                 device_app=self.device_app)
            if self.config["admin"]["enabled"]:
                token = resolve_admin_token(self.config, self.shared_token)
                # The setup downloads carry this running build's public app
                # files, its shared device token and its release identity. The
                # admin token is never part of them.
                self.presto_setup = SetupService(
                    bundle_provider=app_snapshot,
                    shared_token=self.shared_token,
                    host=self.config["daemon"]["host"],
                    port=self.config["daemon"]["port"],
                    release=release_version(),
                    token_temporary=bool(self.config["daemon"].get("shared_token_generated")),
                    container=in_container())
                self.admin = AdminServer(self.config["admin"]["port"],
                                         self.source_configuration, token,
                                         host=self.config["admin"]["host"],
                                         allowed_hosts=self.config["admin"]["allowed_hosts"],
                                         token_generated=bool(self.config["admin"].get("token_generated")),
                                         secure_cookie=bool(self.config["admin"].get("secure_cookie")),
                                         setup=self.presto_setup)
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
                log.debug("retention cleanup removed %d snapshots and %d alert observations",
                          counts["snapshots"], counts["alerts"])
            except Exception:
                log.exception("periodic retention cleanup failed")
            try:
                # An admin deduplication record is kept for a day; a live one
                # is never dropped, so a double click still returns its result.
                self.storage.prune_device_admin_requests(
                    now_utc() - timedelta(hours=24))
            except Exception:
                log.exception("device request cleanup failed")

    def start(self):
        try:
            # A release check that was in flight when the last process stopped
            # is not happening now: say so instead of claiming it still is.
            self.storage.clear_release_checking()
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
            # A test aid, off unless DASH_PRESTO_PREVIEW is on. It polls this
            # daemon's device port, so it starts after the port is open.
            self.preview = start_preview(self.config["daemon"]["port"], self.shared_token)
            return self
        except Exception:
            self.stop()
            raise

    def announce_access(self):
        """Log the page address and any generated tokens as the last startup lines."""
        if self.admin is not None:
            log.info("configuration page: http://%s:%d", self.admin.host,
                     self.admin.actual_port)
            if self.config["admin"].get("token_generated"):
                log.info("temporary admin token (new at every start; set admin.token in "
                         "dash.toml to keep one): %s", self.config["admin"]["token"])
        if self.config["daemon"].get("shared_token_generated"):
            log.warning("no shared token configured; temporary token: %s", self.shared_token)

    def stop(self):
        with self._stop_lock:
            if self._stopped:
                return
            self._stopped = True
            self.stop_event.set()
            if self.problems is not None:
                try:
                    logging.getLogger().removeHandler(self.problems)
                    if self.source_configuration is not None:
                        logging.getLogger().removeHandler(self.source_configuration.logs)
                except Exception:  # pragma: no cover - defensive
                    pass
            try:
                try:
                    if self.preview is not None:
                        self.preview.stop()
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
    arguments = sys.argv[1:] if argv is None else list(argv)
    if arguments and arguments[0] in ("install-presto", "check-presto"):
        # USB commands for the device; they never start the daemon.
        from . import device_install
        return device_install.main(arguments[0], arguments[1:])
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
        daemon = Daemon(config, config_path=args.config)
        daemon.setup()
        for signum in (signal.SIGINT, signal.SIGTERM):
            signal.signal(signum, lambda *_: daemon.stop_event.set())
        daemon.start()
        log.info("presto-ai-dash %s ready", __version__)
        daemon.announce_access()
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
