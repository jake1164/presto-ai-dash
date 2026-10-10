"""M2 daemon: collect fake snapshots and serve the authenticated status API."""

import argparse
import logging
import signal
import sys
import threading

from . import __version__
from .api import ApiServer
from .config import ConfigError, configure_logging, load_config, resolve_shared_token
from .sources import build_sources
from .storage import Storage

log = logging.getLogger(__name__)


class Collector:
    def __init__(self, source, storage, config, stop_event):
        self.source = source
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

    def setup(self):
        self.collectors = [Collector(source, self.storage, self.config, self.stop_event)
                           for source in build_sources(self.config)]
        self.api = ApiServer(self.config["daemon"]["host"], self.config["daemon"]["port"],
                             self.storage, self.shared_token)
        return self

    def start(self):
        # Populate storage before accepting the first request.
        for collector in self.collectors:
            collector.collect_once()
            collector.start()
        self.api.start()
        return self

    def stop(self):
        self.stop_event.set()
        if self.api is not None:
            self.api.stop()
        for collector in self.collectors:
            collector.join()
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
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print("configuration error: %s" % exc, file=sys.stderr)
        return 2
    configure_logging(config)
    if args.check:
        print("configuration OK (M2: fake source only)")
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
    except (OSError, ValueError) as exc:
        log.error("daemon startup failed: %s", exc)
        return 1
    finally:
        if daemon is not None:
            daemon.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
