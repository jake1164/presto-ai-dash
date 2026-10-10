"""Source interface and registry.

v0 keeps sources internal but structured: adding one means adding a module and
registering it here. There is no plugin loader, no entry points and no package
system, which is deliberate.
"""

import logging

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

    def __init__(self, config=None):
        self.config = config or {}

    def collect(self):
        raise NotImplementedError

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
        log.warning("source %s offline: %s", self.id, message)
        return self.snapshot("offline", error=message)

    def collect_safely(self):
        """Run ``collect()``, converting any exception into an offline snapshot.

        This is what the collector thread calls. It never raises.
        """
        try:
            snapshot = self.collect()
        except Exception as exc:  # noqa: BLE001 - a source must never kill the daemon
            log.exception("source %s raised during collect()", self.id)
            return self.offline("%s: %s" % (type(exc).__name__, exc))

        if not isinstance(snapshot, SourceSnapshot):
            log.error("source %s returned %s, not a SourceSnapshot",
                      self.id, type(snapshot).__name__)
            return self.offline("source returned an invalid snapshot")

        # Normalise defensively: a source must not be able to invent a status.
        snapshot.status = normalise_status(snapshot.status)
        return snapshot


_REGISTRY = {}


def register(source_class):
    """Class decorator that adds a source to the registry."""
    _REGISTRY[source_class.id] = source_class
    return source_class


def available_sources():
    """The registered source ids, in registration order."""
    return list(_REGISTRY.keys())


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
from . import fake  # noqa: E402,F401
# Real integrations are future milestone drafts and have no M2 runtime path.
