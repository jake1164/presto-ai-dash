"""Provider-neutral observation ingest: one validator, one freshness rule.

Both transports — the in-process status-line helper and the remote HTTP route
(Phases 3+) — store readings through :func:`ingest`, so every envelope passes
the same validation before anything is written. The client's clock is
informational: every stored timestamp, ``collector_seen_at`` included, is
stamped by :meth:`Storage.save_observation`, never trusted from the envelope.
"""

from datetime import datetime, timedelta, timezone
import logging
import math
import re

from .collectors import served_version, update_state
from .ingest_extras import clean_activity, clean_credits, clean_reset_credits, clean_usage_cache
from .models import now_utc
from .quotas import quota_report
from .sources import source_type

log = logging.getLogger(__name__)

SCHEMA_NAME = "presto-ai-dash.observation/1"
MAX_WINDOWS = 8
MAX_HOST_CHARS = 32
MAX_NAME_CHARS = 64
MAX_VERSION_CHARS = 32
MAX_PROVIDER_VERSION_CHARS = 64
MAX_LABEL_CHARS = 32
MAX_PERIOD_MINUTES = 527040
MAX_USED_PERCENT = 1000
MAX_RESET_DAYS = 400

_TOP_LEVEL_KEYS = frozenset(("schema", "collector", "provider_version", "client_time",
                             "windows", "evidence", "progress"))
_COLLECTOR_KEYS = frozenset(("name", "version", "host"))
_WINDOW_KEYS = frozenset(("id", "label", "period_minutes", "used_percent", "resets_at", "state"))
# Letters, digits and _ . : (a Codex bucket id is part of its window id).
_WINDOW_ID = re.compile(r"[a-z0-9_.:]{1,32}\Z")
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_STATES = frozenset(("available", "unavailable", "unlimited"))


def _ascii(value, limit):
    return (isinstance(value, str) and 0 < len(value) <= limit and
            all(0x20 <= ord(char) <= 0x7E for char in value))


def _moment(value, what):
    if not isinstance(value, str) or not value:
        raise ValueError("%s must be an ISO-8601 string" % what)
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("%s must be ISO-8601" % what) from None


def _check_hash(value, what):
    if value is not None and (not isinstance(value, str) or not _HASH.match(value)):
        raise ValueError("%s must be 64 lowercase hex characters" % what)


def _check_window(window, seen_ids, allowed_ids, now):
    if not isinstance(window, dict) or set(window) != _WINDOW_KEYS:
        raise ValueError("window must have exactly the quota window fields")
    window_id = window["id"]
    if not isinstance(window_id, str) or not _WINDOW_ID.match(window_id):
        raise ValueError("window id must be 1-32 lowercase letters, digits or _ . :")
    if window_id in seen_ids:
        raise ValueError("duplicate window id: %s" % window_id)
    if allowed_ids is not None and window_id not in allowed_ids:
        raise ValueError("window %s is not published by this source" % window_id)
    seen_ids.add(window_id)
    if not _ascii(window["label"], MAX_LABEL_CHARS):
        raise ValueError("window label must be 1-%d ASCII-printable characters" % MAX_LABEL_CHARS)
    period = window["period_minutes"]
    if period is not None and (type(period) is not int or
                               not 1 <= period <= MAX_PERIOD_MINUTES):
        raise ValueError("period_minutes must be an integer from 1 to %d" % MAX_PERIOD_MINUTES)
    percent = window["used_percent"]
    if percent is not None and (type(percent) not in (int, float) or not math.isfinite(percent) or
                                not 0 <= percent <= MAX_USED_PERCENT):
        raise ValueError("used_percent must be a finite number from 0 to %d" % MAX_USED_PERCENT)
    if window["state"] not in _STATES:
        raise ValueError("window state must be a quota state")
    resets_at = window["resets_at"]
    if resets_at is not None:
        reset = _moment(resets_at, "resets_at")
        if reset.tzinfo is None:
            raise ValueError("resets_at must include a UTC offset")
        if abs(reset.astimezone(timezone.utc) - now) > timedelta(days=MAX_RESET_DAYS):
            raise ValueError("resets_at must be within %d days of now" % MAX_RESET_DAYS)


def validate(envelope, source_class):
    """Reject an envelope that breaks the protocol; unknown keys are never ignored.

    Raises ValueError on the first bad field so a transport can map it onto a
    400 response without echoing the input back.
    """
    if not isinstance(envelope, dict):
        raise ValueError("envelope must be a JSON object")
    unknown = set(envelope) - _TOP_LEVEL_KEYS - set(getattr(source_class, "push_extras", ()))
    if unknown:
        raise ValueError("unknown top-level key: %s" % ", ".join(sorted(unknown)))
    if envelope.get("schema") != SCHEMA_NAME:
        raise ValueError("unsupported envelope schema")
    collector = envelope.get("collector")
    if not isinstance(collector, dict) or set(collector) != _COLLECTOR_KEYS:
        raise ValueError("collector must have exactly name, version and host")
    if not _ascii(collector["name"], MAX_NAME_CHARS):
        raise ValueError("collector name must be 1-%d ASCII-printable characters" % MAX_NAME_CHARS)
    if not _ascii(collector["version"], MAX_VERSION_CHARS):
        raise ValueError("collector version must be 1-%d ASCII-printable characters" % MAX_VERSION_CHARS)
    if not _ascii(collector["host"], MAX_HOST_CHARS):
        raise ValueError("collector host must be 1-%d ASCII-printable characters" % MAX_HOST_CHARS)
    provider_version = envelope.get("provider_version")
    if provider_version is not None and not _ascii(provider_version, MAX_PROVIDER_VERSION_CHARS):
        raise ValueError("provider_version must be 1-%d ASCII-printable characters" %
                         MAX_PROVIDER_VERSION_CHARS)
    if "client_time" in envelope:
        _moment(envelope["client_time"], "client_time")
    report_extras(envelope, source_class)
    windows = envelope.get("windows")
    if windows is not None:
        if not isinstance(windows, list):
            raise ValueError("windows must be a list; omit the key for a heartbeat")
        if not windows:
            raise ValueError("windows must not be empty; omit the key for a heartbeat")
        if len(windows) > MAX_WINDOWS:
            raise ValueError("at most %d windows are accepted" % MAX_WINDOWS)
        allowed_ids = getattr(source_class, "push_window_ids", None)
        seen_ids = set()
        now = datetime.now(timezone.utc)
        for window in windows:
            _check_window(window, seen_ids, allowed_ids, now)
    evidence = envelope.get("evidence")
    if evidence is not None:
        if not isinstance(evidence, dict):
            raise ValueError("evidence must map window ids to hashes")
        for key, value in evidence.items():
            if not isinstance(key, str) or not _WINDOW_ID.match(key):
                raise ValueError("evidence keys must be window ids")
            _check_hash(value, "evidence hash")
    _check_hash(envelope.get("progress"), "progress")


# Which report key each pushed reading is stored under. The two Claude readings
# are kept apart from the daemon's own readers' output, so the source can tell
# a reading pushed from another machine from one it read itself.
_EXTRA_KEYS = {"activity": "pushed_activity", "usage_cache": "pushed_usage_cache",
               "reset_credits": "reset_credits", "credits": "credits"}


def report_extras(envelope, source_class, now=None):
    """The cleaned optional readings of an envelope, under the keys they are stored by.

    Raises ValueError for one the source does not take, or one that is not
    what it says. Each carries ``received_at``, the daemon's own time.
    """
    now = now or datetime.now(timezone.utc)
    allowed = set(getattr(source_class, "push_extras", ()))
    result = {}
    for name in ("activity", "usage_cache", "reset_credits", "credits"):
        if name not in envelope:
            continue
        if name not in allowed:
            raise ValueError("this source does not take %s" % name)
        value = envelope[name]
        if name == "activity":
            cleaned = clean_activity(value, now)
        elif name == "usage_cache":
            cleaned = clean_usage_cache(value, now, _check_window)
        elif name == "reset_credits":
            cleaned = clean_reset_credits(value, now)
        else:
            cleaned = clean_credits(value)
        if name in ("activity", "usage_cache"):
            cleaned["received_at"] = now.isoformat()
        result[_EXTRA_KEYS[name]] = cleaned
    return result


def _default_report(source_class):
    """A report with every window unavailable, for readings that arrive before any quota."""
    windows = [{"id": key, "label": label, "period_minutes": minutes, "used_percent": None,
                "resets_at": None, "state": "unavailable"}
               for key, label, minutes in source_class.push_default_windows]
    report = quota_report(source_class.provider, source_class.product, windows,
                          source_class.stale_after_seconds)
    if source_class.report_note:
        report["note"] = source_class.report_note
    return report


def ingest(storage, instance_id, envelope, now=None):
    """Validate and store one observation for a source instance.

    Returns False for an unknown, disabled or non-push instance and raises
    ValueError for an invalid envelope. provider/product come from the source
    class, so this stays provider-generic.
    """
    record = next((row for row in storage.source_instances() if row["id"] == instance_id), None)
    if record is None or not record["enabled"]:
        return False
    source_class = source_type(record["type"])
    if not source_class.accepts_push:
        return False
    validate(envelope, source_class)
    received = now if now is not None else now_utc()
    extras = report_extras(envelope, source_class, received)
    report, base_report = None, None
    if "windows" in envelope:  # Present-but-empty is data too; only absence is a heartbeat.
        report = quota_report(source_class.provider, source_class.product,
                              [dict(window) for window in envelope["windows"]],
                              source_class.stale_after_seconds)
        if source_class.report_note:
            report["note"] = source_class.report_note
        report.update(extras)
    elif extras:
        if not getattr(source_class, "push_default_windows", None):
            raise ValueError("these readings need quota windows with them")
        base_report = _default_report(source_class)
    collector = envelope.get("collector")
    storage.save_observation(instance_id, report, envelope.get("evidence"),
                             collector_host=collector["host"],
                             collector_version=collector["version"],
                             progress=envelope.get("progress"),
                             now=received, extras=None if report is not None else extras,
                             base_report=base_report)
    _note_outdated(instance_id, collector)
    return True


# Which (account, version) pairs have already been mentioned in this run, so a
# collector that reports every minute is warned about once, not every minute.
_WARNED = set()


def _note_outdated(instance_id, collector):
    """One warning, in the log and the Problems list, per account and old version."""
    if update_state(collector["version"]) != "available":
        return
    key = (instance_id, collector["version"])
    if key in _WARNED:
        return
    _WARNED.add(key)
    log.warning("the collector on %s is version %s and version %s is available: "
                "download it again from the Settings tab and replace the file",
                collector["host"], collector["version"], served_version())
