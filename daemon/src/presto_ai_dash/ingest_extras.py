"""The optional readings a collector may push beside quota windows.

A Claude collector can carry the activity summary and the usage-cache reading
that the daemon would otherwise read from files on its own machine; a Codex
collector carries the banked reset credits and the credit balance. Each is
checked here against a fixed list of fields and size limits, and a clean copy
is returned: whatever else was in the object never reaches the database.

The daemon owns time, with one honest exception: a usage-cache reading keeps
the moment Claude Code fetched it, because that reading may be hours old and
stamping it with the receive time would present old numbers as new. That
moment is capped at the receive time and refused when it is older than the
reader itself would use (seven days) or in the future.
"""

from datetime import datetime, timedelta, timezone
import math
import re

from .collectors.claude_usage_cache import MAX_REFRESH_RECORDS
from .collectors.codex_report import MAX_BALANCE_CHARS, MAX_RESET_COUNT, MAX_RESET_EXPIRIES

MAX_REASON_CHARS = 120
MAX_MODEL_CHARS = 64
MAX_ENTRYPOINT_CHARS = 16
MAX_COUNT = 10 ** 9
MAX_TOKENS = 10 ** 13
MAX_CREDIT_DOLLARS = 10 ** 7
MAX_CACHE_WINDOWS = 4
# A reading this far ahead of the daemon's clock is clock skew; further is not a reading.
FUTURE_TOLERANCE = timedelta(minutes=5)
MAX_FETCH_AGE = timedelta(days=7)
MAX_EXPIRY_DAYS = 3660

ACTIVITY_KEYS = frozenset(("available", "complete", "reason", "last_request_at", "last_model",
                           "last_entrypoint", "requests_5h", "tokens_5h", "tokens_7d"))
USAGE_CACHE_KEYS = frozenset(("available", "reason", "fetched_at", "windows", "credit",
                              "account_changed", "refreshes", "since"))
CREDIT_KEYS = frozenset(("limit", "used", "currency", "resets_at"))
RESET_CREDIT_KEYS = frozenset(("available", "expires_at"))
CREDITS_KEYS = frozenset(("balance", "unlimited"))

_BALANCE = re.compile(r"-?[0-9]{1,15}(\.[0-9]{1,15})?\Z")


def _text(value, limit, what, allow_none=False):
    if value is None and allow_none:
        return None
    if (not isinstance(value, str) or not 0 < len(value) <= limit
            or not all(" " <= char <= "~" for char in value)):
        raise ValueError("%s must be 1-%d printable ASCII characters" % (what, limit))
    return value


def _flag(value, what):
    if type(value) is not bool:
        raise ValueError("%s must be true or false" % what)
    return value


def _count(value, limit, what):
    if type(value) is not int or not 0 <= value <= limit:
        raise ValueError("%s must be a whole number from 0 to %d" % (what, limit))
    return value


def _moment(value, what):
    if not isinstance(value, str) or not value or len(value) > 40:
        raise ValueError("%s must be an ISO-8601 time" % what)
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("%s must be an ISO-8601 time" % what) from None
    if moment.tzinfo is None:
        raise ValueError("%s must include a UTC offset" % what)
    return moment.astimezone(timezone.utc)


def _exact(value, keys, what):
    if not isinstance(value, dict):
        raise ValueError("%s must be an object" % what)
    unknown = set(value) - keys
    if unknown:
        raise ValueError("unknown %s key: %s" % (what, ", ".join(sorted(map(str, unknown)))))


def clean_activity(value, now):
    """The activity summary, with only the fields the local reader returns."""
    _exact(value, ACTIVITY_KEYS, "activity")
    available = _flag(value.get("available"), "activity.available")
    result = {"available": available,
              "complete": _flag(value.get("complete"), "activity.complete")}
    if "reason" in value:
        result["reason"] = _text(value["reason"], MAX_REASON_CHARS, "activity.reason")
    last = value.get("last_request_at")
    if last is not None:
        moment = _moment(last, "activity.last_request_at")
        if moment > now + FUTURE_TOLERANCE:
            raise ValueError("activity.last_request_at is in the future")
        last = min(moment, now).isoformat()
    result["last_request_at"] = last
    result["last_model"] = _text(value.get("last_model"), MAX_MODEL_CHARS,
                                 "activity.last_model", allow_none=True)
    result["last_entrypoint"] = _text(value.get("last_entrypoint", "?"), MAX_ENTRYPOINT_CHARS,
                                      "activity.last_entrypoint")
    result["requests_5h"] = _count(value.get("requests_5h"), MAX_COUNT, "activity.requests_5h")
    result["tokens_5h"] = _count(value.get("tokens_5h"), MAX_TOKENS, "activity.tokens_5h")
    result["tokens_7d"] = _count(value.get("tokens_7d"), MAX_TOKENS, "activity.tokens_7d")
    return result


def _money(value, what):
    if (type(value) not in (int, float) or not math.isfinite(value)
            or not 0 <= value <= MAX_CREDIT_DOLLARS):
        raise ValueError("%s must be a number from 0 to %d" % (what, MAX_CREDIT_DOLLARS))
    return value


def clean_usage_cache(value, now, check_window):
    """The usage-cache reading: windows, credit and when Claude Code fetched them.

    ``check_window`` is the envelope's own window check, so a window here obeys
    the same rules as one in ``windows``.
    """
    _exact(value, USAGE_CACHE_KEYS, "usage_cache")
    available = _flag(value.get("available"), "usage_cache.available")
    result = {"available": available,
              "reason": _text(value.get("reason"), MAX_REASON_CHARS, "usage_cache.reason",
                              allow_none=True)}
    fetched = value.get("fetched_at")
    if available:
        moment = _moment(fetched, "usage_cache.fetched_at")
        if moment > now + FUTURE_TOLERANCE:
            raise ValueError("usage_cache.fetched_at is in the future")
        moment = min(moment, now)
        if now - moment > MAX_FETCH_AGE:
            raise ValueError("usage_cache.fetched_at is more than 7 days old")
        fetched = moment.isoformat()
    elif fetched is not None:
        raise ValueError("an unavailable usage cache has no fetched_at")
    result["fetched_at"] = fetched
    windows = value.get("windows", [])
    if not isinstance(windows, list) or len(windows) > MAX_CACHE_WINDOWS:
        raise ValueError("usage_cache.windows must be a short list")
    seen = set()
    for window in windows:
        check_window(window, seen, ("five_hour", "seven_day"), now)
    result["windows"] = [dict(window) for window in windows]
    credit = value.get("credit")
    if credit is not None:
        _exact(credit, CREDIT_KEYS, "usage_cache.credit")
        if credit.get("currency") != "USD" or set(credit) != CREDIT_KEYS:
            raise ValueError("usage_cache.credit must have limit, used, currency USD and resets_at")
        reset = _moment(credit["resets_at"], "usage_cache.credit.resets_at")
        if abs(reset - now) > timedelta(days=MAX_EXPIRY_DAYS):
            raise ValueError("usage_cache.credit.resets_at is out of range")
        credit = {"limit": _money(credit["limit"], "credit limit"),
                  "used": _money(credit["used"], "credit used"), "currency": "USD",
                  "resets_at": reset.isoformat()}
    result["credit"] = credit
    result["account_changed"] = _flag(value.get("account_changed", False),
                                      "usage_cache.account_changed")
    refreshes = value.get("refreshes", [])
    if not isinstance(refreshes, list) or len(refreshes) > MAX_REFRESH_RECORDS:
        raise ValueError("usage_cache.refreshes must be a short list")
    result["refreshes"] = [_moment(item, "usage_cache.refreshes").isoformat()
                           for item in refreshes]
    since = value.get("since")
    result["since"] = None if since is None else _moment(since, "usage_cache.since").isoformat()
    return result


def clean_reset_credits(value, now):
    """Banked reset credits: the provider's count and the expiry of each available one."""
    _exact(value, RESET_CREDIT_KEYS, "reset_credits")
    available = value.get("available")
    if available is not None:
        available = _count(available, MAX_RESET_COUNT, "reset_credits.available")
    expiries = value.get("expires_at", [])
    if not isinstance(expiries, list) or len(expiries) > MAX_RESET_EXPIRIES:
        raise ValueError("reset_credits.expires_at must be a short list")
    cleaned = []
    for item in expiries:
        moment = _moment(item, "reset_credits.expires_at")
        if abs(moment - now) > timedelta(days=MAX_EXPIRY_DAYS):
            raise ValueError("a reset credit expiry is out of range")
        cleaned.append(moment.isoformat())
    return {"available": available, "expires_at": cleaned}


def clean_credits(value):
    """The credit balance: a short decimal string and whether it is unlimited."""
    _exact(value, CREDITS_KEYS, "credits")
    balance = value.get("balance")
    if balance is not None:
        if (not isinstance(balance, str) or len(balance) > MAX_BALANCE_CHARS
                or not _BALANCE.match(balance)):
            raise ValueError("credits.balance must be a short decimal number")
    unlimited = value.get("unlimited")
    if unlimited is not None:
        _flag(unlimited, "credits.unlimited")
    return {"balance": balance, "unlimited": unlimited}
