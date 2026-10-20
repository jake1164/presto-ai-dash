"""Admin page provenance: every Claude number says where it came from."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from presto_ai_dash.models import SourceSnapshot
from presto_ai_dash.quotas import quota_report, quota_window
from test_management import AdminServerBase

NOW = datetime(2026, 10, 18, 12, tzinfo=timezone.utc)
DAY = timedelta(days=1)


def local_text(moment):
    """The same local rendering the admin payload carries."""
    return moment.astimezone().strftime("%Y-%m-%d %H:%M")


def window(wid, label, percent, observed, reset=None, period=300, state=None):
    return {"id": wid, "label": label, "period_minutes": period,
            "used_percent": percent,
            "resets_at": reset.isoformat() if reset else None,
            "observed_at": observed.isoformat(),
            "state": state or ("available" if percent is not None else "unavailable")}


def activity(available=True, complete=True, last_request_at=NOW - timedelta(hours=3),
             surface="VS Code", tokens_5h=120000, tokens_7d=2100000, reason=None):
    """The local_activity dict the Phase 5 reader puts into the report."""
    if not available:
        return {"available": False, "reason": reason or "no session directory",
                "complete": False, "last_request_at": None, "last_model": None,
                "last_entrypoint": "?", "requests_5h": 0, "tokens_5h": 0,
                "tokens_7d": 0}
    return {"available": True, "complete": complete,
            "last_request_at": last_request_at.isoformat() if last_request_at else None,
            "last_model": "claude-test", "last_entrypoint": surface,
            "requests_5h": 4, "tokens_5h": tokens_5h, "tokens_7d": tokens_7d}


class AdminProvenanceBase(AdminServerBase):
    """A Claude account with a stored reading, on a temporary database."""

    def setUp(self):
        super().setUp()
        self.disable_fake()
        self.row = self.manager.save({"type": "claude", "name": "Claude personal",
                                      "enabled": True,
                                      "config": {"settings_path": str(Path(self.temp.name) / "settings.json"),
                                                 "activity_logs": True}})
        self.addCleanup(self.storage.close)

    def set_reader(self, enabled):
        """Turn the local activity reader on or off, as the Edit form does."""
        row = next(row for row in self.manager.list_sources()["sources"]
                   if row["id"] == self.row["id"])
        self.manager.save({"revision": row["revision"],
                           "config": dict(row["config"], activity_logs=enabled)}, row["id"])

    def report(self, windows=None, seen=NOW - 6 * DAY, host="laptop", act=None,
               progress=NOW - 6 * DAY):
        if windows is None:
            windows = [window("five_hour", "5h", 12, NOW - 6 * DAY, NOW + DAY),
                       window("seven_day", "Weekly", 34, NOW - 6 * DAY, NOW + 4 * DAY,
                              period=10080)]
        report = quota_report("anthropic", "Claude", windows)
        report["collector_seen_at"] = seen.isoformat() if seen else None
        report["collector_host"] = host if seen else None
        report["collector_version"] = "2.1" if seen else None
        report["last_progress_at"] = progress.isoformat() if seen else None
        if act is not None:
            report["local_activity"] = act
            report["active_seconds"] = 300
            report["warn_on_stale"] = False
        return report

    def store(self, report=None, status="ok"):
        if report is None:
            report = self.report()
        self.storage.save_snapshot(SourceSnapshot(self.row["id"], status, NOW,
                                                  metrics={"quota": report}))

    def listing(self):
        with patch("presto_ai_dash.quotas.now_utc", return_value=NOW):
            return self.manager.list_sources()

    def entry(self, report=None, status="ok"):
        self.store(report, status)
        return next(row for row in self.listing()["sources"] if row["id"] == self.row["id"])

    def provenance(self, report=None, status="ok"):
        return self.entry(report, status)["provenance"]


class AdminQuotaLineTests(AdminProvenanceBase):
    """The quota line says the reading came from the terminal export."""

    def test_the_reading_names_its_source_time_host_and_age(self):
        lines = self.provenance()
        self.assertEqual(lines["quota"], "Quota: terminal export - last reading %s from laptop (6 days old)"
                         % local_text(NOW - 6 * DAY))

    def test_without_a_reading_the_line_says_what_to_do(self):
        entry = self.entry(self.report(windows=[window("five_hour", "5h", None, NOW,
                                                        state="unavailable")]))
        self.assertEqual(entry["provenance"]["quota"],
                         "Quota: no reading yet (use claude in a terminal)")

    def test_an_account_that_never_reported_has_no_quota_reading(self):
        entry = next(row for row in self.listing()["sources"] if row["id"] == self.row["id"])
        self.assertEqual(entry["provenance"]["quota"],
                         "Quota: no reading yet (use claude in a terminal)")

    def test_the_freshness_hint_is_shown_as_a_status_note(self):
        lines = self.provenance()
        self.assertEqual(lines["status_note"], "Status note: No report 6d - run claude")

    def test_without_a_hint_there_is_no_status_note(self):
        report = self.report(seen=NOW, progress=NOW)
        report["windows"] = [window("five_hour", "5h", 12, NOW - timedelta(minutes=2),
                                    NOW + DAY)]
        lines = self.provenance(report)
        self.assertIsNone(lines["status_note"])


class AdminActivityLineTests(AdminProvenanceBase):
    """The activity line names the local logs and what they say."""

    def test_the_reader_off_line_says_where_to_turn_it_on(self):
        self.set_reader(False)
        lines = self.provenance()
        self.assertEqual(lines["activity"],
                         "Activity: reader off (tick 'Read local Claude Code activity' in Edit)")

    def test_a_failing_reader_says_why(self):
        lines = self.provenance(self.report(act=activity(available=False)))
        self.assertEqual(lines["activity"],
                         "Activity: unavailable (no session directory)")

    def test_a_cold_scan_says_scanning_not_lower_bounds(self):
        lines = self.provenance(self.report(act=activity(complete=False)))
        self.assertEqual(lines["activity"],
                         "Activity: local logs - last used 3h ago (VS Code) - tokens scanning")

    def test_a_working_reader_reports_the_surface_and_the_tokens(self):
        lines = self.provenance(self.report(act=activity()))
        self.assertEqual(lines["activity"],
                         "Activity: local logs - last used 3h ago (VS Code) - tokens 5h/7d 120k/2.1M")

    def test_a_reader_with_no_request_yet_says_so(self):
        lines = self.provenance(self.report(act=activity(last_request_at=None,
                                                          tokens_5h=0, tokens_7d=0)))
        self.assertEqual(lines["activity"],
                         "Activity: local logs - no request recorded yet - tokens 5h/7d 0/0")


class AdminWindowTests(AdminProvenanceBase):
    """A window whose reset has passed keeps its last known value."""

    def windows(self):
        return self.entry(self.report(windows=[
            window("five_hour", "5h", 6.0, NOW - 6 * DAY, NOW - 2 * DAY),
            window("seven_day", "Weekly", 34, NOW - 6 * DAY, NOW + 4 * DAY, period=10080)]))["quota"]["windows"]

    def test_a_passed_reset_is_marked(self):
        passed, fresh = self.windows()
        self.assertTrue(passed["reset_passed"])
        self.assertFalse(fresh["reset_passed"])

    def test_a_fresh_window_is_not_marked(self):
        report = self.report()
        entry = self.entry(report)
        self.assertTrue(all(not w["reset_passed"] for w in entry["quota"]["windows"]))

    def test_the_old_value_is_kept_next_to_the_passed_reset(self):
        passed = self.windows()[0]
        self.assertEqual(passed["used_percent"], 6.0)
        self.assertEqual(passed["state"], "stale")


class AdminPayloadKeepsEverythingTests(AdminProvenanceBase):
    """Adding provenance must not remove anything the page already uses."""

    def test_the_existing_payload_keys_are_still_there(self):
        entry = self.entry()
        for key in ("id", "type", "name", "enabled", "config", "revision", "has_key",
                    "export_installed", "ingest", "status", "updated_at", "quota"):
            self.assertIn(key, entry)
        for key in ("token_exists", "created_at", "collector_seen_at", "collector_host",
                    "last_progress_at"):
            self.assertIn(key, entry["ingest"])
        for key in ("provider", "product", "windows", "stale_after_seconds", "error",
                    "collector_seen_at", "collector_host", "hint"):
            self.assertIn(key, entry["quota"])


class AdminPageAssetTests(AdminServerBase):
    """The page wires the provenance lines and the passed-reset marker."""

    def test_the_script_reads_the_provenance_lines(self):
        status, _, script = self.request("/admin.js")
        self.assertEqual(status, 200)
        for marker in (b"provenance", b"reset_passed", b"(passed)", b"last known"):
            self.assertIn(marker, script)

    def test_the_page_serves_the_script(self):
        status, _, html = self.request("/")
        self.assertEqual(status, 200)
        self.assertIn(b"admin.js", html)


if __name__ == "__main__":
    unittest.main()
