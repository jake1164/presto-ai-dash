"""Alert policy: how a quota reading becomes a user-facing status.

The daemon decides the status; the device only draws it. This module holds
the two decisions that belong to the daemon:

* the percentage -> status mapping, so every provider behaves the same way
  (a window that is nearly used is a warning, one that is about to stop
  work is critical, one that is used up is rate limited), and
* the quiet-hours clock, so the device needs no time zone of its own.

Two rules keep the mapping honest:

* only a fresh, available window alerts. A stale, unavailable or
  already-reset window keeps today's stale presentation and never raises
  an alert, and
* a window that resets within the grace period does not alert either: it
  is about to clear on its own.

The per-source metric thresholds and the trend arrows live in the older
``alerts`` module; this one is about quota windows and the alarm clock.
"""

from .localzone import local_now, resolve_zone
from .models import now_utc, severity
from .quotas import timestamp

# The threshold ladder, least severe first. It is not the card order: at
# 100% a service is rate limited, which is worse than the critical state
# just below it.
THRESHOLD_ORDER = ("ok", "near_limit", "critical", "rate_limited")

DEFAULT_WARNING_PERCENT = 80
DEFAULT_CRITICAL_PERCENT = 95
MIN_PERCENT = 1
MAX_PERCENT = 100

# A window that resets within this many minutes is about to clear, so it
# does not raise a threshold alert.
RESET_GRACE_SECONDS = 30 * 60

# Quiet hours, in the daemon's own local time.
DEFAULT_QUIET_START = "22:00"
DEFAULT_QUIET_END = "07:00"

# The master switch: alerts are on unless the user turns them off.
DEFAULT_MASTER_ENABLED = True


def validate_thresholds(warning_percent, critical_percent):
    """Raise ValueError unless the two percentages can be used.

    Both are whole numbers in 1..100, and the warning must sit below the
    critical one: a window that is both would never be a warning.
    """
    for name, value in (("warning_percent", warning_percent),
                        ("critical_percent", critical_percent)):
        if type(value) is not int or value < MIN_PERCENT or value > MAX_PERCENT:
            raise ValueError("%s must be an integer in range %d..%d" %
                             (name, MIN_PERCENT, MAX_PERCENT))
    if warning_percent >= critical_percent:
        raise ValueError(
            "the warning percent (%d) must be below the critical percent (%d)" %
            (warning_percent, critical_percent))
    return warning_percent, critical_percent


def _pair_is_usable(warning_percent, critical_percent):
    """Whether these two percentages can be used together."""
    try:
        validate_thresholds(warning_percent, critical_percent)
    except ValueError:
        return False
    return True


def resolve_alert_settings(global_settings=None, service_settings=None):
    """One service's effective alert behaviour, resolved from two settings.

    Pure: it reads two dictionaries and returns a dictionary. It never
    logs, writes or reads anything, so the same answer comes out every
    time and it can be tested on its own.

    * the master switch in ``global_settings`` governs every service,
    * the service's own switch in ``service_settings`` governs that one
      service, and a service is alerting only when both are on,
    * a threshold the service set wins over the global one, and a
      threshold it did not set is inherited, and
    * a pair that cannot be used - the warning is not below the
      critical one, which can happen when the global value changed
      under a service that had set its own - falls back to the global
      pair, and the caller is told with ``override_ignored`` so the
      admin page can say that the override was not used.

    The result holds ``enabled`` (the effective answer), the two
    effective percentages, and the flags ``master_enabled``,
    ``service_enabled`` and ``override_ignored``.
    """
    global_settings = global_settings if isinstance(global_settings, dict) else {}
    service_settings = service_settings if isinstance(service_settings, dict) else {}

    master = global_settings.get("enabled", DEFAULT_MASTER_ENABLED)
    if type(master) is not bool:
        master = DEFAULT_MASTER_ENABLED
    service_enabled = service_settings.get("enabled", True)
    if type(service_enabled) is not bool:
        service_enabled = True

    global_warning = global_settings.get("warning_percent", DEFAULT_WARNING_PERCENT)
    global_critical = global_settings.get("critical_percent", DEFAULT_CRITICAL_PERCENT)
    if not _pair_is_usable(global_warning, global_critical):
        global_warning = DEFAULT_WARNING_PERCENT
        global_critical = DEFAULT_CRITICAL_PERCENT

    own_warning = service_settings.get("warning_percent")
    own_critical = service_settings.get("critical_percent")
    warning = global_warning if own_warning is None else own_warning
    critical = global_critical if own_critical is None else own_critical
    override_ignored = False
    if not _pair_is_usable(warning, critical):
        # The pair this service would use is impossible: fall back to the
        # global pair and say that the override was not used.
        warning, critical = global_warning, global_critical
        override_ignored = own_warning is not None or own_critical is not None

    return {
        "enabled": bool(master and service_enabled),
        "master_enabled": master,
        "service_enabled": service_enabled,
        "warning_percent": warning,
        "critical_percent": critical,
        "override_ignored": override_ignored,
    }


def level_for_percent(percent, warning_percent=DEFAULT_WARNING_PERCENT,
                      critical_percent=DEFAULT_CRITICAL_PERCENT):
    """The status one percentage maps to, or None when it is below the warning.

    The mapping is monotonic in the percentage: 100% is rate limited, as it
    always was, so a service that reaches the limit keeps the status the
    provider already gave it.
    """
    if isinstance(percent, bool) or not isinstance(percent, (int, float)):
        return None
    if percent >= MAX_PERCENT:
        return "rate_limited"
    if percent >= critical_percent:
        return "critical"
    if percent >= warning_percent:
        return "near_limit"
    return None


def _reset_seconds(resets_at, now):
    """Seconds until this window resets, or None when that is not known."""
    if not resets_at:
        return None
    try:
        moment = timestamp(resets_at)
    except (ValueError, TypeError):
        return None
    return (moment - now).total_seconds()


def threshold_status(report, warning_percent=DEFAULT_WARNING_PERCENT,
                     critical_percent=DEFAULT_CRITICAL_PERCENT, now=None):
    """The worst threshold status among a report's fresh, available windows.

    Returns None when nothing alerts: no window is fresh and available, or
    every window is below the warning percent, or every window that would
    alert is about to reset.
    """
    if not isinstance(report, dict):
        return None
    windows = report.get("windows")
    if not isinstance(windows, list):
        return None
    now = timestamp(now or now_utc())
    best = None
    for window in windows:
        if not isinstance(window, dict):
            continue
        # Only a fresh, available window alerts. A stale or unavailable
        # window keeps the stale presentation it already has.
        if window.get("state") != "available":
            continue
        percent = window.get("used_percent")
        if isinstance(percent, bool) or not isinstance(percent, (int, float)):
            continue
        seconds = _reset_seconds(window.get("resets_at"), now)
        if seconds is not None and seconds <= RESET_GRACE_SECONDS:
            # The window is about to clear, or has already reset: it must
            # not raise an alert.
            continue
        level = level_for_percent(percent, warning_percent, critical_percent)
        if level is None:
            continue
        if best is None or THRESHOLD_ORDER.index(level) > THRESHOLD_ORDER.index(best):
            best = level
    return best


def apply_threshold(status, level):
    """The worse of the two statuses, for the card and the source row."""
    if level is None:
        return status
    if severity(level) > severity(status):
        return level
    return status


def parse_clock(value):
    """Minutes since midnight for an "HH:MM" string, or None."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    parts = text.split(":")
    if len(parts) != 2:
        return None
    try:
        hour, minute = int(parts[0]), int(parts[1])
    except ValueError:
        return None
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        return None
    return hour * 60 + minute


def format_clock(minutes):
    """An "HH:MM" string for minutes since midnight."""
    minutes = max(0, min(24 * 60 - 1, int(minutes)))
    return "%02d:%02d" % (minutes // 60, minutes % 60)


def is_quiet(now=None, start=DEFAULT_QUIET_START, end=DEFAULT_QUIET_END):
    """True when the daemon's local time is inside the quiet window.

    The window may wrap around midnight: 22:00 to 07:00 is quiet from ten
    in the evening until seven in the morning. The local time is the
    process's own zone, which a container sets with ``TZ``.
    """
    start_minutes = parse_clock(start)
    end_minutes = parse_clock(end)
    if start_minutes is None or end_minutes is None:
        return False
    moment = now if now is not None else local_now()
    if moment.tzinfo is not None:
        moment = moment.astimezone(resolve_zone())
    current = moment.hour * 60 + moment.minute
    if start_minutes <= end_minutes:
        return start_minutes <= current < end_minutes
    return current >= start_minutes or current < end_minutes
