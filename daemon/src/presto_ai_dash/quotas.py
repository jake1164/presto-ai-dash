"""Provider-independent allowance windows, preserving observation and reset times."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import math

from .collectors.claude_usage_cache import REASON_MISSING as CACHE_FILE_MISSING
from .models import iso, now_utc, severity

QUOTA_SCHEMA_VERSION = 1

# The device draws a hint instead of a reset note, so it must stay short.
MAX_HINT_CHARS = 28

# Claude's weekly window has a fixed cycle (Phase 5); its 5h window does not.
WEEKLY_WINDOW_ID = "seven_day"

# Where a Claude reading came from. Every window carries one of these labels,
# so a number is never presented as if another source had produced it.
SOURCE_EXPORT = "terminal export"
SOURCE_CACHE = "usage cache"

# How long a reading from Claude Code's usage cache stays fresh. The cache is
# only refreshed when /usage is opened, so the terminal export's threshold
# would call a good reading stale. A Claude account can set its own value;
# this is the default when it does not.
USAGE_CACHE_STALE_SECONDS = 7200


def window_stale_after(report, window):
    """The freshness threshold that applies to one window.

    A window from Claude Code's usage cache has its own, longer threshold: the
    cache is only refreshed when /usage is opened, so the terminal export's
    threshold would call a good reading stale. Every other window keeps the
    report's own threshold, so no other provider and no terminal-export window
    changes.
    """
    if isinstance(window, dict) and window.get("source") == SOURCE_CACHE:
        value = report.get("usage_cache_stale_seconds")
        if type(value) is int and value >= 1:
            return value
    return report.get("stale_after_seconds", 180)


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

    An account that reads Claude Code's local usage cache gets the cache rules
    instead: the cache refreshes when /usage is opened in Claude Code, so the
    advice names that action rather than telling a panel user to open a
    terminal. With the reader off, every rule below is exactly as it was.
    """
    if "collector_seen_at" not in report:
        return None
    now = timestamp(now or now_utc())
    stale_after = report.get("stale_after_seconds", 180)
    fresh = not report.get("error") and any(
        window["used_percent"] is not None and
        (not window["resets_at"] or timestamp(window["resets_at"]) > now) and
        (now - timestamp(window["observed_at"])).total_seconds() <=
        window_stale_after(report, window)
        for window in report["windows"])
    if fresh:
        return None
    measured = [window for window in report["windows"]
                if window["used_percent"] is not None]
    seen = report.get("collector_seen_at")
    cache = report.get("usage_cache")
    if isinstance(cache, dict):
        if cache.get("available"):
            hint = cache_hint(report, measured, now)
            if hint:
                return hint
        elif not measured and cache.get("reason") == CACHE_FILE_MISSING:
            # The reader is on and Claude Code has not written the file yet:
            # opening /usage is what creates it. Nothing was ever measured, so
            # there is no quota to be old.
            return "Run /usage in Claude Code"
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


def cache_hint(report, measured, now):
    """The hint for an account that reads Claude Code's local usage cache.

    The cache is refreshed by opening /usage in Claude Code, so that is what
    the hint names. The quota age is that of the newest measured window, the
    same age the detail card shows. At most MAX_HINT_CHARS ASCII characters;
    when the age makes the full wording too long, the action goes first.
    """
    if not measured:
        return None
    if all(window["resets_at"] and timestamp(window["resets_at"]) <= now
           for window in measured):
        return "Reset passed - open /usage"
    newest = max(timestamp(window["observed_at"]) for window in measured)
    age = compact_age((now - newest).total_seconds())
    if claude_in_use(report, now):
        return "In use - quota %s old" % age
    hint = "Quota %s old - open /usage" % age
    if len(hint) > MAX_HINT_CHARS:
        hint = "Quota %s old" % age
    return hint


def claude_in_use(report, now):
    """Whether the local activity reader says Claude is in use right now."""
    activity = report.get("local_activity")
    if not (isinstance(activity, dict) and activity.get("available")
            and activity.get("last_request_at")):
        return False
    idle = (now - timestamp(activity["last_request_at"])).total_seconds()
    return idle <= report.get("active_seconds", 300)


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

    The quota line names the source of the reading, its time, the host that
    reported it and how old it is. The activity line names the local session
    logs and what they say, or why they say nothing. The usage cache lines
    say whether that reader works and what it found. The status note repeats
    the freshness hint. Nothing here is stored: the lines are derived from
    the report the daemon already holds.
    """
    now = timestamp(now or now_utc())
    report = report if isinstance(report, dict) else {}
    config = config if isinstance(config, dict) else {}
    return {"quota": _quota_source_line(report, now, config),
            "activity": _activity_source_line(report, config, now),
            "usage_cache": _usage_cache_line(report, config, now),
            "usage_cache_refreshes": _usage_cache_refresh_line(report, config, now),
            "credit": _credit_line(report),
            "status_note": _status_note_line(report, config)}


def _quota_source_line(report, now, config):
    """Where the quota numbers came from, and how old they are."""
    measured = [window for window in report.get("windows", [])
                if isinstance(window, dict) and window.get("used_percent") is not None
                and window.get("observed_at")]
    if not measured:
        if config.get("usage_cache"):
            # The cache reader is on, so opening /usage is what brings a
            # reading: telling this user to open a terminal would be wrong.
            return "Quota: no reading yet (open /usage in Claude Code)"
        return "Quota: no reading yet (use claude in a terminal)"
    newest = max(measured, key=lambda window: timestamp(window["observed_at"]))
    observed = timestamp(newest["observed_at"])
    when = observed.astimezone().strftime("%Y-%m-%d %H:%M")
    source = _source_label(newest.get("source"))
    host = report.get("collector_host")
    origin = " from %s" % ascii_label(host) if host and source == SOURCE_EXPORT else ""
    return "Quota: %s - last reading %s%s (%s old)" % (
        source, when, origin, age_label((now - observed).total_seconds()))


def _source_label(source):
    """The short label of where a reading came from."""
    return SOURCE_CACHE if source == SOURCE_CACHE else SOURCE_EXPORT


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


def _usage_cache_line(report, config, now):
    """Whether the usage cache reader works, and what it found."""
    if not config.get("usage_cache"):
        return "Usage cache: off (tick 'Read Claude Code's local usage cache' in Edit)"
    cache = report.get("usage_cache")
    if not isinstance(cache, dict):
        return "Usage cache: UNAVAILABLE - no reading yet"
    if not cache.get("available"):
        return "Usage cache: UNAVAILABLE - %s" % ascii_label(
            cache.get("message") or cache.get("reason") or "unknown reason", 160)
    fetched = cache.get("fetched_at")
    if not fetched:
        return "Usage cache: UNAVAILABLE - no reading yet"
    when = timestamp(fetched).astimezone().strftime("%Y-%m-%d %H:%M")
    age = age_label((now - timestamp(fetched)).total_seconds())
    path = ascii_label(cache.get("path") or "unknown path", 160)
    return "Usage cache: ok - fetched %s (%s old), %s" % (when, age, path)


def _usage_cache_refresh_line(report, config, now):
    """How often Claude Code refreshed its usage cache, as far as we saw it.

    The record is the daemon's own: it starts empty on every daemon start, so
    the line says when the watching began instead of pretending to know Claude
    Code's schedule. The gap is the median of the observed gaps and is only
    shown with at least three refreshes, because two do not make a rate.
    """
    if not config.get("usage_cache"):
        return None
    cache = report.get("usage_cache")
    if not isinstance(cache, dict):
        return None
    refreshes = cache.get("refreshes")
    if not isinstance(refreshes, list):
        return None
    since = _local_stamp(cache.get("since"))
    moments = []
    for value in refreshes:
        try:
            moments.append(timestamp(value))
        except (TypeError, ValueError):
            continue
    recent = [moment for moment in moments
              if (now - moment).total_seconds() <= 24 * 3600]
    if not recent:
        return "Usage cache refreshes seen: none yet since %s" % since
    newest = max(recent)
    parts = ["last %s ago" % age_label((now - newest).total_seconds())]
    gap = median_gap(recent)
    if len(recent) >= 3 and gap is not None:
        parts.append("usually every %s" % age_label(gap))
    return ("Usage cache refreshes seen: %d in the last 24 h (%s; since %s)"
            % (len(recent), ", ".join(parts), since))


def median_gap(moments):
    """The median gap between consecutive moments, in seconds.

    An even number of gaps is the mean of the two middle ones. None means
    there is no gap to measure.
    """
    ordered = sorted(moments)
    gaps = [(later - earlier).total_seconds()
            for earlier, later in zip(ordered, ordered[1:])]
    if not gaps:
        return None
    middle = len(gaps) // 2
    if len(gaps) % 2:
        return gaps[middle]
    return (gaps[middle - 1] + gaps[middle]) / 2.0


def _local_stamp(value):
    """A local timestamp for the admin page, or "unknown"."""
    if not value:
        return "unknown"
    try:
        return timestamp(value).astimezone().strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError):
        return "unknown"


def _credit_line(report):
    """The included credit the usage cache reports, when it reports one."""
    cache = report.get("usage_cache")
    if not isinstance(cache, dict):
        return None
    credit = cache.get("credit")
    if not isinstance(credit, dict):
        return None
    limit = credit.get("limit")
    used = credit.get("used")
    if type(limit) not in (int, float) or type(used) not in (int, float):
        return None
    expires = ""
    resets_at = credit.get("resets_at")
    if resets_at:
        try:
            expires = ", expires %s" % timestamp(resets_at).strftime("%Y-%m-%d")
        except (TypeError, ValueError):
            expires = ""
    return "Credit: $%s of $%s left%s" % (_money(limit - used), _money(limit), expires)


def _money(value):
    """A dollar amount without noise: 100, 12.50."""
    if float(value) == int(value):
        return "%d" % int(value)
    return "%.2f" % value


def _status_note_line(report, config):
    """The freshness hint, labelled so it never reads as a measurement."""
    hint = report.get("hint")
    if not isinstance(hint, str) or not hint.strip():
        return None
    line = "Status note: %s" % hint
    if config.get("usage_cache"):
        # The cache refreshes when /usage is opened: that is the way to fix it.
        line += " (opening /usage in Claude Code refreshes the cache)"
    return line


def quota_window(window_id, label, percent, reset, observed, period_minutes=None,
                 state="available", used_amount=None, limit_amount=None, currency=None,
                 source=None):
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
    if source is not None:
        # The label says where the reading came from; it is never a value.
        if (not isinstance(source, str) or not source or len(source) > 32 or
                not source.isascii()):
            raise ValueError("quota source must be a short ASCII label")
        result["source"] = source
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
        activity["state"] = "stale" if (activity.get("error") or result.get("error") or
            (now - timestamp(activity["observed_at"])).total_seconds() > result["stale_after_seconds"]) else "available"
    balance = result.get("cloud_balance")
    if balance:
        balance["state"] = "stale" if (result.get("error") or
            (now - timestamp(balance["observed_at"])).total_seconds() > result["stale_after_seconds"] or
            (balance.get("period_until") and timestamp(balance["period_until"]) <= now)) else "available"
    for window in result["windows"]:
        if window["state"] not in ("available", "unlimited"):
            continue
        observed = timestamp(window["observed_at"])
        reset = timestamp(window["resets_at"]) if window["resets_at"] else None
        stale_after = window_stale_after(result, window)
        if result.get("error") or (now - observed).total_seconds() > stale_after:
            window["state"] = "stale"
        if reset and reset <= now:
            window["state"] = "stale"
            window["reason"] = "awaiting provider reading after reset"
    if "collector_seen_at" in result:
        # Built here, not at collect time, so the hint ages between polls.
        result["hint"] = claude_hint(result, now)
        result.pop("last_update", None)
        measured = [w for w in result["windows"] if w.get("used_percent") is not None]
        if measured:
            observed = max(timestamp(w["observed_at"]) for w in measured)
            result["last_update"] = {
                "age_seconds": max(0, int((now - observed).total_seconds())),
                "in_use": claude_in_use(result, now),
            }
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
