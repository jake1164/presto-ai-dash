"""The time zone the daemon runs in, including the ``TZ`` variable.

A container starts in UTC. Quiet hours and the weekly-reset estimate use the
process's local time, so a container that should behave like the user's own
machine sets ``TZ`` (for example ``Europe/Berlin``). This module resolves
that variable into a zone the daemon can use on every platform: a fixed
offset for the POSIX spellings, and the IANA database for a named zone.

Nothing here raises. An unknown ``TZ`` is logged and the system's own zone is
used instead, so a typo can never stop the daemon.
"""

import logging
import os
import re
from datetime import datetime, timedelta, timezone

log = logging.getLogger(__name__)

UTC = timezone.utc

# The POSIX spellings of the whole-hour zones have the opposite sign: on
# POSIX, "Etc/GMT+3" is three hours *behind* UTC.
_ETC_PATTERN = re.compile(r"^etc/gmt([+-])(\d{1,2})$")
# "UTC+2", "GMT-05:30", "+03:00".
_OFFSET_PATTERN = re.compile(r"^(?:utc|gmt)?([+-])(\d{1,2})(?::?(\d{2}))?$")

# The whole-world UTC spellings.
_UTC_NAMES = frozenset(("utc", "gmt", "z", "universal", "zulu", "etc/utc", "etc/gmt",
                         "etc/universal", "etc/zulu", "greenwich"))


def _fixed_offset(text):
    """A fixed-offset zone for the POSIX spellings, or None."""
    name = text.strip()
    if not name:
        return None
    lowered = name.lower()
    if lowered in _UTC_NAMES:
        return timezone.utc
    match = _ETC_PATTERN.match(lowered)
    if match:
        hours = int(match.group(2))
        if hours > 14:
            return None
        minutes = -hours * 60 if match.group(1) == "+" else hours * 60
        return timezone(timedelta(minutes=minutes), "UTC%s%02d:00" %
                        ("+" if minutes >= 0 else "-", abs(minutes) // 60))
    match = _OFFSET_PATTERN.match(lowered)
    if match:
        hours = int(match.group(2))
        minutes = int(match.group(3) or 0)
        if hours > 24 or minutes > 59:
            return None
        total = hours * 60 + minutes
        if match.group(1) == "-":
            total = -total
        label = "UTC%s%02d:%02d" % ("+" if total >= 0 else "-", abs(total) // 60,
                                    abs(total) % 60)
        return timezone(timedelta(minutes=total), label)
    return None


def _named_zone(text):
    """An IANA zone, or None when the name is unknown here."""
    try:
        from zoneinfo import ZoneInfo
    except ImportError:  # pragma: no cover - Python 3.8 and older
        return None
    try:
        return ZoneInfo(text)
    except Exception:  # noqa: BLE001 - an unknown name is not a crash
        return None


def resolve_zone(environ=None, now=None):
    """The zone the process should use: ``TZ`` when it is set, else the system's."""
    names = os.environ if environ is None else environ
    text = (names.get("TZ") or "").strip()
    if text:
        zone = _fixed_offset(text)
        if zone is not None:
            return zone
        zone = _named_zone(text)
        if zone is not None:
            return zone
        log.warning("TZ=%s is not a time zone this system knows; the system zone "
                    "is used instead", text)
    # The system's own zone, as a fixed offset for the moment asked about.
    moment = now or datetime.now()
    try:
        return moment.astimezone().tzinfo or UTC
    except (OSError, ValueError):  # pragma: no cover - defensive
        return UTC


def local_now(environ=None):
    """The current local time, in the process's zone."""
    return datetime.now(resolve_zone(environ))


def zone_label(now=None, environ=None):
    """A short readable label for the zone: ``Europe/Berlin (UTC+02:00)``.

    With ``TZ`` set, the name the operator configured is shown, so the admin
    page says which zone the quiet hours and the reset times use.
    """
    names = os.environ if environ is None else environ
    text = (names.get("TZ") or "").strip()
    zone = resolve_zone(names, now=now)
    moment = (now or datetime.now(UTC)).astimezone(zone)
    offset = moment.utcoffset() or timedelta(0)
    minutes = int(offset.total_seconds() // 60)
    sign = "+" if minutes >= 0 else "-"
    minutes = abs(minutes)
    zone_name = moment.tzname() or "unknown"
    if text:
        # A fixed offset already says everything it needs to say.
        if re.match(r"^UTC[+-]\d{2}:\d{2}$", zone_name):
            return zone_name
        return "%s (UTC%s%02d:%02d)" % (text, sign, minutes // 60, minutes % 60)
    return "%s (UTC%s%02d:%02d)" % (zone_name, sign, minutes // 60, minutes % 60)
