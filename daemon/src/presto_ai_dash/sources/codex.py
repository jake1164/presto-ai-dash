"""Codex subscription allowances from supported local app-server reporting."""

from copy import deepcopy
from datetime import datetime, timezone
import time

from ..collectors.codex_report import (MAX_BALANCE_CHARS, MAX_RESET_COUNT,
                                       MAX_RESET_EXPIRIES, balance, credits, period_label,
                                       reset_credits)
from ..models import now_utc
from ..quotas import quota_report, quota_window
from . import Source, register
from ._codex_rpc import (CodexReportingError, executable_setting_problem, find_executable,
                         read_subscription)

# The rules for reading Codex's answer are shared with the one-file collector.
_balance = balance
_reset_credits = reset_credits
_credits = credits


# A pushed reading this recent is used instead of starting a local app-server.
PUSHED_FRESH_SECONDS = 300


@register
class CodexSource(Source):
    id = "codex"
    name = "OpenAI / Codex"
    provider = "openai"
    product = "Codex"
    stale_after_seconds = 180
    report_note = "Codex rate limits, read on another computer and pushed by its collector."
    # A collector on the machine where Codex is signed in may push the same
    # reading this source takes itself: the daemon can then run in a container,
    # which has neither Codex nor a login.
    accepts_push = True
    push_extras = frozenset(("reset_credits", "credits"))
    # Two windows for each quota bucket Codex reports; a body must still fit the 16 KiB limit.
    push_max_windows = 24
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

    def attach_storage(self, storage):
        self.storage = storage

    def _pushed(self):
        """The newest pushed reading and whether it is recent enough to use, or (None, False)."""
        observation = (self.storage.observation(self.id)
                       if getattr(self, "storage", None) is not None else None)
        if not observation or not observation.get("report"):
            return None, False
        report = deepcopy(observation["report"])
        seen = observation.get("collector_seen_at")
        fresh = False
        if seen:
            age = (now_utc() - datetime.fromisoformat(seen)).total_seconds()
            fresh = age <= PUSHED_FRESH_SECONDS
        report["error"] = None
        return report, fresh

    def _from_push(self, report):
        limited = any(window.get("used_percent") is not None and window["used_percent"] >= 100
                      for window in report.get("windows", []))
        return self.snapshot("rate_limited" if limited else "ok", metrics={"quota": report})

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
        pushed, fresh = self._pushed()
        if fresh:
            self._last_snapshot = self._from_push(pushed)
            return deepcopy(self._last_snapshot)
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
        if error and pushed is not None:
            # This machine cannot read Codex (a container, say) and the collector
            # has gone quiet: its last reading is shown, and ages as stale.
            self._last_snapshot = self._from_push(pushed)
            return deepcopy(self._last_snapshot)
        report = deepcopy(self._last_quota) if self._last_quota else quota_report(
            "openai", "Codex", [quota_window("codex:primary", "Subscription usage", None,
                                            None, now_utc(), state="unavailable")])
        report["error"] = error
        self._last_snapshot = self.snapshot(status, metrics={"quota": report}, error=error)
        return deepcopy(self._last_snapshot)
