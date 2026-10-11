"""Shared bounded cadence and failure handling for authenticated cloud reporting."""

from copy import deepcopy
import time

from ..http_client import HttpError, HttpUnreachable, get_json
from ..models import now_utc
from ..quotas import quota_report, quota_window
from . import Source


class QuotaHttpSource(Source):
    secret_field = "api_key"
    config_fields = (
        {"key": "timeout", "label": "Request timeout (seconds)", "type": "int",
         "default": 10, "minimum": 1, "maximum": 60},
        {"key": "refresh_seconds", "label": "Refresh interval (seconds)", "type": "int",
         "default": 60, "minimum": 30, "maximum": 3600},
    )

    def __init__(self, config=None):
        super().__init__(config)
        self.api_key = self.config.get("api_key", "")
        self._last_quota = None
        self._last_snapshot = None
        self._next_probe = 0

    def empty_report(self):
        return quota_report(self.provider, self.product,
                            [quota_window(key, label, None, None, now_utc(), minutes,
                                          state="unavailable") for key, label, minutes in self.windows])

    def collect(self):
        now = time.monotonic()
        if self._last_snapshot is not None and now < self._next_probe:
            return deepcopy(self._last_snapshot)
        self._next_probe = now + self.config.get("refresh_seconds", 60)
        error = None
        status = "offline"
        if not self.api_key:
            error = "Add a %s API key in source configuration" % self.name
        else:
            try:
                data = get_json(self.usage_url, timeout=self.config.get("timeout", 10),
                                headers={"Authorization": "Bearer " + self.api_key,
                                         "Accept": "application/json", "User-Agent": "presto-ai-dash/0.1"},
                                allow_redirects=False)
                self._last_quota, limited = self.parse(data, now_utc())
                status = "rate_limited" if limited else "ok"
            except HttpError as exc:
                error = ("API key rejected" if exc.status == 401 else
                         "Subscription access denied" if exc.status == 403 else
                         "Usage server returned HTTP %d" % exc.status)
            except (HttpUnreachable, ValueError):
                error = "Subscription usage unavailable; network or reporting schema changed"
        report = deepcopy(self._last_quota) if self._last_quota else self.empty_report()
        report["error"] = error
        self._last_snapshot = self.snapshot(status, metrics={"quota": report}, error=error)
        return deepcopy(self._last_snapshot)
