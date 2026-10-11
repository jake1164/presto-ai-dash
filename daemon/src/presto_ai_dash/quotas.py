"""Provider-independent allowance windows, preserving observation and reset times."""

from copy import deepcopy
from datetime import datetime, timezone
import math

from .models import iso, now_utc, severity

QUOTA_SCHEMA_VERSION = 1


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
