"""Codex subscription allowances from supported local app-server reporting."""

from copy import deepcopy
from datetime import datetime, timezone
import time

from ..models import now_utc
from ..quotas import quota_report, quota_window
from . import Source, register
from ._codex_rpc import CodexReportingError, find_executable, read_subscription


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

    def __init__(self, config=None):
        super().__init__(config)
        self._last_quota = None
        self._last_snapshot = None
        self._next_probe = 0

    def parse(self, data, observed):
        if not isinstance(data, dict):
            raise ValueError("invalid reporting response")
        if "rateLimitsByLimitId" in data and data["rateLimitsByLimitId"] is not None:
            buckets = data["rateLimitsByLimitId"]
            if not isinstance(buckets, dict) or not buckets:
                raise ValueError("no quota buckets reported")
        else:
            legacy = data.get("rateLimits")
            if not isinstance(legacy, dict):
                raise ValueError("no quota buckets reported")
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
        return quota_report("openai", "Codex", windows,
                            max(180, self.config.get("refresh_seconds", 60) * 3)), limited

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
