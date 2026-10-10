"""Alert calculation and trend indicators.

v0 has thresholds, not a rule engine. This module decides when a snapshot
escalates to ``warning`` or ``critical``, and computes the simple up/down/flat
trends the plan asks for.
"""

import logging
from datetime import timedelta

from .models import Alert, normalise_status, now_utc, severity

log = logging.getLogger(__name__)

# How far back to look when comparing trends.
TREND_WINDOW_HOURS = 24

# Trend directions.
UP = "up"
DOWN = "down"
FLAT = "flat"
UNKNOWN = "unknown"

# A relative change smaller than this is treated as flat.
FLAT_THRESHOLD = 0.02


def _threshold(config, key):
    """Read a threshold, treating 0 and missing as "disabled"."""
    if not config:
        return None
    value = config.get(key, 0)
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def evaluate_thresholds(snapshot, config):
    """Return the alerts raised by a snapshot's metrics against thresholds.

    Only LiteLLM metrics have configurable thresholds in v0. A threshold of 0
    (the default) disables that check entirely.
    """
    alerts = []
    if not config:
        return alerts

    metrics = snapshot.metrics or {}

    checks = (
        ("spend_today", "litellm_daily_spend"),
        ("requests_today", "litellm_daily_requests"),
        ("errors_today", "litellm_daily_errors"),
    )

    for metric_key, config_prefix in checks:
        value = metrics.get(metric_key)
        if value is None or value == "unavailable":
            continue
        try:
            value = float(value)
        except (TypeError, ValueError):
            continue

        warn = _threshold(config, "%s_warn" % config_prefix)
        critical = _threshold(config, "%s_critical" % config_prefix)

        if critical is not None and value >= critical:
            alerts.append(Alert(
                "critical",
                "%s is %s, at or above the critical threshold of %s"
                % (metric_key, _format(value), _format(critical)),
                source_id=snapshot.source_id,
            ))
        elif warn is not None and value >= warn:
            alerts.append(Alert(
                "warning",
                "%s is %s, at or above the warning threshold of %s"
                % (metric_key, _format(value), _format(warn)),
                source_id=snapshot.source_id,
            ))

    return alerts


def _format(value):
    """Format a threshold value without a trailing ``.0``."""
    if float(value).is_integer():
        return str(int(value))
    return str(value)


def escalate_status(snapshot, alerts):
    """Raise a snapshot's status to match its worst alert.

    A snapshot never drops below the severity of an alert it carries, so a
    threshold breach cannot be hidden by an otherwise healthy source.
    """
    status = normalise_status(snapshot.status)
    for alert in alerts:
        alert_status = normalise_status(alert.get("severity"))
        if severity(alert_status) > severity(status):
            status = alert_status
    return status


def trend(current, previous):
    """Compare two numbers and return ``up``, ``down``, ``flat`` or ``unknown``.

    ``unknown`` is returned whenever either value is missing or non-numeric.
    The device shows a dash rather than guessing a direction.
    """
    if current is None or previous is None:
        return UNKNOWN
    try:
        current = float(current)
        previous = float(previous)
    except (TypeError, ValueError):
        return UNKNOWN

    if previous == 0:
        return UP if current > 0 else FLAT

    change = (current - previous) / abs(previous)
    if change > FLAT_THRESHOLD:
        return UP
    if change < -FLAT_THRESHOLD:
        return DOWN
    return FLAT


def trend_arrow(direction):
    """The glyph shown on the device for a trend direction."""
    return {
        UP: "^",
        DOWN: "v",
        FLAT: "-",
        UNKNOWN: "-",
    }.get(direction, "-")


def compute_trends(snapshots, storage, window_hours=TREND_WINDOW_HOURS):
    """Compute trends for each source from stored history.

    Compares the newest snapshot to the oldest within the lookback window.
    With fewer than two snapshots in the window the trend is ``unknown``.
    """
    trends = {}
    cutoff = now_utc() - timedelta(hours=window_hours)

    for source_id, snapshot in snapshots.items():
        history = storage.snapshots_since(source_id, cutoff)
        if len(history) < 2:
            trends[source_id] = {
                "tokens": UNKNOWN,
                "requests": UNKNOWN,
                "speed": UNKNOWN,
            }
            continue

        oldest = history[0]
        newest = history[-1]

        trends[source_id] = {
            "tokens": trend(newest.metrics.get("tokens_today"),
                            oldest.metrics.get("tokens_today")),
            "requests": trend(newest.metrics.get("requests_today"),
                              oldest.metrics.get("requests_today")),
            "speed": trend(newest.metrics.get("last_measured_tokens_per_sec"),
                           oldest.metrics.get("last_measured_tokens_per_sec")),
        }

    return trends


def recently_active(snapshot, within_minutes=30):
    """Whether a source reported activity recently.

    Used for the plan's "provider recently active/inactive" indicator.
    """
    if snapshot is None:
        return False
    if normalise_status(snapshot.status) == "active":
        return True
    if snapshot.updated_at is None:
        return False
    age = now_utc() - snapshot.updated_at
    return age <= timedelta(minutes=within_minutes)
