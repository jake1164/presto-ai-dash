"""Phase 4: freshness hints at payload-build time and the detail-card trail."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from presto_ai_dash.claude_export import build_envelope
from presto_ai_dash.dashboard import (DETAIL_LINE_CHARS, _account_card, build_instance_payload)
from presto_ai_dash.ingest import ingest
from presto_ai_dash.management import ManagedCollector
from presto_ai_dash.models import SourceSnapshot
from presto_ai_dash.quotas import (age_label, claude_hint, compact_age, compact_tokens,
                                   estimated_weekly_reset, present_quota, quota_report)
from test_management import ManagementBase

NOW = datetime(2026, 10, 18, 12, tzinfo=timezone.utc)
DAY = timedelta(days=1)
HINTS = {"rule1": "Run claude in a terminal",
         "rule2": "Waiting for a Claude reply",
         "rule3": "Awaiting refresh",
         "rule4a": "Reply in claude to refresh",
         "rule4b": "No report 6d - run claude"}


def window(wid, label, percent, observed, reset=None, period=300, state=None):
    return {"id": wid, "label": label, "period_minutes": period,
            "used_percent": percent,
            "resets_at": reset.isoformat() if reset else None,
            "observed_at": observed.isoformat(),
            "state": state or ("available" if percent is not None else "unavailable")}


def unmeasured():
    return [window("five_hour", "5h", None, NOW, state="unavailable"),
            window("seven_day", "Weekly", None, NOW, state="unavailable", period=10080)]


def measured(hours_old):
    """Both windows measured once, aged beyond the default 900s, unexpired."""
    observed = NOW - timedelta(hours=hours_old)
    return [window("five_hour", "5h", 12, observed, observed + 6 * DAY),
            window("seven_day", "Weekly", 34, observed, observed + 8 * DAY, period=10080)]


def trail_report(windows, seen=NOW, host="laptop", progress=NOW, stale_after=900):
    """A quota report shaped like what ClaudeSource.collect hands to the payload."""
    report = quota_report("anthropic", "Claude", windows, stale_after_seconds=stale_after)
    report["collector_seen_at"] = seen.isoformat() if seen else None
    report["collector_host"] = host if seen else None
    report["collector_version"] = "2.1" if seen else None
    report["last_progress_at"] = progress.isoformat() if seen else None
    return report


def presented(windows, seen=NOW, seen_ago=None, **trail):
    """The hint as the payload builder computes it: the report is aged first."""
    age = timedelta(minutes=seen_ago) if seen_ago is not None else None
    seen = NOW - age if age is not None else seen
    return present_quota(trail_report(windows, seen=seen, **trail), NOW)["hint"]


def sample(percent=23.5, tokens=100):
    """A synthetic Claude status-line export, anchored to this module's NOW.

    The context tokens act as the progress marker; a response changes them,
    which refreshes the evidence of every measurable window.
    """
    return {"version": "2.1.296",
            "cost": {"total_api_duration_ms": 300},
            "context_window": {"total_input_tokens": 600, "total_output_tokens": tokens},
            "rate_limits": {
                "five_hour": {"used_percentage": percent,
                              "resets_at": int((NOW + timedelta(hours=5)).timestamp())},
                "seven_day": {"used_percentage": 41.2,
                              "resets_at": int((NOW + DAY).timestamp() + 7 * 86400)}}}


class HintRuleTests(unittest.TestCase):
    def test_rule1_nothing_has_ever_been_reported(self):
        self.assertEqual(presented(unmeasured(), seen=None), HINTS["rule1"])
        self.assertEqual(claude_hint(trail_report(unmeasured(), seen=None), NOW),
                         HINTS["rule1"])

    def test_rule2_collector_heartbeats_without_any_window(self):
        self.assertEqual(presented(unmeasured(), seen_ago=1), HINTS["rule2"])
        self.assertEqual(claude_hint(trail_report(unmeasured(), seen=NOW), NOW),
                         HINTS["rule2"])

    def test_rule3_every_reset_passed_waits_for_the_provider(self):
        windows = measured(6 * 24)
        for every in windows:
            every["resets_at"] = (NOW - timedelta(hours=3)).isoformat()
        # The collector beats recently (or never): rule 3 still decides first.
        for seen in (None, NOW - timedelta(minutes=5)):
            with self.subTest(recent_collector=seen is not None):
                self.assertEqual(presented(windows, seen=seen), HINTS["rule3"])

    def test_rule4a_collector_beacons_recently_but_quota_is_stale(self):
        self.assertEqual(presented(measured(24), seen_ago=5), HINTS["rule4a"])

    def test_rule4b_collector_quiet_names_the_newest_signal_age(self):
        self.assertEqual(presented(measured(6 * 24), seen_ago=6 * 24 * 60),
                         HINTS["rule4b"])
        # The newest signal of any kind decides: a 59m-old collector beats a
        # 6d-old window, but 59m is no longer "recent", so the hint still says.
        windows = [window("five_hour", "5h", 12, NOW - 3 * DAY, NOW + timedelta(hours=4))]
        self.assertEqual(presented(windows, seen_ago=59), "No report 59m - run claude")

    def test_legacy_install_without_collector_uses_the_quota_age(self):
        # A migrated database carries quota data but no collector_seen_at:
        # the collector counts as never seen and rule 4's second branch applies.
        self.assertEqual(presented(measured(12), seen=None, progress=None),
                         "No report 12h - run claude")

    def test_rule5_fresh_quota_needs_no_hint(self):
        fresh = [window("five_hour", "5h", 12, NOW - timedelta(minutes=5),
                        NOW + timedelta(hours=4)),
                 window("seven_day", "Weekly", 34, NOW - DAY, NOW + DAY, period=10080)]
        self.assertIsNone(presented(fresh, seen_ago=3 * 24 * 60))

    def test_age_formatting_boundaries(self):
        self.assertEqual([compact_age(seconds) for seconds in
                          (0, 59 * 60, 60 * 60, 47 * 3600, 48 * 3600, 6 * 86400)],
                         ["0m", "59m", "1h", "47h", "2d", "6d"])

    def test_detail_age_spells_out_units_at_each_boundary(self):
        self.assertEqual([age_label(seconds) for seconds in
                          (0, 59 * 60, 60 * 60 - 1, 60 * 60,
                           47 * 3600, 48 * 3600, 6 * 86400)],
                         ["0 min", "59 min", "59 min", "1 h", "47 h",
                          "2 days", "6 days"])
        # The 48-hour boundary switches directly to days, so "1 day" is
        # intentionally unreachable under this display rule.

    def test_hints_are_ascii_and_never_exceed_28_characters(self):
        stale = measured(24)
        for every in stale:
            every["resets_at"] = (NOW - timedelta(hours=3)).isoformat()
        cases = (("rule1", unmeasured(), None), ("rule2", unmeasured(), NOW - timedelta(minutes=1)),
                 ("rule3", stale, NOW - timedelta(minutes=5)),
                 ("rule4a", measured(24), NOW - timedelta(minutes=5)),
                 ("rule4b", measured(6 * 24), NOW - timedelta(hours=6 * 24)),
                 ("legacy", measured(12), None))
        for name, windows, seen in cases:
            hint = presented(windows, seen=seen)
            with self.subTest(rule=name):
                self.assertTrue(hint)
                self.assertTrue(all(ord(char) < 128 for char in hint), hint)
                self.assertLessEqual(len(hint), 28, hint)
        got = {name: presented(windows, seen=seen) for name, windows, seen in cases}
        self.assertEqual({got["rule1"], got["rule2"], got["rule3"], got["rule4a"],
                          got["rule4b"]}, set(HINTS.values()))
        self.assertEqual(got["legacy"], "No report 12h - run claude")


class HintPayloadTests(ManagementBase):
    """The hint must age at payload-build time, as the status API serves it."""

    def setUp(self):
        super().setUp()
        self.disable_fake()
        self.manager.save({"type": "claude", "name": "Claude personal", "enabled": True,
                           "config": {"settings_path": str(Path(self.temp.name) / "settings.json")}})

    def export(self, observed, data=None):
        envelope = build_envelope(data or sample(), "laptop", client_time=observed)
        row = next(source for source in self.manager.list_sources()["sources"]
                   if source["type"] == "claude")
        self.assertTrue(ingest(self.storage, row["id"], envelope, observed))

    def collect(self):
        ManagedCollector(self.manager, self.config, threading.Event()).collect_once()

    def later(self, moment):
        return patch("presto_ai_dash.quotas.now_utc", return_value=moment), \
            patch("presto_ai_dash.dashboard.now_utc", return_value=moment)

    def card_and_source(self):
        payload = self.manager.status_payload()
        source = next(source for source in payload["sources"] if source["type"] == "claude")
        return payload["cards"][1], source

    def test_hint_reaches_the_status_payload_then_fresh_data_drops_it(self):
        self.export(NOW)
        self.collect()
        minute = timedelta(minutes=16)
        quota_patch, card_patch = self.later(NOW + minute)
        with quota_patch, card_patch:
            card, source = self.card_and_source()
            self.assertEqual(card["status"], "warning")
            self.assertEqual(source["status"], "warning")
            self.assertEqual(source["quota"]["hint"], "No report 16m - run claude")
        # A new reading answers the hint at once; it is never a fresh 0%.
        self.export(NOW + 5 * minute, sample(44.5, tokens=200))
        self.collect()
        minute = 5 * minute
        quota_patch, card_patch = self.later(NOW + minute)
        with quota_patch, card_patch:
            card, source = self.card_and_source()
            self.assertEqual(card["status"], "ok")
            self.assertIsNone(source["quota"]["hint"])
            self.assertIn("5h: 44.5%", card["primary"])


class HintStatusTests(unittest.TestCase):
    """Hints warn about freshness; they never turn a healthy source offline."""

    def report_for_hint(self, name):
        if name == "rule1":
            return trail_report(unmeasured(), seen=None)
        if name == "rule2":
            return trail_report(unmeasured(), seen=NOW)
        if name == "rule3":
            windows = measured(24)
            for item in windows:
                item["resets_at"] = (NOW - timedelta(hours=1)).isoformat()
            return trail_report(windows)
        if name == "rule4a":
            return trail_report(measured(24), seen=NOW - timedelta(minutes=5))
        return trail_report(measured(6 * 24), seen=NOW - 6 * DAY)

    def payload(self, report, status):
        snapshot = SourceSnapshot("c", status, NOW, {"quota": report})
        row = {"id": "c", "type": "claude", "name": "Claude", "enabled": True}
        with patch("presto_ai_dash.quotas.now_utc", return_value=NOW), \
                patch("presto_ai_dash.dashboard.now_utc", return_value=NOW):
            return build_instance_payload({"c": snapshot}, [row])

    def test_each_hint_warns_but_never_marks_ok_snapshot_offline(self):
        for name in HINTS:
            with self.subTest(hint=name):
                payload = self.payload(self.report_for_hint(name), "ok")
                card = next(card for card in payload["cards"] if card["id"] == "c")
                source = next(source for source in payload["sources"] if source["id"] == "c")
                self.assertEqual(card["status"], "warning")
                self.assertEqual(source["status"], "warning")
                self.assertNotEqual(card["status"], "offline")
                self.assertNotEqual(source["status"], "offline")

    def test_hint_preserves_offline_and_other_worse_statuses(self):
        report = self.report_for_hint("rule1")
        for status in ("offline", "critical", "rate_limited"):
            with self.subTest(status=status):
                payload = self.payload(report, status)
                card = next(card for card in payload["cards"] if card["id"] == "c")
                source = next(source for source in payload["sources"] if source["id"] == "c")
                self.assertEqual(card["status"], status)
                self.assertEqual(source["status"], status)

    def test_fresh_reading_without_hint_keeps_status_unchanged(self):
        fresh = [window("five_hour", "5h", 12, NOW - timedelta(minutes=5),
                         NOW + timedelta(hours=4)),
                 window("seven_day", "Weekly", 34, NOW - timedelta(minutes=5),
                        NOW + DAY, period=10080)]
        payload = self.payload(trail_report(fresh), "ok")
        card = next(card for card in payload["cards"] if card["id"] == "c")
        source = next(source for source in payload["sources"] if source["id"] == "c")
        self.assertEqual(card["status"], "ok")
        self.assertEqual(source["status"], "ok")
        self.assertIsNone(source["quota"]["hint"])


class ClaudeDetailCardTests(unittest.TestCase):
    """Part B: the observation trail lines, in order, within the six-line cap."""

    def card(self, windows, seen=NOW, host="laptop", progress=NOW - timedelta(minutes=90)):
        report = trail_report(windows, seen=seen, host=host, progress=progress)
        snapshot = SourceSnapshot("c", "ok", NOW, {"quota": report})
        with patch("presto_ai_dash.dashboard.now_utc", return_value=NOW):
            return _account_card({"id": "c", "name": "Claude", "type": "claude"}, snapshot)

    def fresh_windows(self):
        return [window("five_hour", "5h", 12, NOW - timedelta(minutes=5), NOW + timedelta(hours=4)),
                window("seven_day", "Weekly", 34, NOW - timedelta(minutes=90), NOW + DAY,
                       period=10080)]

    def test_reset_lines_stay_first_then_data_age_reply_and_reports(self):
        card = self.card(self.fresh_windows(), seen=NOW - timedelta(minutes=10))
        self.assertEqual(len(card.detail), 5)
        self.assertEqual(card.detail[2], "Quota via export, 5 min old")
        self.assertEqual(card.detail[3], "Last reply: 1 h ago")
        self.assertEqual(card.detail[4], "Reports: laptop 10 min ago")
        self.assertEqual(card.primary, "5h: 12%")

    def test_missing_progress_and_collector_are_honest_not_zero(self):
        card = self.card(self.fresh_windows(), seen=None, progress=None)
        self.assertEqual(card.detail[2], "Quota via export, 5 min old")
        self.assertEqual(card.detail[3], "Last reply: unavailable")
        self.assertEqual(card.detail[4], "Reports: never")

    def test_six_line_cap_drops_the_priorities_from_the_bottom(self):
        windows = [window("w%d" % index, "W%d" % index, index * 10, NOW - timedelta(minutes=5),
                          NOW + timedelta(hours=5 + index), period=60)
                   for index in range(5)]
        card = self.card(windows)
        self.assertEqual(len(card.detail), 6)
        self.assertTrue(all(line.startswith("W") for line in card.detail[:5]))
        self.assertEqual(card.detail[5], "Quota via export, 5 min old")
        self.assertNotIn("Last reply: unavailable", card.detail)
        self.assertNotIn("Reports: never", card.detail)

    def test_reports_without_the_trail_keep_the_existing_card(self):
        report = quota_report("opencode", "Go", self.fresh_windows())
        snapshot = SourceSnapshot("c", "ok", NOW, {"quota": report})
        with patch("presto_ai_dash.dashboard.now_utc", return_value=NOW):
            card = _account_card({"id": "c", "name": "Go", "type": "opencode_go"}, snapshot)
        self.assertEqual(len(card.detail), 2)
        self.assertTrue(all(line.endswith("Z") for line in card.detail))
        self.assertFalse(any(line.startswith("Quota age") for line in card.detail))

    def test_every_detail_line_fits_firmware_bitmap_font_for_edge_fixtures(self):
        preview_dir = Path(__file__).resolve().parents[2] / "tools" / "presto_preview"
        sys.path.insert(0, str(preview_dir))
        from framebuffer import Framebuffer

        fresh = self.fresh_windows()
        stale = [window("five_hour", "5h", 12, NOW - 6 * DAY, NOW + DAY),
                 window("seven_day", "Weekly", 34, NOW - 6 * DAY,
                        NOW + 2 * DAY, period=10080)]
        passed = [window("five_hour", "5h", 12, NOW - 6 * DAY,
                         NOW - 2 * DAY - timedelta(hours=20)),
                  window("seven_day", "Weekly", 34, NOW - 6 * DAY,
                         NOW - 2 * DAY - timedelta(hours=20), period=10080)]
        codex = quota_report("openai", "Codex", [
            window("primary", "5h", 12, NOW - timedelta(minutes=5), NOW + DAY),
            window("secondary", "Weekly", 34, NOW - timedelta(minutes=5),
                   NOW + 7 * DAY, period=10080)])
        long_label = [window("weekly", "Extremely Long Weekly Allowance Window", 34,
                             NOW - 6 * DAY, NOW - 2 * DAY - timedelta(hours=20),
                             period=10080)]
        worst = [window("weekly", "Weekly", 99, NOW - timedelta(hours=47),
                        NOW - timedelta(minutes=1), period=10080)]
        fixtures = [
            ("fresh", trail_report(fresh, seen=NOW - timedelta(minutes=5),
                                   host="a-very-long-collector-host-name")),
            ("stale", trail_report(stale, seen=NOW - 6 * DAY,
                                    host="a-very-long-collector-host-name",
                                    progress=NOW - 6 * DAY)),
            ("both-resets-passed", trail_report(passed, seen=NOW - 6 * DAY,
                                                 progress=NOW - 6 * DAY)),
            ("codex-two-windows", codex),
            ("long-window-label", trail_report(long_label, seen=NOW,
                                                host="a-very-long-collector-host-name")),
            ("worst-age-and-passed-reset", trail_report(
                worst, seen=NOW - 7 * DAY, host="a-very-long-collector-host-name",
                progress=NOW - 7 * DAY)),
        ]
        from copy import deepcopy
        for name, report in list(fixtures):
            if name in ("stale", "both-resets-passed", "worst-age-and-passed-reset"):
                # Phase 5: the reader lines ride the same six-line budget.
                fixtures.append((name + "-activity", with_activity(deepcopy(report),
                    activity(last_request_at=NOW - 6 * DAY + timedelta(minutes=5),
                             surface="12345678", requests=99,
                             tokens_5h=999999, tokens_7d=299999999))))
        # The worst case for the token line: 999k in five hours and 999.9M in
        # seven days. The line must still fit, label included.
        fixtures.append(("worst-token-totals", with_activity(
            trail_report(fresh, seen=NOW - timedelta(minutes=5)),
            activity(last_request_at=NOW - timedelta(minutes=5),
                     tokens_5h=999999, tokens_7d=999900000))))
        display = Framebuffer()
        display.set_font("bitmap6")
        available_width = display.width - (8 + 6) - 8  # draw_card x and right margin
        for name, report in fixtures:
            with self.subTest(fixture=name):
                snapshot = SourceSnapshot("c", "warning", NOW, {"quota": report})
                with patch("presto_ai_dash.dashboard.now_utc", return_value=NOW):
                    card = _account_card({"id": "c", "name": "Claude account", "type": "claude"},
                                         snapshot)
                lines = card.detail[:6]
                for line in lines:
                    self.assertLessEqual(display.measure_text(line, 1), available_width, line)
                    # draw_card applies this limit before drawing, so a fitting
                    # detail must retain its complete reset timestamp too.
                    self.assertLessEqual(len(line), DETAIL_LINE_CHARS, line)
                if name == "both-resets-passed":
                    self.assertTrue(any(line.endswith("16:00Z") for line in lines), lines)
                if name == "long-window-label":
                    self.assertTrue(any(line.endswith("16:00Z") for line in lines), lines)
                if name == "worst-token-totals":
                    self.assertIn("Log tokens 5h/7d: 999k/999.9M", lines)


def activity(available=True, last_request_at=NOW, model="claude-test", surface="VS Code",
             requests=4, tokens_5h=120000, tokens_7d=2100000, reason=None, complete=True):
    """The local_activity dict the Phase 5 reader puts into the report."""
    if not available:
        return {"available": False, "reason": reason or "no session directory",
                "complete": False, "last_request_at": None, "last_model": None,
                "last_entrypoint": "?", "requests_5h": 0, "tokens_5h": 0, "tokens_7d": 0}
    return {"available": True, "complete": complete,
            "last_request_at": last_request_at.isoformat()
            if last_request_at else None,
            "last_model": model, "last_entrypoint": surface, "requests_5h": requests,
            "tokens_5h": tokens_5h, "tokens_7d": tokens_7d}


def with_activity(report, act=None, active_seconds=300):
    """A trail report plus the Phase 5 keys ClaudeSource sets when the reader works."""
    report["local_activity"] = act if act is not None else activity()
    report["active_seconds"] = active_seconds
    report["warn_on_stale"] = False
    return report


def expired(windows):
    for every in windows:
        every["resets_at"] = (NOW - timedelta(hours=3)).isoformat()
    return windows


class ActivityHintTests(unittest.TestCase):
    """Phase 5: the in-use hint table, with the reader available."""

    def hint(self, report):
        return present_quota(report, NOW)["hint"]

    def test_in_use_with_old_quota_names_the_data_age(self):
        # A fresh heartbeat must not move the quoted quota age: windows lead.
        report = with_activity(trail_report(measured(6 * 24), seen=NOW))
        report["local_activity"] = activity(last_request_at=NOW - timedelta(seconds=60))
        self.assertEqual(self.hint(report), "In use - quota 6d old")

    def test_in_use_with_no_quota_reading_ever(self):
        report = with_activity(trail_report(unmeasured(), seen=NOW - 6 * DAY))
        report["local_activity"] = activity(last_request_at=NOW - timedelta(seconds=60))
        self.assertEqual(self.hint(report), "In use - no quota data")

    def test_in_use_after_every_reset_passed(self):
        report = with_activity(trail_report(expired(measured(24))))
        report["local_activity"] = activity(last_request_at=NOW - timedelta(seconds=60))
        self.assertEqual(self.hint(report), "In use - quota expired")

    def test_used_hours_ago_still_names_the_surface_age(self):
        report = with_activity(trail_report(measured(6 * 24), seen=NOW - 6 * DAY))
        report["local_activity"] = activity(last_request_at=NOW - timedelta(hours=3))
        self.assertEqual(self.hint(report), "Used 3h ago - quota 6d old")

    def test_used_hours_ago_with_expired_quota(self):
        report = with_activity(trail_report(expired(measured(24))))
        report["local_activity"] = activity(last_request_at=NOW - timedelta(hours=3))
        self.assertEqual(self.hint(report), "Used 3h ago - quota expired")

    def test_fresh_quota_needs_no_hint_however_recent_the_use(self):
        fresh = [window("five_hour", "5h", 12, NOW - timedelta(minutes=5),
                        NOW + timedelta(hours=4))]
        report = with_activity(trail_report(fresh))
        report["local_activity"] = activity(last_request_at=NOW - timedelta(seconds=30))
        self.assertIsNone(self.hint(report))

    def test_the_active_window_edge_splits_in_use_from_used_ago(self):
        for seconds, expected in ((300, "In use - quota 6d old"),
                                  (301, "Used 5m ago - quota 6d old")):
            with self.subTest(seconds=seconds):
                report = with_activity(trail_report(measured(6 * 24), seen=NOW - 6 * DAY))
                report["local_activity"] = activity(last_request_at=NOW - timedelta(seconds=seconds))
                self.assertEqual(self.hint(report), expected)

    def test_quiet_for_24_hours_falls_back_to_the_phase_4_hint(self):
        for seen, expected in ((NOW, HINTS["rule4a"]), (NOW - 6 * DAY, HINTS["rule4b"])):
            report = with_activity(trail_report(measured(6 * 24), seen=seen))
            report["local_activity"] = activity(last_request_at=NOW - timedelta(hours=24))
            with self.subTest(collector="recent" if seen is NOW else "quiet"):
                self.assertEqual(self.hint(report), expected)

    def test_reader_unavailable_or_silent_keeps_phase_4_rules(self):
        for act in (activity(available=False), activity(last_request_at=None)):
            report = with_activity(trail_report(unmeasured(), seen=None), act)
            self.assertEqual(self.hint(report), HINTS["rule1"])

    def test_long_hints_drop_the_word_old_before_exceeding_the_cap(self):
        report = with_activity(trail_report(
            [window("five_hour", "5h", 12, NOW - timedelta(days=400), NOW + DAY)]))
        report["local_activity"] = activity(last_request_at=NOW - timedelta(hours=23))
        self.assertEqual(self.hint(report), "Used 23h ago - quota 400d")

    def test_every_activity_hint_is_ascii_and_within_28_characters(self):
        quota_cases = (measured(6 * 24), unmeasured(), expired(measured(24)),
                       [window("five_hour", "5h", 12, NOW - timedelta(days=400), NOW + DAY)])
        uses = (NOW - timedelta(seconds=60), NOW - timedelta(seconds=3601),
                NOW - timedelta(hours=23), NOW - timedelta(hours=23, minutes=59))
        hints = set()
        for windows in quota_cases:
            for last in uses:
                report = with_activity(trail_report(windows, seen=NOW - 6 * DAY))
                report["local_activity"] = activity(last_request_at=last)
                hint = self.hint(report)
                hints.add(hint)
                with self.subTest(hint=hint):
                    self.assertTrue(hint and hint.isascii(), hint)
                    self.assertLessEqual(len(hint), 28, hint)
        self.assertIn("In use - quota 6d old", hints)
        self.assertIn("Used 23h ago - quota 400d", hints)


class EstimatedWeeklyResetTests(unittest.TestCase):
    """The weekly cycle is fixed, so a passed reset predicts the next one."""

    def seven(self, reset):
        return window("seven_day", "Weekly", 34, NOW - 6 * DAY, reset, period=10080)

    def test_a_passed_reset_repeats_by_weeks_until_it_is_in_the_future(self):
        for days_ago, weeks_forward in ((2, 1), (9, 2), (16, 3), (30, 5)):
            reset = NOW - timedelta(days=days_ago, hours=1)
            estimate = estimated_weekly_reset([self.seven(reset)], NOW)
            with self.subTest(days_ago=days_ago):
                self.assertEqual(estimate, reset + timedelta(days=7 * weeks_forward))

    def test_a_future_measured_reset_is_the_real_answer_not_an_estimate(self):
        self.assertIsNone(estimated_weekly_reset([self.seven(NOW + timedelta(hours=6))], NOW))

    def test_no_weekly_reading_never_becomes_a_guess(self):
        self.assertIsNone(estimated_weekly_reset([], NOW))
        self.assertIsNone(estimated_weekly_reset([self.seven(None)], NOW))
        five_only = window("five_hour", "5h", 12, NOW - DAY, NOW - timedelta(hours=2))
        self.assertIsNone(estimated_weekly_reset([five_only], NOW))

    def test_the_newest_real_reset_wins(self):
        estimate = estimated_weekly_reset(
            [self.seven(NOW - timedelta(days=9)), self.seven(NOW - timedelta(days=2))], NOW)
        self.assertEqual(estimate, NOW + timedelta(days=5))


class CompactTokenTests(unittest.TestCase):
    def test_token_totals_compact_at_thousand_and_million(self):
        cases = ((0, "0"), (999, "999"), (1000, "1k"), (120456, "120k"),
                 (999999, "999k"), (1000000, "1.0M"), (2100000, "2.1M"),
                 (2160000, "2.2M"), (12500000, "12.5M"))
        for count, expected in cases:
            with self.subTest(count=count):
                self.assertEqual(compact_tokens(count), expected)


class ActivityDetailCardTests(unittest.TestCase):
    """Phase 5 detail lines: estimates and activity before the Phase 4 trail."""

    def card(self, report, status="ok"):
        snapshot = SourceSnapshot("c", status, NOW, {"quota": report})
        return _account_card({"id": "c", "name": "Claude", "type": "claude"}, snapshot, NOW)

    def test_estimate_last_used_and_tokens_precede_the_classic_trail(self):
        windows = [window("five_hour", "5h", 12, NOW - timedelta(minutes=5),
                          NOW + timedelta(hours=4)),
                   window("seven_day", "Weekly", 34, NOW - 6 * DAY,
                          NOW - timedelta(hours=2), period=10080)]
        report = with_activity(trail_report(windows, seen=NOW - timedelta(minutes=10)),
                               activity(last_request_at=NOW - timedelta(hours=3)))
        detail = self.card(report).detail
        self.assertEqual(len(detail), 6)
        self.assertTrue(detail[0].startswith("5h reset:"))
        self.assertTrue(detail[1].startswith("Weekly passed"))
        self.assertEqual(detail[2], "Quota via export, 5 min old")
        self.assertEqual(detail[3], "Wk reset ~6 days (est)")
        self.assertEqual(detail[4], "Last used: 3h ago (VS Code)")
        self.assertEqual(detail[5], "Log tokens 5h/7d: 120k/2.1M")
        self.assertNotIn("Last reply: 10 min ago", detail)

    def test_a_future_real_reset_is_shown_as_today_not_estimated(self):
        windows = [window("five_hour", "5h", 12, NOW - timedelta(minutes=5),
                          NOW + timedelta(hours=4)),
                   window("seven_day", "Weekly", 34, NOW - 6 * DAY,
                          NOW + 2 * DAY, period=10080)]
        report = with_activity(trail_report(windows, seen=NOW - timedelta(minutes=10)),
                               activity(last_request_at=NOW - timedelta(hours=3)))
        detail = self.card(report).detail
        self.assertFalse(any("(est)" in line for line in detail), detail)
        self.assertEqual(detail[2:6], ["Quota via export, 5 min old",
                                       "Last used: 3h ago (VS Code)",
                                       "Log tokens 5h/7d: 120k/2.1M",
                                       "Reports: laptop 10 min ago"])

    def test_last_used_replaces_last_reply_only_when_the_reader_works(self):
        report = with_activity(trail_report(measured(24), seen=NOW - 6 * DAY,
                                            progress=NOW - timedelta(hours=1)),
                               activity(available=False))
        report.pop("warn_on_stale")
        detail = self.card(report).detail
        self.assertTrue(any(line.startswith("Last reply: 1 h ago") for line in detail), detail)
        self.assertFalse(any("Last used" in line for line in detail), detail)

    def test_no_activity_reading_says_unavailable_not_zero(self):
        report = with_activity(trail_report(measured(24), seen=NOW - 6 * DAY),
                               activity(last_request_at=None, requests=0,
                                        tokens_5h=0, tokens_7d=0))
        detail = self.card(report).detail
        self.assertIn("Last used: unavailable", detail)
        self.assertIn("Log tokens 5h/7d: 0/0", detail)

    def test_reset_lines_survive_the_six_line_cap_with_activity(self):
        windows = [window("w%d" % index, "W%d" % index, index * 10, NOW - timedelta(minutes=5),
                          NOW + timedelta(hours=5 + index), period=60)
                   for index in range(5)]
        detail = self.card(with_activity(trail_report(windows))).detail
        self.assertEqual(len(detail), 6)
        self.assertTrue(all(line.startswith("W") for line in detail[:5]))
        # The quota line now leads the trail lines, so "Last used" is the one
        # that the six-line cap drops.
        self.assertEqual(detail[5], "Quota via export, 5 min old")
        for dropped in ("Log tokens", "Last used", "Reports"):
            self.assertFalse(any(line.startswith(dropped) for line in detail), detail)

    def test_the_worst_token_totals_still_fit_the_labelled_line(self):
        report = with_activity(trail_report(measured(24), seen=NOW - 6 * DAY),
                               activity(tokens_5h=999999, tokens_7d=999900000))
        detail = self.card(report).detail
        self.assertIn("Log tokens 5h/7d: 999k/999.9M", detail)


class ScanningTokensTests(unittest.TestCase):
    """The token totals are lower bounds until the cold scan has finished."""

    def card(self, report):
        snapshot = SourceSnapshot("c", "ok", NOW, {"quota": report})
        return _account_card({"id": "c", "name": "Claude", "type": "claude"}, snapshot, NOW)

    def report(self, complete):
        return with_activity(trail_report(measured(24), seen=NOW - 6 * DAY),
                             activity(last_request_at=NOW - timedelta(hours=3),
                                      complete=complete))

    def test_scanning_line_while_the_cold_scan_is_incomplete(self):
        detail = self.card(self.report(False)).detail
        self.assertIn("Log tokens 5h/7d: scanning", detail)
        self.assertFalse(any(line.startswith("Log tokens 5h/7d: 120") for line in detail), detail)

    def test_the_numbers_appear_once_the_scan_is_complete(self):
        detail = self.card(self.report(True)).detail
        self.assertIn("Log tokens 5h/7d: 120k/2.1M", detail)
        self.assertFalse(any("scanning" in line for line in detail), detail)

    def test_the_scanning_line_fits_the_detail_limit(self):
        preview_dir = Path(__file__).resolve().parents[2] / "tools" / "presto_preview"
        sys.path.insert(0, str(preview_dir))
        from framebuffer import Framebuffer
        display = Framebuffer()
        display.set_font("bitmap6")
        available_width = display.width - (8 + 6) - 8
        for line in self.card(self.report(False)).detail:
            with self.subTest(line=line):
                self.assertLessEqual(len(line), DETAIL_LINE_CHARS, line)
                self.assertLessEqual(display.measure_text(line, 1), available_width, line)

    def test_an_unavailable_reader_keeps_the_phase_4_lines(self):
        report = with_activity(trail_report(measured(24), seen=NOW - 6 * DAY),
                               activity(available=False))
        detail = self.card(report).detail
        self.assertFalse(any("Log tokens" in line for line in detail), detail)


class QuotaAgeLineTests(unittest.TestCase):
    """"Quota via ..." needs a measured reading; a placeholder has no age."""

    def card(self, report):
        snapshot = SourceSnapshot("c", "ok", NOW, {"quota": report})
        return _account_card({"id": "c", "name": "Claude", "type": "claude"}, snapshot, NOW)

    def test_no_measured_window_omits_the_data_age_line(self):
        detail = self.card(trail_report(unmeasured(), seen=None)).detail
        self.assertFalse(any(line.startswith("Quota via") for line in detail), detail)
        self.assertTrue(any(line.startswith("Last reply") for line in detail), detail)

    def test_one_measured_window_shows_the_real_age(self):
        detail = self.card(trail_report(measured(6 * 24), seen=NOW - 6 * DAY)).detail
        self.assertIn("Quota via export, 6 days old", detail)

    def test_the_reader_on_variant_also_omits_the_fabricated_age(self):
        report = with_activity(trail_report(unmeasured(), seen=None),
                               activity(last_request_at=NOW - timedelta(hours=3)))
        detail = self.card(report).detail
        self.assertFalse(any(line.startswith("Quota via") for line in detail), detail)
        self.assertIn("Last used: 3h ago (VS Code)", detail)

    def test_the_reader_on_variant_shows_the_age_of_a_measured_window(self):
        report = with_activity(trail_report(measured(6 * 24), seen=NOW - 6 * DAY),
                               activity(last_request_at=NOW - timedelta(hours=3)))
        detail = self.card(report).detail
        self.assertIn("Quota via export, 6 days old", detail)


class CacheHintTests(unittest.TestCase):
    """The Phase 5d cache rules: the advice names /usage, not a terminal."""

    def cached(self, report, available=True, reason=None):
        """The usage_cache dict the usage cache reader puts into the report."""
        report["usage_cache"] = {"available": available, "reason": reason,
                                 "message": None, "path": "/home/you/.claude.json",
                                 "fetched_at": None, "credit": None,
                                 "account_changed": False, "refreshes": [],
                                 "since": None}
        return report

    def hint(self, report):
        return present_quota(report, NOW)["hint"]

    def fresh_windows(self):
        return [window("five_hour", "5h", 12, NOW - timedelta(minutes=5),
                       NOW + timedelta(hours=4)),
                window("seven_day", "Weekly", 34, NOW - timedelta(minutes=5),
                       NOW + DAY, period=10080)]

    def test_a_fresh_reading_needs_no_hint_with_the_cache_reader_on(self):
        report = self.cached(trail_report(self.fresh_windows()))
        self.assertIsNone(self.hint(report))

    def test_every_passed_reset_asks_for_the_usage_dialog(self):
        report = self.cached(trail_report(expired(measured(24))))
        self.assertEqual(self.hint(report), "Reset passed - open /usage")

    def test_a_stale_reading_names_its_age_and_the_usage_dialog(self):
        report = self.cached(trail_report(measured(6 * 24), seen=NOW - 6 * DAY))
        self.assertEqual(self.hint(report), "Quota 6d old - open /usage")

    def test_a_stale_reading_while_claude_is_in_use_keeps_the_old_wording(self):
        report = self.cached(with_activity(trail_report(measured(6 * 24),
                                                        seen=NOW - 6 * DAY)))
        report["local_activity"] = activity(last_request_at=NOW - timedelta(seconds=60))
        self.assertEqual(self.hint(report), "In use - quota 6d old")

    def test_the_used_ago_variant_is_replaced_only_with_the_cache_reader(self):
        # Without the cache reader the Phase 5 wording is untouched.
        report = with_activity(trail_report(measured(6 * 24), seen=NOW - 6 * DAY))
        report["local_activity"] = activity(last_request_at=NOW - timedelta(hours=3))
        self.assertEqual(self.hint(report), "Used 3h ago - quota 6d old")
        # With it, the age and the action replace it; the card keeps the detail.
        cached = self.cached(with_activity(trail_report(measured(6 * 24),
                                                         seen=NOW - 6 * DAY)))
        cached["local_activity"] = activity(last_request_at=NOW - timedelta(hours=3))
        self.assertEqual(self.hint(cached), "Quota 6d old - open /usage")

    def test_the_longest_age_keeps_the_action_and_the_longest_one_drops_it(self):
        oldest = [window("five_hour", "5h", 12, NOW - timedelta(days=400), NOW + DAY)]
        report = self.cached(trail_report(oldest))
        self.assertEqual(self.hint(report), "Quota 400d old - open /usage")
        self.assertEqual(len("Quota 400d old - open /usage"), 28)
        absurd = [window("five_hour", "5h", 12, NOW - timedelta(days=1000), NOW + DAY)]
        report = self.cached(trail_report(absurd))
        self.assertEqual(self.hint(report), "Quota 1000d old")

    def test_no_reading_yet_and_no_cache_file_asks_for_the_usage_dialog(self):
        report = self.cached(trail_report(unmeasured(), seen=None), available=False,
                              reason="no usage cache found")
        self.assertEqual(self.hint(report), "Run /usage in Claude Code")

    def test_a_heartbeat_does_not_hide_the_missing_cache_file(self):
        # The collector was seen, but no quota was ever measured and Claude
        # Code has not written its cache: opening /usage is still the action.
        report = self.cached(trail_report(unmeasured(), seen=NOW), available=False,
                              reason="no usage cache found")
        self.assertEqual(self.hint(report), "Run /usage in Claude Code")

    def test_a_cache_problem_keeps_the_existing_phase_four_hints(self):
        report = self.cached(trail_report(unmeasured(), seen=None), available=False,
                              reason="format changed")
        self.assertEqual(self.hint(report), HINTS["rule1"])
        report = self.cached(trail_report(measured(6 * 24), seen=NOW - 6 * DAY),
                              available=False, reason="access denied")
        self.assertEqual(self.hint(report), HINTS["rule4b"])

    def test_the_cache_reader_off_keeps_every_existing_hint(self):
        for windows, seen, expected in ((unmeasured(), None, HINTS["rule1"]),
                                        (unmeasured(), NOW, HINTS["rule2"]),
                                        (expired(measured(24)), NOW, HINTS["rule3"]),
                                        (measured(24), NOW - timedelta(minutes=5),
                                         HINTS["rule4a"]),
                                        (measured(6 * 24), NOW - 6 * DAY, HINTS["rule4b"])):
            report = trail_report(windows, seen=seen)
            with self.subTest(expected=expected):
                self.assertEqual(self.hint(report), expected)

    def test_every_cache_hint_is_ascii_and_within_28_characters(self):
        absurd = [window("five_hour", "5h", 12, NOW - timedelta(days=1000), NOW + DAY)]
        cases = (self.cached(trail_report(expired(measured(24)))),
                 self.cached(with_activity(trail_report(measured(6 * 24),
                                                        seen=NOW - 6 * DAY))),
                 self.cached(trail_report(measured(6 * 24), seen=NOW - 6 * DAY)),
                 self.cached(trail_report(absurd)),
                 self.cached(trail_report(unmeasured(), seen=None), available=False,
                             reason="no usage cache found"))
        for report in cases:
            hint = self.hint(report)
            with self.subTest(hint=hint):
                self.assertTrue(hint and hint.isascii(), hint)
                self.assertLessEqual(len(hint), 28, hint)


class CacheHintFontTests(unittest.TestCase):
    """The longest cache hints fit the Home note slot in the real font."""

    HINTS = ("Reset passed - open /usage", "Quota 6d old - open /usage",
             "Quota 400d old - open /usage", "Quota 1000d old",
             "In use - quota 2h old", "Run /usage in Claude Code")

    def test_every_cache_hint_fits_the_home_note_slot(self):
        preview_dir = Path(__file__).resolve().parents[2] / "tools" / "presto_preview"
        sys.path.insert(0, str(preview_dir))
        from framebuffer import Framebuffer

        display = Framebuffer()
        display.set_font("bitmap8")
        # The Home row is 240 wide: 14 for the marker, 8 of margin, 8 of slack.
        available = display.width - 14 - 8 - 8
        for hint in self.HINTS:
            with self.subTest(hint=hint):
                self.assertLessEqual(display.measure_text(hint, 1), available, hint)
                self.assertTrue(hint.isascii(), hint)
                self.assertLessEqual(len(hint), 28, hint)


if __name__ == "__main__":
    unittest.main()
