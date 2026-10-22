"""Claude Code subscription windows: the status-line export and the usage cache."""

from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path

from ..collectors.claude_activity import ActivityReader
from ..collectors.claude_usage_cache import UsageCacheReader, candidate_paths
from ..models import SourceSnapshot, now_utc
from ..quotas import (USAGE_CACHE_STALE_SECONDS, quota_report, quota_window, timestamp)
from . import Source, register

WINDOWS = (("five_hour", "5h", 300), ("seven_day", "Weekly", 10080))

# Where a number came from. Every window carries one, so a reading is never
# presented as if another source had produced it.
SOURCE_EXPORT = "terminal export"
SOURCE_CACHE = "usage cache"

NOTE_EXPORT = ("Claude Code terminal export; idle readings become stale. "
               "The VS Code panel does not run the status line, so quota only "
               "updates from terminal sessions.")
NOTE_CACHE = ("Claude Code terminal export and its local usage cache; idle readings "
              "become stale. The VS Code panel does not run the status line, so quota "
              "arrives from terminal sessions and from the usage cache Claude Code "
              "writes for its /usage dialog (opening /usage refreshes it).")
# A pushed activity summary older than this is no longer a reading: the
# collector that sent it has stopped, and its token totals would stand still.
PUSHED_ACTIVITY_SECONDS = 600

NOTE_AWAITING = "Awaiting quota export from normal Claude Code terminal use"
NOTE_AWAITING_CACHE = ("Awaiting quota export from normal Claude Code terminal use "
                       "or its local usage cache")


@register
class ClaudeSource(Source):
    id = "claude"
    name = "Claude"
    provider = "anthropic"
    product = "Claude"
    stale_after_seconds = 900
    report_note = NOTE_EXPORT
    accepts_push = True
    push_window_ids = ("five_hour", "seven_day")
    # A collector may also push what the local readers would find on its own
    # machine, and may do so before any quota has been reported.
    push_extras = frozenset(("activity", "usage_cache"))
    push_default_windows = WINDOWS
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
        {"key": "usage_cache",
         "label": "Read Claude Code's local usage cache (usage numbers only)",
         "type": "bool", "default": False},
        {"key": "usage_cache_path", "label": "Claude Code state file (blank: automatic)",
         "type": "string", "default": ""},
        {"key": "usage_cache_stale_seconds",
         "label": "Mark usage cache readings stale after (seconds)",
         "type": "int", "default": USAGE_CACHE_STALE_SECONDS, "minimum": 300,
         "maximum": 604800},
    )

    def __init__(self, config=None):
        super().__init__(config)
        # One reader per source instance, so polls stay incremental; a settings
        # edit rebuilds the source and the new instance performs a fresh scan.
        self._activity_reader = None
        # The usage cache reader lives as long as the configuration: it holds
        # the account fingerprint in memory, and a re-save starts a new one.
        self._usage_reader = None

    def attach_storage(self, storage):
        self.storage = storage

    def _activity(self, now):
        """Local session-log activity from the profile's projects tree."""
        if self._activity_reader is None:
            root = Path(self.config["settings_path"]).parent / "projects"
            self._activity_reader = ActivityReader(root)
        return self._activity_reader.refresh(now)

    def _usage_cache(self, now):
        """The usage cache reading, or None when the reader is off."""
        if not self.config.get("usage_cache"):
            return None
        if self._usage_reader is None:
            override = (self.config.get("usage_cache_path") or "").strip()
            candidates = candidate_paths(self.config.get("settings_path"),
                                         override or None)
            self._usage_reader = UsageCacheReader(candidates)
        return self._usage_reader.refresh(now)

    @staticmethod
    def _pushed_usage_cache(stored, host):
        """The usage-cache reading a collector pushed, in the shape the local reader returns."""
        pushed = stored.get("pushed_usage_cache") if stored else None
        if not isinstance(pushed, dict):
            return None
        cache = {key: value for key, value in pushed.items() if key != "received_at"}
        cache["path"] = None
        cache["message"] = None if cache.get("available") else (
            "Claude usage cache on %s: %s" % (host or "the collector",
                                              cache.get("reason") or "unavailable"))
        cache["reported_by"] = host
        return cache

    @staticmethod
    def _pushed_activity(stored, now):
        """The activity summary a collector pushed, or an unavailable one once it has gone quiet."""
        pushed = stored.get("pushed_activity") if stored else None
        if not isinstance(pushed, dict):
            return None
        received = timestamp(pushed["received_at"])
        if now - received > timedelta(seconds=PUSHED_ACTIVITY_SECONDS):
            return {"available": False, "reason": "the collector stopped reporting",
                    "complete": False, "last_request_at": None, "last_model": None,
                    "last_entrypoint": "?", "requests_5h": 0, "tokens_5h": 0, "tokens_7d": 0}
        return {key: value for key, value in pushed.items() if key != "received_at"}

    def _merge_cache(self, report, cache):
        """The newest reading per window wins; the terminal export wins a tie.

        Nothing is stored for the cache: the merge happens here, at collect
        time, and is recomputed on every poll.
        """
        if not cache.get("available") or not cache.get("fetched_at"):
            return
        observed = timestamp(cache["fetched_at"])
        windows = list(report.get("windows", []))
        positions = {window.get("id"): index for index, window in enumerate(windows)}
        for entry in cache.get("windows", []):
            percent = entry.get("used_percent")
            if percent is None:
                continue  # a window that is not reported stays as it was
            position = positions.get(entry.get("id"))
            if position is not None:
                existing = windows[position]
                if (existing.get("used_percent") is not None and
                        timestamp(existing["observed_at"]) >= observed):
                    continue  # the terminal export is newer, or it is a tie
            window = quota_window(entry["id"], entry["label"], percent,
                                  entry.get("resets_at"), observed,
                                  entry.get("period_minutes"), source=SOURCE_CACHE)
            if position is None:
                positions[entry["id"]] = len(windows)
                windows.append(window)
            else:
                windows[position] = window
        report["windows"] = windows

    @staticmethod
    def _note(observation, cache):
        """Why the quota looks the way it does, in the current wording."""
        if cache is not None and cache.get("available"):
            return NOTE_CACHE
        if observation is None:
            return NOTE_AWAITING if cache is None else NOTE_AWAITING_CACHE
        return NOTE_EXPORT

    def _usage_cache_stale(self):
        """The freshness threshold for readings from the usage cache.

        The cache is only refreshed when /usage is opened in Claude Code, so
        the terminal export's threshold would call a good reading stale. A
        missing or unusable setting falls back to the default.
        """
        value = self.config.get("usage_cache_stale_seconds")
        if type(value) is not int or value < 1:
            return USAGE_CACHE_STALE_SECONDS
        return value

    def collect(self):
        now = now_utc()
        observation = self.storage.observation(self.id) if hasattr(self, "storage") else None
        stored = observation["report"] if observation else None
        host = observation["collector_host"] if observation else None
        cache = self._usage_cache(now)
        pushed_cache = self._pushed_usage_cache(stored, host)
        if pushed_cache is not None and (cache is None or not cache.get("available")):
            cache = pushed_cache  # the daemon's own reader has nothing; the collector's does
        if stored is None:
            report = quota_report(self.provider, self.product, [
                quota_window(key, label, None, None, now, minutes, state="unavailable",
                             source=SOURCE_EXPORT)
                for key, label, minutes in WINDOWS])
        else:
            report = deepcopy(stored)
        # What a collector pushed is the source's input, not part of its report.
        for key in ("pushed_activity", "pushed_usage_cache"):
            report.pop(key, None)
        # Every window says where it came from.
        for window in report["windows"]:
            window.setdefault("source", SOURCE_EXPORT)
        if cache is not None:
            report["usage_cache"] = {key: value for key, value in cache.items()
                                     if key != "windows"}
            self._merge_cache(report, cache)
        # The note is the wording of today, not a copy frozen into the stored
        # reading when it was received (ingest writes one into every report).
        report["note"] = self._note(observation, cache)
        # Every report carries the collector trail (None before the first
        # push), so payload building can produce freshness hints (Phase 4).
        report["stale_after_seconds"] = self.config.get("stale_after_seconds", 900)
        # The usage cache has its own threshold; the terminal export keeps the
        # one above, and every window is judged by the source it came from.
        report["usage_cache_stale_seconds"] = self._usage_cache_stale()
        for key in ("collector_seen_at", "collector_host", "collector_version", "last_progress_at"):
            report[key] = observation[key] if observation else None
        available = [w for w in report["windows"] if w["state"] == "available"]
        activity = self._activity(now) if self.config.get("activity_logs") else None
        pushed_activity = self._pushed_activity(stored, now)
        if pushed_activity is not None and (activity is None or not activity["available"]):
            activity = pushed_activity
        if activity is not None:
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
        if stored is None and not available:
            return self.snapshot("warning", metrics={"quota": report})
        status = ("rate_limited" if any(w["used_percent"] >= 100 for w in available)
                  else "ok" if available else "warning")
        observed = max(datetime.fromisoformat(w["observed_at"]) for w in report["windows"])
        return SourceSnapshot(self.id, status, updated_at=observed, metrics={"quota": report})
