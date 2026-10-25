"""Read-only cloud balances and separately dated rolling activity."""

from copy import deepcopy
import math
import time

from ..quotas import quota_report, quota_window, timestamp
from ..models import iso, now_utc
from ..http_client import get_json, HttpError, HttpUnreachable
from . import register
from ._quota_http import QuotaHttpSource


@register
class OllamaCloudSource(QuotaHttpSource):
    id = "ollama_cloud"
    name = "Ollama Cloud"
    usage_url = "https://ollama.com/api/usage"
    balance_url = "https://ollama.com/api/balance"
    provider = "ollama"
    product = "Cloud"
    windows = (("monthly_credits", "Monthly credits", None),)
    config_fields = tuple(dict(field, minimum=60) if field["key"] == "refresh_seconds"
                          else field for field in QuotaHttpSource.config_fields)

    def __init__(self, config=None):
        super().__init__(config)
        self._activity = None

    @staticmethod
    def _amount(value):
        if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
            raise ValueError("invalid cloud balance")
        return value

    def parse_balance(self, data, observed):
        if not isinstance(data, dict) or not isinstance(data.get("included"), dict):
            raise ValueError("unrecognized cloud balance response")
        included = data["included"]
        windows = []
        balance = {"observed_at": iso(observed)}
        if "allowance_usd" in included:
            allowance = self._amount(included.get("allowance_usd"))
            remaining = self._amount(included.get("balance_usd"))
            if remaining > allowance:
                raise ValueError("cloud balance exceeds allowance")
            period = included.get("period")
            if not isinstance(period, dict):
                raise ValueError("missing cloud billing period")
            start, end = timestamp(period.get("from")), timestamp(period.get("until"))
            if end <= start:
                raise ValueError("invalid cloud billing period")
            used = allowance - remaining
            windows.append(quota_window("monthly_credits", "Monthly credits",
                           100 * used / allowance if allowance else None, end, observed,
                           used_amount=used, limit_amount=allowance, currency="USD"))
            balance.update(included_remaining_usd=remaining, period_from=iso(start),
                           period_until=iso(end))
        else:
            for key, label, minutes in (("session", "Session", None),
                                        ("weekly", "Weekly", 10080)):
                if key not in included:
                    continue
                limit = included[key]
                if not isinstance(limit, dict):
                    raise ValueError("invalid cloud legacy limit")
                remaining = self._amount(limit.get("remaining_percent"))
                if remaining > 100:
                    raise ValueError("invalid cloud legacy percentage")
                reset = timestamp(limit.get("resets_at"))
                windows.append(quota_window(key, label, 100 - remaining, reset,
                                             observed, minutes))
        if not windows:
            raise ValueError("cloud balance has no included allowance or limits")
        purchased = data.get("purchased")
        if purchased is not None:
            if not isinstance(purchased, dict):
                raise ValueError("invalid purchased cloud balance")
            balance["purchased_remaining_usd"] = self._amount(purchased.get("balance_usd"))
        # Included credit exhaustion does not block usage backed by purchased funds.
        if balance.get("purchased_remaining_usd", 0) > 0:
            for window in windows:
                if window["id"] == "monthly_credits":
                    window["exhaustion_blocks"] = False
        report = quota_report(self.provider, self.product, windows,
                              max(180, self.config.get("refresh_seconds", 60) * 3))
        report["cloud_balance"] = balance
        return report, any(w.get("exhaustion_blocks", True) and
                           w["used_percent"] is not None and w["used_percent"] >= 100
                           for w in windows)

    def _read(self, url):
        return get_json(url, timeout=self.config.get("timeout", 10),
                        headers={"Authorization": "Bearer " + self.api_key,
                                 "Accept": "application/json", "User-Agent": "presto-ai-dash/0.1"},
                        allow_redirects=False)

    def _failure(self, exc, endpoint):
        if isinstance(exc, HttpError):
            if exc.status == 429:
                try:
                    seconds = float(exc.retry_after)
                    if math.isfinite(seconds) and seconds > 0:
                        self._next_probe = max(self._next_probe, time.monotonic() + seconds)
                except (TypeError, ValueError):
                    pass
            return ("API key rejected" if exc.status == 401 else
                    "Cloud access denied" if exc.status == 403 else
                    "%s server returned HTTP %d" % (endpoint, exc.status))
        return "%s unavailable; network or reporting schema changed" % endpoint

    def collect(self):
        now = time.monotonic()
        if self._last_snapshot is not None and now < self._next_probe:
            return deepcopy(self._last_snapshot)
        # Existing configurations may still contain a 30-second interval.
        self._next_probe = now + max(60, self.config.get("refresh_seconds", 60))
        error, activity_error, limited = None, None, False
        skip_activity = False
        if not self.api_key:
            error = "Add an Ollama Cloud API key in source configuration"
            skip_activity = True
        else:
            try:
                self._last_quota, limited = self.parse_balance(self._read(self.balance_url), now_utc())
            except (HttpError, HttpUnreachable, ValueError) as exc:
                error = self._failure(exc, "Balance")
                skip_activity = isinstance(exc, HttpError) and exc.status in (401, 403, 429)
        if self.api_key and not skip_activity:
            try:
                report, _ = self.parse(self._read(self.usage_url + "?range=7d"), now_utc())
                self._activity = report["activity"]
            except (HttpError, HttpUnreachable, ValueError) as exc:
                activity_error = self._failure(exc, "Activity")
        elif skip_activity:
            activity_error = "Activity refresh deferred"
        report = deepcopy(self._last_quota) if self._last_quota else self.empty_report()
        report["error"] = error
        if self._activity:
            report["activity"] = deepcopy(self._activity)
            report["activity"]["error"] = activity_error
        if activity_error:
            report["note"] = activity_error
        status = "offline" if error else "rate_limited" if limited else "ok"
        self._last_snapshot = self.snapshot(status, metrics={"quota": report}, error=error)
        return deepcopy(self._last_snapshot)

    def parse(self, data, observed):
        if not isinstance(data, dict) or not isinstance(data.get("totals"), dict):
            raise ValueError("unrecognized cloud activity response")
        start, end = timestamp(data.get("from")), timestamp(data.get("until"))
        if end <= start:
            raise ValueError("invalid cloud activity range")
        totals = data["totals"]
        activity = {"from": iso(start), "until": iso(end), "observed_at": iso(observed)}
        if data.get("range") in ("24h", "7d", "30d"):
            activity["range"] = data["range"]
        for key in ("request_count", "usage_usd", "input_tokens", "cached_input_tokens", "output_tokens"):
            value = totals.get(key)
            # Legacy requests report counts without monetary/token measurements.
            if key != "request_count" and value is None:
                activity[key] = None
                continue
            if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                raise ValueError("invalid cloud activity measurement")
            if key != "usage_usd" and type(value) is not int:
                raise ValueError("invalid cloud activity count")
            activity[key] = value
        report = quota_report(self.provider, self.product,
                              [quota_window("monthly_credits", "Monthly credits", None, None,
                                            observed, state="unavailable")],
                              max(180, self.config.get("refresh_seconds", 60) * 3))
        report["activity"] = activity
        report["note"] = "Rolling activity is separate from the monthly credit balance."
        return report, False
