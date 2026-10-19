"""Provider-independent allowance windows, preserving observation and reset times."""

from copy import deepcopy
from datetime import datetime, timezone
import math

from .models import iso, now_utc, severity

QUOTA_SCHEMA_VERSION = 1

# The device draws a hint instead of a reset note, so it must stay short.
MAX_HINT_CHARS = 28


def compact_age(seconds):
    """A compact ASCII age: 59m, 1h, 47h, 2d. A future moment counts as 0m."""
    if seconds is None:
        return None
    seconds = max(0, int(seconds))
    minutes = seconds // 60
    if minutes < 60:
        return "%dm" % minutes
    hours = seconds // 3600
    if hours < 48:
        return "%dh" % hours
    return "%dd" % (seconds // 86400)


def age_label(seconds):
    """A readable age for small detail text; the 48h boundary jumps to days."""
    if seconds is None:
        return None
    seconds = max(0, int(seconds))
    minutes = seconds // 60
    if minutes < 60:
        return "%d min" % minutes
    hours = seconds // 3600
    if hours < 48:
        return "%d h" % hours
    # At 48 hours this becomes 2 days; "1 day" is unreachable by design.
    days = seconds // 86400
    return "%d day%s" % (days, "" if days == 1 else "s")


def claude_hint(report, now=None):
    """Why a Claude account's readings are missing or old, and what to do.

    Returns a hint of at most 28 ASCII characters, or None. Reports without the
    observation trail (collector_seen_at and friends) get None, so providers
    that send no hint are never decorated. First match wins; every answer stays
    a warning, because none of these cases means the provider is unreachable.
    """
    if "collector_seen_at" not in report:
        return None
    now = timestamp(now or now_utc())
    stale_after = report.get("stale_after_seconds", 180)
    fresh = not report.get("error") and any(
        window["used_percent"] is not None and
        (not window["resets_at"] or timestamp(window["resets_at"]) > now) and
        (now - timestamp(window["observed_at"])).total_seconds() <= stale_after
        for window in report["windows"])
    if fresh:
        return None
    measured = [window for window in report["windows"]
                if window["used_percent"] is not None]
    if not measured:
        if report.get("collector_seen_at"):
            return "Waiting for a Claude reply"
        return "Run claude in a terminal"
    if all(window["resets_at"] and timestamp(window["resets_at"]) <= now
           for window in measured):
        return "Awaiting refresh"
    seen = report.get("collector_seen_at")
    if seen and (now - timestamp(seen)).total_seconds() <= stale_after:
        return "Reply in claude to refresh"
    # The newest signal of any kind quotes: a recent collector beats an old
    # window headline. Without a collector (migrated data), the quota leads.
    signals = [timestamp(window["observed_at"]) for window in report["windows"]]
    if seen:
        signals.append(timestamp(seen))
    return "No report %s - run claude" % compact_age((now - max(signals)).total_seconds())


def timestamp(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("quota timestamps must include a timezone")
    return value.astimezone(timezone.utc)


def quota_window(window_id, label, percent, reset, observed, period_minutes=None,
                 state="available", used_amount=None, limit_amount=None, currency=None):
    if state not in ("available", "unavailable", "unlimited"):
        raise ValueError("invalid quota availability")
    if not isinstance(window_id, str) or not window_id or not isinstance(label, str) or not label:
        raise ValueError("quota window requires an identity and label")
    if percent is not None and (type(percent) not in (int, float) or
                                not math.isfinite(percent) or percent < 0):
        raise ValueError("quota percentage must be a finite nonnegative number")
    if state != "available":
        percent = None
    elif percent is None:
        state = "unavailable"
    if period_minutes is not None and (type(period_minutes) is not int or period_minutes <= 0):
        raise ValueError("quota period must be a positive number of minutes")
    result = {"id": window_id, "label": label, "period_minutes": period_minutes,
              "used_percent": percent, "resets_at": iso(timestamp(reset)) if reset else None,
              "observed_at": iso(timestamp(observed)), "state": state}
    for key, amount in (("used_amount", used_amount), ("limit_amount", limit_amount)):
        if amount is not None:
            if type(amount) not in (int, float) or not math.isfinite(amount) or amount < 0:
                raise ValueError("quota amount must be a finite nonnegative number")
            result[key] = amount
    if currency is not None:
        if currency != "USD":
            raise ValueError("unsupported quota currency")
        result["currency"] = currency
    return result


def quota_report(provider, product, windows, stale_after_seconds=180):
    if type(stale_after_seconds) is not int or stale_after_seconds < 1:
        raise ValueError("quota freshness must be positive")
    ids = [window["id"] for window in windows]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate quota window identity")
    return {"provider": provider, "product": product, "windows": windows,
            "stale_after_seconds": stale_after_seconds, "error": None}


def present_quota(report, now=None):
    """Age stored telemetry at read time; never invent a new reading at reset."""
    result = deepcopy(report)
    now = timestamp(now or now_utc())
    activity = result.get("activity")
    if activity:
        activity["state"] = "stale" if (result.get("error") or
            (now - timestamp(activity["observed_at"])).total_seconds() > result["stale_after_seconds"]) else "available"
    for window in result["windows"]:
        if window["state"] not in ("available", "unlimited"):
            continue
        observed = timestamp(window["observed_at"])
        reset = timestamp(window["resets_at"]) if window["resets_at"] else None
        if result.get("error") or (now - observed).total_seconds() > result["stale_after_seconds"]:
            window["state"] = "stale"
        if reset and reset <= now:
            window["state"] = "stale"
            window["reason"] = "awaiting provider reading after reset"
    if "collector_seen_at" in result:
        # Built here, not at collect time, so the hint ages between polls.
        result["hint"] = claude_hint(result, now)
    return result


def present_snapshot(snapshot, now=None):
    report = snapshot.metrics.get("quota")
    if not report:
        return snapshot
    result = deepcopy(snapshot)
    result.metrics["quota"] = present_quota(report, now)
    if ((any(w["state"] == "stale" for w in result.metrics["quota"]["windows"]) or
         result.metrics["quota"].get("activity", {}).get("state") == "stale") and
            severity(result.status) < severity("warning")):
        result.status = "warning"
    return result
