"""Background daemon-release checks and their durable cache.

``ReleaseChecker`` owns one background thread. It checks shortly after startup
and then about once a day, retries a failure with a growing delay, honors a
registry's ``Retry-After``, and lets the admin page ask for a check now (at
most one new start a minute, and a check already running is joined rather than
doubled). It writes only the existing ``daemon_release_cache`` row for the
running channel; readers (the status route, the device poll, the admin page)
read that row and never reach a registry.

``last_attempt_at`` moves whenever a check starts. ``checked_at`` moves only
when a check read the registry to the end, so a failure never makes the
installation look freshly verified, and a failed check keeps the last good
``latest`` and ``checked_at`` to show honestly as old.

The checker knows nothing about Presto, the admin page or the status payload.
Registry adapters and the version policy are injected.
"""

import logging
import math
import random
import threading
import time
from datetime import datetime, timezone

from . import release_policy
from .release_registry import Cancelled, GhcrAdapter, PypiAdapter, RegistryError

log = logging.getLogger(__name__)

CHECK_INTERVAL_S = 86400
STARTUP_DELAY_S = 15
# Restarts do not re-ask a registry sooner than this after the last attempt.
RESTART_SPACING_S = 600
COOLDOWN_S = 60
# After a failed check: wait this long, then longer, never longer than a day.
BACKOFF_S = (300, 900, 3600, 14400)
JITTER = 0.1
# When a registry rate-limits without saying for how long.
RATE_LIMIT_DEFAULT_S = 3600

PROJECT = "presto-ai-dash"
IMAGE = "jake1164/presto-ai-dash"
ENV_SWITCH = "DASH_RELEASE_CHECK"


def stamp(moment):
    """A UTC RFC 3339 timestamp at second precision, ending in Z."""
    return datetime.fromtimestamp(float(moment), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_stamp(text):
    """Seconds since the epoch for a stored timestamp, or None."""
    try:
        return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc).timestamp()
    except (TypeError, ValueError):
        return None


def checks_switched_off(environ):
    """Whether the owner turned release checks off (``DASH_RELEASE_CHECK=off``)."""
    return (environ.get(ENV_SWITCH) or "").strip().lower() in ("0", "off", "false", "no")


def adapter_for(installation, transport=None, cancelled=None):
    """The one adapter for this installation's own registry, or None.

    There is no second choice: a PyPI install is never checked against GHCR, a
    container never against PyPI, and nothing is checked against TestPyPI.
    """
    if installation.channel == "pypi":
        return PypiAdapter(PROJECT, transport=transport, cancelled=cancelled)
    if installation.channel == "ghcr":
        return GhcrAdapter(IMAGE, transport=transport, cancelled=cancelled)
    return None


class ReleaseChecker(object):
    def __init__(self, storage, installation, adapter_factory=None, enabled=True,
                 clock=time.time, monotonic=time.monotonic, interval=CHECK_INTERVAL_S,
                 startup_delay=STARTUP_DELAY_S, cooldown=COOLDOWN_S, backoff=BACKOFF_S,
                 jitter=JITTER, rng=None):
        self.storage = storage
        self.installation = installation
        self._factory = adapter_factory or (
            lambda cancelled: adapter_for(installation, cancelled=cancelled))
        # A development build, or an owner who switched checks off, never asks.
        self.enabled = bool(enabled) and installation.channel in ("pypi", "ghcr")
        self._clock = clock
        self._mono = monotonic
        self._interval = interval
        self._startup_delay = startup_delay
        self._cooldown = cooldown
        self._backoff = tuple(backoff)
        self._jitter = jitter
        self._rng = rng or random.Random()
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread = None
        self._running = False
        self._requested = False
        self._last_start = None
        self._failures = 0
        self._retry_after = None
        self._attempt_time = None
        self._adapter = None
        self.runs = 0

    # -- public ---------------------------------------------------------

    @property
    def channel(self):
        return self.installation.channel

    def is_checking(self):
        with self._lock:
            return self._running or self._requested

    def start(self):
        """Begin the background loop. Returns at once; nothing here blocks startup."""
        if not self.enabled or self._thread is not None:
            return self
        self._thread = threading.Thread(target=self._loop, name="release-check", daemon=True)
        self._thread.start()
        return self

    def stop(self, timeout=3.0):
        """Ask the loop to end and wait briefly. A request in flight is bounded
        by its own timeouts and is abandoned without writing."""
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout)

    def request_check(self):
        """Queue a check now. Returns ``(outcome, retry_after_seconds)``.

        ``queued`` starts one; ``coalesced`` joins one already running or
        queued; ``throttled`` is refused because a check started within the
        last minute; ``rate_limited`` is refused because the registry itself
        asked not to be contacted yet (the wait is the registry's own, and
        survives a restart); ``disabled`` means this installation is never
        checked.
        """
        if not self.enabled:
            return "disabled", None
        with self._lock:
            if self._running or self._requested:
                return "coalesced", None
            blocked = self._blocked_for()
            if blocked > 0:
                return "rate_limited", int(math.ceil(blocked))
            if self._last_start is not None:
                wait = self._cooldown - (self._mono() - self._last_start)
                if wait > 0:
                    return "throttled", max(1, int(math.ceil(wait)))
            self._requested = True
            self._last_start = self._mono()
        self._wake.set()
        return "queued", None

    def next_check_at(self):
        """When the next scheduled check is due, as a timestamp, or None."""
        if not self.enabled:
            return None
        if self.runs == 0:
            # Nothing has run in this process: the first check is due after
            # the startup delay, the restart spacing and any registry wait.
            return stamp(self._clock() + self._first_delay())
        row = self._row()
        base = parse_stamp((row or {}).get("last_attempt_at"))
        if base is None:
            return stamp(self._clock() + self._startup_delay)
        due = base + self._nominal_delay()
        until = parse_stamp((row or {}).get("retry_after_at"))
        return stamp(max(due, until) if until is not None else due)

    # -- scheduling -----------------------------------------------------

    def _loop(self):
        delay = self._first_delay()
        while not self._stop.is_set():
            self._wake.wait(delay)
            if self._stop.is_set():
                break
            self._wake.clear()
            self.run_once()
            delay = self._next_delay()

    def _first_delay(self):
        row = self._row()
        attempted = parse_stamp((row or {}).get("last_attempt_at"))
        delay = self._startup_delay
        if attempted is not None:
            # A crash-looping container must not ask a registry every start.
            delay = max(delay, attempted + RESTART_SPACING_S - self._clock())
        # What the registry asked for outlasts a restart.
        return max(delay, self._blocked_for())

    def _blocked_for(self):
        """Seconds until the registry's own "ask again after" time, or 0."""
        previous = self._previous()
        until = parse_stamp((previous or {}).get("retry_after_at"))
        if until is None:
            return 0
        return max(0, until - self._clock())

    def _nominal_delay(self):
        """The wait after the last attempt, before any random spread."""
        if self._failures:
            index = min(self._failures, len(self._backoff)) - 1
            delay = min(self._backoff[index], self._interval)
            if self._retry_after:
                delay = max(delay, self._retry_after)
            return delay
        return self._interval

    def _next_delay(self):
        delay = self._nominal_delay()
        if self._failures:
            return delay
        spread = delay * self._jitter
        return delay + self._rng.uniform(-spread, spread)

    # -- one check ------------------------------------------------------

    def run_once(self):
        """Run a check in the calling thread unless one is already running."""
        with self._lock:
            if self._running:
                return False
            self._running = True
            self._requested = False
            self._last_start = self._mono()
        try:
            self.runs += 1
            self._check()
        except Cancelled:
            log.debug("a release check was abandoned at shutdown")
        except Exception:
            # A bug in parsing is not the registry's fault, but the person
            # reading the page is told only a fixed code.
            log.exception("the release check failed unexpectedly")
            self._finish_failure("invalid_metadata", None)
        finally:
            with self._lock:
                self._running = False
        return True

    def _check(self):
        installation = self.installation
        previous = self._previous()
        self._begin(previous)
        adapter = self._adapter = self._factory(self._stop.is_set)
        if adapter is None:
            return
        if release_policy.parse_running(installation.running) is None:
            return self._finish_failure("running_version_unknown", None, previous)
        try:
            releases = adapter.releases()
        except RegistryError as exc:
            return self._finish_failure(exc.code, exc.retry_after, previous)
        outcome = release_policy.choose(installation.running, releases,
                                        ambiguous=installation.ambiguous)
        if outcome is None:  # pragma: no cover - parse_running succeeded above
            return self._finish_failure("running_version_unknown", None, previous)
        if self._stop.is_set():
            raise Cancelled()
        latest = outcome.latest.label if outcome.latest is not None else None
        self._failures = 0
        self._retry_after = None
        self._save({
            "latest": latest, "checked_at": stamp(self._clock()),
            "state": outcome.state, "error_code": None, "retry_after_at": None,
            "source_url": adapter.package_url(
                latest if outcome.state == "available" else None)}, previous)

    # -- the cache row --------------------------------------------------

    def _row(self):
        try:
            return self.storage.release_cache(self.installation.channel)
        except Exception:
            log.debug("the release cache could not be read", exc_info=True)
            return None

    def _previous(self):
        """The last row, but only if it describes this running version."""
        row = self._row()
        if row is not None and row.get("running") == self.installation.running:
            return row
        return None

    def _save(self, fields, previous):
        record = {"channel": self.installation.channel, "running": self.installation.running,
                  "latest": None, "checked_at": None, "retry_after_at": None,
                  "last_attempt_at": stamp(self._attempt_time), "state": "unknown",
                  "error_code": None, "source_url": None}
        if previous is not None:
            record["latest"] = previous.get("latest")
            record["checked_at"] = previous.get("checked_at")
            record["source_url"] = previous.get("source_url")
            record["retry_after_at"] = previous.get("retry_after_at")
        record.update(fields)
        try:
            self.storage.save_release_cache(record)
        except Exception:
            log.exception("the release cache could not be written")

    def _begin(self, previous):
        """Record that an attempt started. A first check shows ``checking``;
        one that already has a result keeps showing it until the new one lands,
        so a recheck never flickers an available update away."""
        self._attempt_time = self._clock()
        fields = {}
        if previous is None or previous.get("checked_at") is None:
            fields["state"] = "checking"
            fields["error_code"] = None
        else:
            fields["state"] = previous["state"]
            fields["error_code"] = previous.get("error_code")
        self._save(fields, previous)

    def _finish_failure(self, code, retry_after, previous=None):
        if self._stop.is_set():
            return
        if previous is None:
            previous = self._previous()
        self._failures += 1
        if code == "rate_limited":
            # A registry that says "too many requests" without saying when
            # still gets a real wait, not the next scheduled retry.
            retry_after = retry_after or RATE_LIMIT_DEFAULT_S
            self._retry_after = retry_after
        else:
            retry_after = None
            self._retry_after = None
        if self._attempt_time is None:  # pragma: no cover - defensive
            self._attempt_time = self._clock()
        source = None
        adapter = self._adapter
        if adapter is not None and (previous is None or not previous.get("source_url")):
            source = adapter.package_url()
        fields = {"state": "error", "error_code": code,
                  "retry_after_at": (stamp(self._clock() + retry_after)
                                     if retry_after else None)}
        if source:
            fields["source_url"] = source
        self._save(fields, previous)
