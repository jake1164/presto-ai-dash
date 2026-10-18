"""Claude Code subscription windows exported by its documented status line."""

from copy import deepcopy
from datetime import datetime
from pathlib import Path

from ..models import SourceSnapshot, now_utc
from ..quotas import quota_report, quota_window
from . import Source, register

WINDOWS = (("five_hour", "5h", 300), ("seven_day", "Weekly", 10080))


@register
class ClaudeSource(Source):
    id = "claude"
    name = "Claude"
    provider = "anthropic"
    product = "Claude"
    stale_after_seconds = 900
    report_note = ("Claude Code terminal export; idle readings become stale. "
                   "VS Code panel coverage is unverified.")
    accepts_push = True
    push_window_ids = ("five_hour", "seven_day")
    config_fields = (
        {"key": "settings_path", "label": "Claude Code settings file (one profile per account)",
         "type": "string", "default": str(Path.home() / ".claude" / "settings.json")},
        {"key": "stale_after_seconds", "label": "Mark idle readings stale after (seconds)",
         "type": "int", "default": 900, "minimum": 30, "maximum": 86400},
    )

    def attach_storage(self, storage):
        self.storage = storage

    def collect(self):
        observation = self.storage.observation(self.id) if hasattr(self, "storage") else None
        report = deepcopy(observation["report"]) if observation else None
        if report is None:
            report = quota_report(self.provider, self.product, [
                quota_window(key, label, None, None, now_utc(), minutes, state="unavailable")
                for key, label, minutes in WINDOWS])
            report["note"] = "Awaiting quota export from normal Claude Code terminal use"
            return self.snapshot("warning", metrics={"quota": report})
        report["stale_after_seconds"] = self.config.get("stale_after_seconds", 900)
        for key in ("collector_seen_at", "collector_host", "collector_version", "last_progress_at"):
            report[key] = observation[key]
        available = [w for w in report["windows"] if w["state"] == "available"]
        status = ("rate_limited" if any(w["used_percent"] >= 100 for w in available)
                  else "ok" if available else "warning")
        observed = max(datetime.fromisoformat(w["observed_at"]) for w in report["windows"])
        return SourceSnapshot(self.id, status, updated_at=observed, metrics={"quota": report})
