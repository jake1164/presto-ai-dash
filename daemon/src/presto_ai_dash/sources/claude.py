"""Claude Code subscription windows exported by its documented status line."""

from copy import deepcopy
from datetime import datetime
from pathlib import Path

from ..collectors.claude_activity import ActivityReader
from ..models import SourceSnapshot, now_utc
from ..quotas import quota_report, quota_window, timestamp
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
        {"key": "activity_logs",
         "label": "Read local Claude Code activity (timestamps and token counts only)",
         "type": "bool", "default": False},
        {"key": "active_seconds", "label": "Treat Claude as in use for (seconds) after a request",
         "type": "int", "default": 300, "minimum": 30, "maximum": 3600},
    )

    def __init__(self, config=None):
        super().__init__(config)
        # One reader per source instance, so polls stay incremental; a settings
        # edit rebuilds the source and the new instance performs a fresh scan.
        self._activity_reader = None

    def attach_storage(self, storage):
        self.storage = storage

    def _activity(self, now):
        """Local session-log activity from the profile's projects tree."""
        if self._activity_reader is None:
            root = Path(self.config["settings_path"]).parent / "projects"
            self._activity_reader = ActivityReader(root)
        return self._activity_reader.refresh(now)

    def collect(self):
        now = now_utc()
        observation = self.storage.observation(self.id) if hasattr(self, "storage") else None
        if observation is None:
            report = quota_report(self.provider, self.product, [
                quota_window(key, label, None, None, now, minutes, state="unavailable")
                for key, label, minutes in WINDOWS])
            report["note"] = "Awaiting quota export from normal Claude Code terminal use"
        else:
            report = deepcopy(observation["report"])
        # Every report carries the collector trail (None before the first
        # push), so payload building can produce freshness hints (Phase 4).
        report["stale_after_seconds"] = self.config.get("stale_after_seconds", 900)
        for key in ("collector_seen_at", "collector_host", "collector_version", "last_progress_at"):
            report[key] = observation[key] if observation else None
        available = [w for w in report["windows"] if w["state"] == "available"]
        if self.config.get("activity_logs"):
            activity = self._activity(now)
            report["local_activity"] = activity
            if activity["available"]:
                # Assumption D: while the reader works, stale or missing quota
                # alone never raises the status to warning; the hint text
                # carries the staleness. Only a real rate limit stays worse.
                active_seconds = self.config.get("active_seconds", 300)
                report["active_seconds"] = active_seconds
                report["warn_on_stale"] = False
                in_use = (activity["last_request_at"] and
                          (now - timestamp(activity["last_request_at"])).total_seconds()
                          <= active_seconds)
                status = ("rate_limited" if any(w["used_percent"] >= 100 for w in available)
                          else "active" if in_use else "ok")
                observed = max(datetime.fromisoformat(w["observed_at"])
                               for w in report["windows"])
                return SourceSnapshot(self.id, status, updated_at=observed,
                                      metrics={"quota": report})
        if observation is None:
            return self.snapshot("warning", metrics={"quota": report})
        status = ("rate_limited" if any(w["used_percent"] >= 100 for w in available)
                  else "ok" if available else "warning")
        observed = max(datetime.fromisoformat(w["observed_at"]) for w in report["windows"])
        return SourceSnapshot(self.id, status, updated_at=observed, metrics={"quota": report})
