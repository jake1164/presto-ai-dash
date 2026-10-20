"""Provider-independent allowance windows, preserving observation and reset times."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import math

from .models import iso, now_utc, severity

QUOTA_SCHEMA_VERSION = 1

# The device draws a hint instead of a reset note, so it must stay short.
MAX_HINT_CHARS = 28

# Claude's weekly window has a fixed cycle (Phase 5); its 5h window does not.
WEEKLY_WINDOW_ID = "seven_day"


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


def compact_tokens(count):
    """A token total for the detail card: plain, whole k under a million, else M."""
    if count is None:
        return None
    if count < 1000:
        return "%d" % count
    if count < 1000000:
        return "%dk" % (count // 1000)
    return "%.1fM" % (count / 1000000)


def estimated_weekly_reset(windows, now):
    """The next weekly reset guessed from the newest real seven_day reset.

    The weekly cycle is fixed: from a real reset R (even one long past) the
    next reset is R plus 7 days, repeated until it lies after ``now``. Returns
    None when no weekly window ever reported a reset, and when the newest real
    reset is still in the future - a measured future reset is shown as today,
    only a passed one needs the guess. The 5h window never gets an estimate:
    its start rule is not documented. Callers must label the result an
    estimate; it is never presented as a provider reading.
    """
    real = [timestamp(window["resets_at"]) for window in windows
            if window.get("id") == WEEKLY_WINDOW_ID and window.get("resets_at")]
    if not real:
        return None
    estimate = max(real)
    if estimate > now:
        return None
    while estimate <= now:
        estimate += timedelta(days=7)
    return estimate


def activity_hint(report, measured, last_request_at, now):
    """The Phase 5 hint while the local activity reader is working.

    Called when Claude has been used within the last 24 hours; older silence
    belongs to the Phase 4 rules. At most MAX_HINT_CHARS ASCII characters;
    if the quota-age wording does not fit, the word "old" goes first.
    """
    idle = (now - timestamp(last_request_at)).total_seconds()
    if not measured:
        tail = "no quota data"
    elif all(window["resets_at"] and timestamp(window["resets_at"]) <= now
             for window in measured):
        tail = "quota expired"
    else:
        newest = max(timestamp(window["observed_at"]) for window in report["windows"])
        tail = "quota %s old" % compact_age((now - newest).total_seconds())
    active_seconds = report.get("active_seconds", 300)
    if idle <= active_seconds:
        head = "In use"
    else:
        head = "Used %s ago" % compact_age(idle)
    hint = "%s - %s" % (head, tail)
    if len(hint) > MAX_HINT_CHARS and tail.endswith(" old"):
        hint = hint[:-len(" old")]
    return hint


def claude_hint(report, now=None):
    """Why a Claude account's readings are missing or old, and what to do.

    Returns a hint of at most 28 ASCII characters, or None. Reports without the
    observation trail (collector_seen_at and friends) get None, so providers
    that send no hint are never decorated. First match wins; every answer stays
    a warning, because none of these cases means the provider is unreachable.
    While the Phase 5 local activity reader reports use in the last 24 hours,
    its in-use table speaks instead; with the reader off or failing, exactly
    the Phase 4 rules apply.
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
    seen = report.get("collector_seen_at")
    activity = report.get("local_activity")
    if (isinstance(activity, dict) and activity.get("available")
            and activity.get("last_request_at")):
        idle = (now - timestamp(activity["last_request_at"])).total_seconds()
        if idle < 24 * 3600:
            return activity_hint(report, measured, activity["last_request_at"], now)
    if not measured:
        if seen:
            return "Waiting for a Claude reply"
        return "Run claude in a terminal"
    if all(window["resets_at"] and timestamp(window["resets_at"]) <= now
           for window in measured):
        return "Awaiting refresh"
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


def ascii_label(value, limit=32):
    """A printable ASCII label, bounded; anything else becomes "unknown"."""
    if not isinstance(value, str):
        return "unknown"
    text = "".join(char for char in value if 32 <= ord(char) < 127).strip()
    if not text:
        return "unknown"
    return text[:limit]


def mark_passed_resets(quota, now=None):
    """Flag the windows whose reset time has passed, for the admin page.

    The value of such a window is its last known one, so the page says so
    instead of presenting it as a current reading.
    """
    now = timestamp(now or now_utc())
    for window in quota.get("windows", []):
        resets_at = window.get("resets_at")
        window["reset_passed"] = bool(resets_at and timestamp(resets_at) <= now)
    return quota


def claude_provenance(report, config=None, now=None):
    """Labelled lines that say where each Claude number came from.

    The quota line names the terminal export, the time of the reading, the
    host that reported it and how old it is. The activity line names the
    local session logs and what they say, or why they say nothing. The status
    note repeats the freshness hint. Nothing here is stored: the lines are
    derived from the report the daemon already holds.
    """
    now = timestamp(now or now_utc())
    report = report if isinstance(report, dict) else {}
    config = config if isinstance(config, dict) else {}
    return {"quota": _quota_source_line(report, now),
            "activity": _activity_source_line(report, config, now),
            "status_note": _status_note_line(report)}


def _quota_source_line(report, now):
    """Where the quota numbers came from, and how old they are."""
    measured = [window for window in report.get("windows", [])
                if isinstance(window, dict) and window.get("used_percent") is not None
                and window.get("observed_at")]
    if not measured:
        return "Quota: no reading yet (use claude in a terminal)"
    observed = max(timestamp(window["observed_at"]) for window in measured)
    when = observed.astimezone().strftime("%Y-%m-%d %H:%M")
    host = report.get("collector_host")
    origin = " from %s" % ascii_label(host) if host else ""
    return "Quota: terminal export - last reading %s%s (%s old)" % (
        when, origin, age_label((now - observed).total_seconds()))


def _activity_source_line(report, config, now):
    """Where the activity numbers came from, and what they say."""
    if not config.get("activity_logs"):
        return "Activity: reader off (tick 'Read local Claude Code activity' in Edit)"
    activity = report.get("local_activity")
    if not isinstance(activity, dict):
        return "Activity: unavailable (no reading yet)"
    if not activity.get("available"):
        return "Activity: unavailable (%s)" % ascii_label(
            activity.get("reason") or "unknown reason", 60)
    last = activity.get("last_request_at")
    if last:
        head = "Activity: local logs - last used %s ago (%s)" % (
            compact_age((now - timestamp(last)).total_seconds()),
            ascii_label(activity.get("last_entrypoint") or "?", 12))
    else:
        head = "Activity: local logs - no request recorded yet"
    if not activity.get("complete", True):
        return "%s - tokens scanning" % head
    five = compact_tokens(activity.get("tokens_5h"))
    seven = compact_tokens(activity.get("tokens_7d"))
    if five is None or seven is None:
        return "%s - tokens unknown" % head
    return "%s - tokens 5h/7d %s/%s" % (head, five, seven)


def _status_note_line(report):
    """The freshness hint, labelled so it never reads as a measurement."""
    hint = report.get("hint")
    if not isinstance(hint, str) or not hint.strip():
        return None
    return "Status note: %s" % hint


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
    if (report.get("warn_on_stale", True) and
            (any(w["state"] == "stale" for w in result.metrics["quota"]["windows"]) or
             result.metrics["quota"].get("activity", {}).get("state") == "stale") and
             severity(result.status) < severity("warning")):
        result.status = "warning"
    return result
