"""Phase 4: freshness hints at payload-build time and the detail-card trail."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from presto_ai_dash.claude_export import build_envelope
from presto_ai_dash.dashboard import _account_card, build_instance_payload
from presto_ai_dash.ingest import ingest
from presto_ai_dash.management import ManagedCollector
from presto_ai_dash.models import SourceSnapshot
from presto_ai_dash.quotas import (age_label, claude_hint, compact_age,
                                  present_quota, quota_report)
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
        self.assertEqual(card.detail[2], "Data age: 5 min")
        self.assertEqual(card.detail[3], "Last reply: 1 h ago")
        self.assertEqual(card.detail[4], "Reports: laptop 10 min ago")
        self.assertEqual(card.primary, "5h: 12%")

    def test_missing_progress_and_collector_are_honest_not_zero(self):
        card = self.card(self.fresh_windows(), seen=None, progress=None)
        self.assertEqual(card.detail[2], "Data age: 5 min")
        self.assertEqual(card.detail[3], "Last reply: unavailable")
        self.assertEqual(card.detail[4], "Reports: never")

    def test_six_line_cap_drops_the_priorities_from_the_bottom(self):
        windows = [window("w%d" % index, "W%d" % index, index * 10, NOW - timedelta(minutes=5),
                          NOW + timedelta(hours=5 + index), period=60)
                   for index in range(5)]
        card = self.card(windows)
        self.assertEqual(len(card.detail), 6)
        self.assertTrue(all(line.startswith("W") for line in card.detail[:5]))
        self.assertEqual(card.detail[5], "Data age: 5 min")
        self.assertNotIn("Last reply: unavailable", card.detail)
        self.assertNotIn("Reports: never", card.detail)

    def test_reports_without_the_trail_keep_the_existing_card(self):
        report = quota_report("opencode", "Go", self.fresh_windows())
        snapshot = SourceSnapshot("c", "ok", NOW, {"quota": report})
        with patch("presto_ai_dash.dashboard.now_utc", return_value=NOW):
            card = _account_card({"id": "c", "name": "Go", "type": "opencode_go"}, snapshot)
        self.assertEqual(len(card.detail), 2)
        self.assertTrue(all(line.endswith("Z") for line in card.detail))
        self.assertFalse(any(line.startswith("Data age") for line in card.detail))

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
                    self.assertLessEqual(len(line), 30, line)
                if name == "both-resets-passed":
                    self.assertTrue(any(line.endswith("16:00Z") for line in lines), lines)
                if name == "long-window-label":
                    self.assertTrue(any(line.endswith("16:00Z") for line in lines), lines)


if __name__ == "__main__":
    unittest.main()
