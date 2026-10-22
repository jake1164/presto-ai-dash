"""What Codex's rate-limit answer says, as plain quota windows: stdlib only, Python 3.8+.

The daemon's Codex source and the one-file collector both read Codex's
``account/rateLimits/read`` answer. This module is the one copy of the rules
for turning it into quota windows, banked reset credits and a credit balance,
so a reading pushed from another machine means exactly what a local one does.

Only the quota numbers, the reset times, the count and expiry of reset credits
and the credit balance are taken. Nothing about the account is read.
"""

from datetime import datetime, timezone
import decimal
import math
import re

# Banked reset credits (Phase 9). The provider's count is authoritative; the
# rows only supply expiries. Missing or malformed data means "not reported".
MAX_RESET_COUNT = 99
MAX_RESET_EXPIRIES = 8
# A Unix timestamp outside this range is not a real expiry.
EARLIEST_EXPIRY = 1000000000  # 2001-09-09
LATEST_EXPIRY = 4102444800    # 2100-01-01
# The credit balance is a short decimal string on the detail card.
MAX_BALANCE_CHARS = 16
MAX_BUCKETS = 32
MAX_BUCKET_ID_CHARS = 128
# A window id is bucket and slot, "codex:primary"; the envelope allows 32.
MAX_WINDOW_ID_CHARS = 32


def period_label(minutes):
    if minutes == 10080:
        return "Weekly"
    if minutes == 1440:
        return "Daily"
    if minutes is None:
        return "Reported window"
    if minutes % 1440 == 0:
        return "%dd" % (minutes // 1440)
    if minutes % 60 == 0:
        return "%dh" % (minutes // 60)
    return "%dm" % minutes


def balance(value):
    """A credit balance as a plain decimal string, or None when unusable.

    The provider reports it in exponent notation ("0E-10"), which must never
    reach the screen: Decimal turns it into "0". At most 16 characters.
    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    try:
        amount = decimal.Decimal(text)
    except (decimal.InvalidOperation, ValueError, ArithmeticError):
        return None
    if not amount.is_finite():
        return None
    if amount == 0:
        return "0"
    try:
        amount = amount.normalize()
        if amount.as_tuple().exponent > 0:
            amount = amount.quantize(decimal.Decimal(1))
    except (decimal.InvalidOperation, ArithmeticError):
        return None
    result = format(amount, "f")
    if len(result) > MAX_BALANCE_CHARS:
        return None
    return result


def reset_credits(summary):
    """The banked reset credits: a count and the expiries of the available ones.

    ``available`` is the provider's own count when it is a real integer in
    range, and None otherwise: an unknown count is never shown as zero. Only
    rows whose status is exactly "available" supply an expiry.
    """
    available, expiries = None, []
    if isinstance(summary, dict):
        count = summary.get("availableCount")
        if type(count) is int and 0 <= count <= MAX_RESET_COUNT:
            available = count
        rows = summary.get("credits")
        if isinstance(rows, list):
            seen = set()
            for row in rows:
                if not isinstance(row, dict) or row.get("status") != "available":
                    continue
                expires = row.get("expiresAt")
                if type(expires) is not int:
                    continue
                if expires < EARLIEST_EXPIRY or expires > LATEST_EXPIRY:
                    continue
                moment = datetime.fromtimestamp(expires, timezone.utc).isoformat()
                if moment in seen:
                    continue
                seen.add(moment)
                expiries.append(moment)
            expiries.sort()
            expiries = expiries[:MAX_RESET_EXPIRIES]
    return {"available": available, "expires_at": expiries}


def credits(bucket):
    """The bucket's credit balance, or None when the provider reports none."""
    amount, unlimited = None, None
    if isinstance(bucket, dict):
        reported = bucket.get("credits")
        if isinstance(reported, dict):
            amount = balance(reported.get("balance"))
            if type(reported.get("unlimited")) is bool:
                unlimited = reported["unlimited"]
    return {"balance": amount, "unlimited": unlimited}


def buckets_of(data):
    """The quota buckets of an answer, and the legacy single bucket when that is all there is.

    Returns ``(buckets, legacy_bucket)``. Raises ValueError for an answer that
    holds no usable bucket.
    """
    if not isinstance(data, dict):
        raise ValueError("invalid reporting response")
    legacy_bucket = None
    if "rateLimitsByLimitId" in data and data["rateLimitsByLimitId"] is not None:
        buckets = data["rateLimitsByLimitId"]
        if not isinstance(buckets, dict) or not buckets:
            raise ValueError("no quota buckets reported")
    else:
        legacy = data.get("rateLimits")
        if not isinstance(legacy, dict):
            raise ValueError("no quota buckets reported")
        legacy_bucket = legacy
        buckets = {legacy.get("limitId") or "codex": legacy}
    if len(buckets) > MAX_BUCKETS:
        raise ValueError("too many quota buckets")
    return buckets, legacy_bucket


def credit_bucket(buckets, legacy_bucket):
    """The bucket whose credit balance is shown: ``codex``, or the legacy one."""
    chosen = buckets.get("codex") if isinstance(buckets, dict) else None
    return legacy_bucket if chosen is None else chosen


_ID_CHARS = re.compile(r"[^a-z0-9_.]")


def safe_window_id(bucket_id, slot):
    """``bucket:slot`` in the characters and length a window id may have."""
    stem = _ID_CHARS.sub("_", bucket_id.lower())[:MAX_WINDOW_ID_CHARS - len(slot) - 1]
    return stem + ":" + slot


def rate_limit_windows(data):
    """The quota windows of one answer, in the shape the ingest envelope carries.

    Returns ``(windows, limited, reset_credits, credits)``. A window has
    ``id``, ``label``, ``period_minutes``, ``used_percent``, ``resets_at`` (an
    ISO time or None) and ``state``; there is no time of observation, because
    whoever receives the reading owns the clock. Raises ValueError for an
    answer whose shape is not understood.
    """
    buckets, legacy = buckets_of(data)
    windows, limited, seen = [], False, set()
    for bucket_id, bucket in buckets.items():
        if (not isinstance(bucket_id, str) or not bucket_id
                or len(bucket_id) > MAX_BUCKET_ID_CHARS or not isinstance(bucket, dict)):
            raise ValueError("invalid quota bucket")
        prefix = (bucket_id + " ") if len(buckets) > 1 else ""
        for slot in ("primary", "secondary"):
            window_id = safe_window_id(bucket_id, slot)
            if window_id in seen:
                raise ValueError("duplicate quota window identity")
            seen.add(window_id)
            value = bucket.get(slot)
            if value is None:
                windows.append({"id": window_id, "label": _label(prefix + slot.title()),
                                "period_minutes": None, "used_percent": None,
                                "resets_at": None, "state": "unavailable"})
                continue
            if not isinstance(value, dict):
                raise ValueError("invalid quota window")
            minutes = value.get("windowDurationMins")
            if minutes is not None and (type(minutes) is not int or minutes <= 0):
                raise ValueError("invalid quota period")
            reset = value.get("resetsAt")
            if reset is not None:
                if type(reset) not in (int, float):
                    raise ValueError("invalid reset")
                reset = datetime.fromtimestamp(reset, timezone.utc).isoformat()
            percent = value.get("usedPercent")
            if percent is not None and (type(percent) not in (int, float)
                                        or not math.isfinite(percent) or percent < 0):
                raise ValueError("invalid quota percentage")
            windows.append({"id": window_id, "label": _label(prefix + period_label(minutes)),
                            "period_minutes": minutes, "used_percent": percent,
                            "resets_at": reset,
                            "state": "available" if percent is not None else "unavailable"})
            limited = limited or (percent is not None and percent >= 100)
        limited = limited or bucket.get("rateLimitReachedType") is not None
    return (windows, limited, reset_credits(data.get("rateLimitResetCredits")),
            credits(credit_bucket(buckets, legacy)))


def _label(text):
    """A short printable-ASCII window label."""
    cleaned = "".join(char for char in text if " " <= char <= "~").strip()
    return (cleaned or "Reported window")[:32]
