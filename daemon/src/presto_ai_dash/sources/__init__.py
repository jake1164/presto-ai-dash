"""Source interface and registry.

v0 keeps sources internal but structured: trusted modules in this directory
are discovered at startup and register their classes. There are no external
entry points, package installation, or browser uploads.
"""

import logging
import importlib
import os
import pkgutil

from ..models import SourceSnapshot, normalise_status

log = logging.getLogger(__name__)


class Source(object):
    """Base class for every source.

    Subclasses set ``id`` and ``name`` and implement ``collect()``. ``collect()``
    must return a ``SourceSnapshot`` and should not raise: a source that fails
    returns an ``offline`` snapshot carrying the error text instead, so one
    broken source cannot take down the daemon.
    """

    id = "base"
    name = "Base"
    config_fields = ()
    secret_field = None
    # Quota reports composed by ingest() carry these; None for non-quota sources.
    provider = None
    product = None
    stale_after_seconds = 180
    report_note = None
    # Remote collectors may push readings only when a source opts in, and only
    # for the window ids it publishes.
    accepts_push = False
    push_window_ids = None

    def __init__(self, config=None):
        self.config = config or {}

    def collect(self):
        raise NotImplementedError

    def attach_storage(self, storage):
        """Optional access for local export connectors; ordinary sources ignore it."""

    # -- helpers for subclasses -------------------------------------------

    def snapshot(self, status, metrics=None, alerts=None, cards=None,
                 error=None):
        return SourceSnapshot(
            source_id=self.id,
            status=status,
            metrics=metrics,
            alerts=alerts,
            cards=cards,
            error=error,
        )

    def offline(self, message):
        """An offline snapshot carrying an error message."""
        message = self._redact(message)
        log.warning("source %s offline: %s", self.id, message)
        return self.snapshot("offline", error=message)

    def collect_safely(self):
        """Run ``collect()``, converting any exception into an offline snapshot.

        This is what the collector thread calls. It never raises.
        """
        try:
            snapshot = self.collect()
        except Exception as exc:  # noqa: BLE001 - a source must never kill the daemon
            # Exceptions from external services can contain reflected credentials.
            if self.secret_field and self.config.get(self.secret_field):
                log.error("source %s collection failed (%s)", self.id, type(exc).__name__)
                return self.offline("source collection failed (%s)" % type(exc).__name__)
            log.exception("source %s raised during collect()", self.id)
            return self.offline("%s: %s" % (type(exc).__name__, exc))

        if not isinstance(snapshot, SourceSnapshot):
            log.error("source %s returned %s, not a SourceSnapshot",
                      self.id, type(snapshot).__name__)
            return self.offline("source returned an invalid snapshot")

        # Normalise defensively: a source must not be able to invent a status.
        snapshot.status = normalise_status(snapshot.status)
        snapshot.source_id = self.id
        snapshot.metrics = self._redact(snapshot.metrics)
        snapshot.alerts = self._redact(snapshot.alerts)
        snapshot.cards = self._redact(snapshot.cards)
        snapshot.error = self._redact(snapshot.error)
        return snapshot

    def _redact(self, value):
        secret = self.config.get(self.secret_field) if self.secret_field else None
        if not secret:
            return value
        if isinstance(value, str):
            return value.replace(secret, "[redacted]")
        if isinstance(value, list):
            return [self._redact(item) for item in value]
        if isinstance(value, dict):
            return {self._redact(key): self._redact(item) for key, item in value.items()}
        return value


_REGISTRY = {}


def register(source_class):
    """Class decorator that adds a source to the registry."""
    _REGISTRY[source_class.id] = source_class
    return source_class


def available_sources():
    """The registered source ids, in registration order."""
    return list(_REGISTRY.keys())


def source_type(type_id):
    return _REGISTRY[type_id]


CONTAINER_ENV = "DASH_IN_CONTAINER"
# What a container calls the machine it runs on (Docker Desktop; on Linux the
# compose file maps it with host-gateway).
CONTAINER_HOST = "host.docker.internal"
CONTAINER_NOTE = ("The daemon runs in a container, so localhost is the container itself. "
                  "To reach a program on this computer use " + CONTAINER_HOST + ".")


def in_container(environ=None, exists=os.path.exists):
    """Whether the daemon runs in a container: the image says so, and Docker and
    Podman leave a marker file."""
    environ = os.environ if environ is None else environ
    return (environ.get(CONTAINER_ENV, "").strip().lower() in ("1", "true", "yes")
            or exists("/.dockerenv") or exists("/run/.containerenv"))


def _container_field(field):
    """A URL field's default and a note, for a daemon in a container."""
    if field.get("type") != "url" or not str(field.get("default", "")).startswith(
            "http://localhost:"):
        return field
    field = dict(field)
    field["default"] = field["default"].replace("//localhost:", "//" + CONTAINER_HOST + ":", 1)
    field["note"] = CONTAINER_NOTE
    return field


def source_catalog(container=None):
    container = in_container() if container is None else container
    return [{"id": cls.id, "name": cls.name,
             "fields": [_container_field(f) if container else f for f in cls.config_fields],
             "has_secret": bool(cls.secret_field)}
            for cls in _REGISTRY.values()]


def build_sources(config):
    """Instantiate every enabled source.

    Returns a list of ``Source`` instances. A source whose config section is
    missing is skipped rather than crashing the daemon.
    """
    sources = []
    sources_config = config.get("sources", {})

    for source_id, source_class in _REGISTRY.items():
        section = sources_config.get(source_id, {})
        if not section.get("enabled", False):
            log.debug("source %s is disabled", source_id)
            continue
        try:
            sources.append(source_class(section))
        except Exception as exc:  # noqa: BLE001
            log.error("could not initialise source %s: %s", source_id, exc)

    return sources


# Importing the modules registers their classes. Keep this at the bottom to
# avoid a circular import.
from . import ollama  # noqa: E402,F401
from . import litellm  # noqa: E402,F401

# Developer-installed source modules are trusted code. Discovery happens at
# startup, never from an upload or a web request.
for _module in sorted(pkgutil.iter_modules(__path__), key=lambda item: item.name):
    if not _module.name.startswith("_") and _module.name not in ("ollama", "litellm"):
        try:
            importlib.import_module("." + _module.name, __name__)
        except Exception:
            log.error("source module %s could not be loaded", _module.name)
