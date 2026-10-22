"""Codex subscription allowances from supported local app-server reporting."""

from copy import deepcopy
from datetime import datetime, timezone
import decimal
import time

from ..models import iso, now_utc
from ..quotas import quota_report, quota_window
from . import Source, register
from ._codex_rpc import (CodexReportingError, executable_setting_problem, find_executable,
                         read_subscription)

# Banked reset credits (Phase 9). The provider's count is authoritative; the
# rows only supply expiries. Missing or malformed data means "not reported".
MAX_RESET_COUNT = 99
MAX_RESET_EXPIRIES = 8
# A Unix timestamp outside this range is not a real expiry.
EARLIEST_EXPIRY = 1000000000  # 2001-09-09
LATEST_EXPIRY = 4102444800    # 2100-01-01
# The credit balance is a short decimal string on the detail card.
MAX_BALANCE_CHARS = 16


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


def _balance(value):
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


def _reset_credits(summary):
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
                moment = iso(datetime.fromtimestamp(expires, timezone.utc))
                if moment in seen:
                    continue
                seen.add(moment)
                expiries.append(moment)
            expiries.sort()
            expiries = expiries[:MAX_RESET_EXPIRIES]
    return {"available": available, "expires_at": expiries}


def _credits(bucket):
    """The bucket's credit balance, or None when the provider reports none."""
    balance, unlimited = None, None
    if isinstance(bucket, dict):
        reported = bucket.get("credits")
        if isinstance(reported, dict):
            balance = _balance(reported.get("balance"))
            if type(reported.get("unlimited")) is bool:
                unlimited = reported["unlimited"]
    return {"balance": balance, "unlimited": unlimited}


@register
class CodexSource(Source):
    id = "codex"
    name = "OpenAI / Codex"
    config_fields = (
        {"key": "executable", "label": "Codex executable (blank: automatic discovery)",
         "type": "string", "default": ""},
        {"key": "profile_home", "label": "Codex profile directory (blank: existing default login)",
         "type": "string", "default": ""},
        {"key": "timeout", "label": "Reporting timeout (seconds)", "type": "int",
         "default": 15, "minimum": 1, "maximum": 60},
        {"key": "refresh_seconds", "label": "Refresh interval (seconds)", "type": "int",
         "default": 60, "minimum": 30, "maximum": 3600},
    )

    @staticmethod
    def check_settings(settings):
        problem = executable_setting_problem(settings.get("executable", ""))
        if problem:
            raise ValueError(problem)

    def __init__(self, config=None):
        super().__init__(config)
        self._last_quota = None
        self._last_snapshot = None
        self._next_probe = 0

    def parse(self, data, observed):
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
        if len(buckets) > 32:
            raise ValueError("too many quota buckets")
        windows, limited = [], False
        for bucket_id, bucket in buckets.items():
            if not isinstance(bucket_id, str) or not bucket_id or len(bucket_id) > 128 or not isinstance(bucket, dict):
                raise ValueError("invalid quota bucket")
            prefix = (bucket_id + " ") if len(buckets) > 1 else ""
            for slot in ("primary", "secondary"):
                value = bucket.get(slot)
                if value is None:
                    windows.append(quota_window(bucket_id + ":" + slot, prefix + slot.title(),
                                                None, None, observed, state="unavailable"))
                    continue
                if not isinstance(value, dict):
                    raise ValueError("invalid quota window")
                minutes = value.get("windowDurationMins")
                reset = value.get("resetsAt")
                if reset is not None:
                    if type(reset) not in (int, float):
                        raise ValueError("invalid reset")
                    reset = datetime.fromtimestamp(reset, timezone.utc)
                percent = value.get("usedPercent")
                windows.append(quota_window(bucket_id + ":" + slot,
                                            prefix + period_label(minutes), percent, reset,
                                            observed, minutes))
                limited = limited or (percent is not None and percent >= 100)
            limited = limited or bucket.get("rateLimitReachedType") is not None
        report = quota_report("openai", "Codex", windows,
                               max(180, self.config.get("refresh_seconds", 60) * 3))
        # Banked reset credits and the credit balance come from the same
        # read-only call; no extra request is made and nothing is redeemed.
        report["reset_credits"] = _reset_credits(data.get("rateLimitResetCredits"))
        credit_bucket = buckets.get("codex") if isinstance(buckets, dict) else None
        if credit_bucket is None:
            credit_bucket = legacy_bucket
        report["credits"] = _credits(credit_bucket)
        return report, limited

    def collect(self):
        now = time.monotonic()
        if self._last_snapshot is not None and now < self._next_probe:
            return deepcopy(self._last_snapshot)
        self._next_probe = now + self.config.get("refresh_seconds", 60)
        error, status = None, "offline"
        try:
            executable = find_executable(self.config.get("executable", ""))
            data = read_subscription(executable, self.config.get("profile_home", ""),
                                     self.config.get("timeout", 15))
            self._last_quota, limited = self.parse(data, now_utc())
            status = "rate_limited" if limited else "ok"
        except CodexReportingError as exc:
            error = str(exc)
        except (ValueError, TypeError, OverflowError, OSError):
            error = "Codex usage unavailable; reporting schema changed or local process failed"
        report = deepcopy(self._last_quota) if self._last_quota else quota_report(
            "openai", "Codex", [quota_window("codex:primary", "Subscription usage", None,
                                            None, now_utc(), state="unavailable")])
        report["error"] = error
        self._last_snapshot = self.snapshot(status, metrics={"quota": report}, error=error)
        return deepcopy(self._last_snapshot)
