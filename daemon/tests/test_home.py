"""Quota-preview semantics, actual app input handling, and layout bounds."""

import importlib.util
import os
import sys
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import presto_path
from datetime import datetime, timedelta, timezone
from home import (quota_row_height, DEMO_QUOTAS, HomeScroll, LOCAL_ROW, ROW_HEIGHT, SOURCE_HEIGHT, SOURCE_ROW,
                  SOURCE_TOP, STRIP_LINE_HEIGHT, alert_settings, binding_window,
                  home_total_height, quota_label, quota_width, scroll_limit, source_rows)
from net import embedded_payload
from presto_ai_dash.models import SourceSnapshot
from presto_ai_dash.quotas import quota_report, quota_window
from test_management import ManagementBase
from test_ui import Display
from ui import (AMBER, TRACK, DAILY, WEEKLY, INK_DIM, Palette, build_home_layout, draw_home,
                draw_sources)


HOME_NOW = datetime(2026, 10, 18, 12, tzinfo=timezone.utc)


def iso_past(**delta):
    """An ISO instant before HOME_NOW for source_rows fixtures."""
    return (HOME_NOW - timedelta(**delta)).isoformat()


class QuotaTests(unittest.TestCase):
    def test_demo_daily_and_weekly_values(self):
        self.assertEqual([row["name"] for row in DEMO_QUOTAS],
                         ["OpenAI", "Claude", "Ollama Cloud", "OpenCode.ai"])
        self.assertEqual([row["daily"] for row in DEMO_QUOTAS], [62, 84, 93, 27])
        self.assertEqual([row["weekly"] for row in DEMO_QUOTAS], [38, 68, 79, 54])

    def test_missing_or_invalid_usage_is_not_zero(self):
        for value in (None, "62", True, -1, float("nan"), float("inf")):
            with self.subTest(value=value):
                self.assertEqual(quota_label(value), "--")
                self.assertEqual(quota_width(value, 218), 0)
        self.assertEqual(quota_label(0), "0%")

    def test_threshold_boundaries_and_over_limit(self):
        for value, expected in ((0, 0), (70, 70), (90, 90), (100, 100), (125, 100)):
            self.assertEqual(quota_width(value, 100), expected)
        self.assertEqual(quota_label(125), "125%")

    def test_width_fills_only_used_portion(self):
        for percent in range(101):
            self.assertEqual(quota_width(percent, 218), round(218 * percent / 100))


class HomeLayoutTests(unittest.TestCase):
    def assert_layout(self, display):
        boxes = []
        for row, clip in zip(display.rows, display.row_clips):
            text, x, y, wrap, scale, font = row
            display.set_font(font)
            width = display.measure_text(text, scale)
            height = (8 if font == "bitmap8" else 6) * scale
            self.assertLessEqual(width, wrap, text)
            self.assertLessEqual(x + width, display.width - 8, text)
            if clip:
                bottom = min(y + height, clip[1] + clip[3])
                y = max(y, clip[1])
                height = bottom - y
                if height <= 0:
                    continue
            self.assertLess(y + height, 240, text)
            for previous, px, py, pw, ph in boxes:
                intersects = x < px + pw and px < x + width and y < py + ph and py < y + height
                self.assertFalse(intersects, (text, previous))
            boxes.append((text, x, y, width, height))

    def test_both_periods_fit_without_text_overlap(self):
        for offset, visible in ((0, DEMO_QUOTAS[:3]), (50, DEMO_QUOTAS[1:])):
            display = Display()
            draw_home(display, Palette(display), DEMO_QUOTAS, 0, 3, scroll=offset)
            self.assert_layout(display)
            texts = [row[0] for row in display.rows]
            self.assertIn("Home", texts)
            self.assertIn("DEMO", texts)
            for quota in visible:
                self.assertIn(quota["name"], texts)
                self.assertIn("D " + quota_label(quota["daily"]), texts)
                self.assertIn("W " + quota_label(quota["weekly"]), texts)
            self.assertIsNone(display.clip)

    def test_mid_drag_is_clipped_below_pinned_header(self):
        display = Display()
        draw_home(display, Palette(display), DEMO_QUOTAS, 0, 3, scroll=25)
        self.assert_layout(display)
        self.assertIn((14, 56, 218, 150), display.row_clips)
        self.assertIsNone(display.row_clips[0])
        self.assertIsNone(display.row_clips[-1])

    def test_stacked_lanes_keep_both_periods_visible(self):
        for daily, weekly in ((62, 38), (38, 62), (62, 62), (None, 62), (62, None)):
            with self.subTest(daily=daily, weekly=weekly):
                display = Display()
                draw_home(display, Palette(display),
                          [{"name": "Test", "daily": daily, "weekly": weekly}], 0, 3)
                bars = [(pen, rect) for pen, rect in display.rectangles if rect[1] >= 56 and rect[0] == 14]
                self.assertEqual(bars[:2], [(TRACK, (14, 76, 218, 6)),
                                            (TRACK, (14, 84, 218, 6))])
                fills = bars[2:]
                expected = []
                if daily is not None:
                    expected.append((DAILY, (14, 76, quota_width(daily, 218), 6)))
                if weekly is not None:
                    expected.append((WEEKLY, (14, 84, quota_width(weekly, 218), 6)))
                self.assertEqual(fills, expected)

    def test_unknown_usage_and_muted_footer_fit(self):
        rows = [dict(quota) for quota in DEMO_QUOTAS]
        rows[2]["daily"] = None
        display = Display()
        draw_home(display, Palette(display), rows, 0, 3, muted=True)
        self.assert_layout(display)
        self.assertIn("D --", [row[0] for row in display.rows])
        self.assertIn("MUTED", [row[0] for row in display.rows])


class AppHomeTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location(
            "device_app_for_test", os.path.join(presto_path.PRESTO_DIR, "main.py"))
        self.module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {"config": SimpleNamespace()}):
            spec.loader.exec_module(self.module)
        self.device = SimpleNamespace(display=Display(),
                                      touch=SimpleNamespace(state=False, x=0, y=0,
                                                            poll=lambda: None),
                                      update=lambda: None)
        self.app = self.module.App(self.device)
        with patch.object(self.module, "_now_ms", return_value=0), \
                patch.object(self.module, "_ticks_diff", side_effect=lambda a, b: a - b):
            self.app._set_payload(embedded_payload())

    def touch(self, state, x, y, moment):
        self.device.touch.state = state
        self.device.touch.x = x
        self.device.touch.y = y
        with patch.object(self.module, "_now_ms", return_value=moment), \
                patch.object(self.module, "_ticks_diff", side_effect=lambda a, b: a - b):
            self.app._handle_gestures(self.app._poll_touch())

    def test_home_is_first_and_renders_demo(self):
        self.app._render()
        self.assertEqual(self.app.index, 0)
        self.assertIn("Home", [row[0] for row in self.device.display.rows])
        self.assertIn("DEMO", [row[0] for row in self.device.display.rows])

    def test_home_tap_keeps_both_periods_visible(self):
        self.touch(True, 170, 40, 0)
        self.touch(False, 0, 0, 80)
        self.app._render()
        texts = [row[0] for row in self.device.display.rows]
        self.assertIn("D 62%", texts)
        self.assertIn("W 38%", texts)
        self.assertEqual(self.app.index, 0)
        self.assertFalse(self.app.detail_open)

    def test_swipe_left_goes_to_existing_ollama_card(self):
        self.touch(True, 200, 120, 0)
        self.touch(True, 50, 120, 80)
        self.touch(False, 0, 0, 120)
        self.assertEqual(self.app._current_card().id, "ollama")

    def test_swipe_right_wraps_to_existing_litellm_card(self):
        self.touch(True, 50, 120, 0)
        self.touch(True, 200, 120, 80)
        self.touch(False, 0, 0, 120)
        self.assertEqual(self.app._current_card().id, "litellm")

    def test_vertical_drag_follows_finger_and_reveals_fourth_demo(self):
        self.app._render()
        self.assertFalse(self.app._needs_redraw)
        self.touch(True, 120, 170, 0)
        self.assertFalse(self.app._needs_redraw)
        self.touch(True, 120, 145, 100)
        self.assertTrue(self.app._needs_redraw)
        self.assertEqual(self.app.home_scroll.offset, 25)
        self.touch(True, 120, 90, 200)
        self.touch(False, 0, 0, 300)
        self.assertEqual(self.app.home_scroll.offset, 50)
        self.assertEqual(self.app.index, 0)
        self.assertFalse(self.app.detail_open)
        self.assertFalse(self.app.alerts.muted)
        self.app._render()
        self.assertIn("OpenCode.ai", [row[0] for row in self.device.display.rows])
        self.touch(True, 120, 100, 600)
        self.touch(True, 120, 200, 700)
        self.touch(False, 0, 0, 800)
        self.assertEqual(self.app.home_scroll.offset, 0)

    def test_horizontal_swipe_preserves_list_position(self):
        self.app.home_scroll.offset = 50
        self.touch(True, 200, 120, 0)
        self.touch(True, 50, 120, 80)
        self.touch(False, 0, 0, 120)
        self.assertEqual(self.app._current_card().id, "ollama")
        self.assertEqual(self.app.home_scroll.offset, 50)


class HomeHintTests(unittest.TestCase):
    """Phase 4: the daemon's hint takes the reset-note slot until data is fresh."""

    def rows_for(self, hint, windows, status="warning"):
        payload = {"updated_at": HOME_NOW.isoformat(), "card_order": "accounts",
                   "sources": [{"id": "c", "type": "claude", "name": "Claude",
                                "status": status,
                                "quota": {"windows": windows, "hint": hint}}]}
        return source_rows(payload)

    def window(self, label, period, percent, state, minutes_ago=0,
               reset_days=None, reset_minutes=None):
        """A window as the payload carries it: days/minutes ahead may be negative."""
        reset = None
        if reset_days is not None:
            reset = (HOME_NOW + timedelta(days=reset_days)).isoformat()
        elif reset_minutes is not None:
            reset = (HOME_NOW + timedelta(minutes=reset_minutes)).isoformat()
        return {"label": label, "period_minutes": period, "used_percent": percent,
                "state": state, "resets_at": reset,
                "observed_at": iso_past(minutes=minutes_ago)}

    def unmeasured(self):
        return [self.window("5h", 300, None, "unavailable"),
                self.window("Weekly", 10080, None, "unavailable")]

    def stale_unexpired(self):
        # Both windows were read six days ago; their resets are still ahead.
        return [self.window("5h", 300, 12, "stale", minutes_ago=6 * 24 * 60, reset_days=7),
                self.window("Weekly", 10080, 34, "stale", minutes_ago=6 * 24 * 60,
                            reset_days=9)]

    def expired(self):
        return [self.window("5h", 300, 12, "stale", minutes_ago=6 * 24 * 60, reset_days=-1),
                self.window("Weekly", 10080, 34, "stale", minutes_ago=6 * 24 * 60,
                            reset_days=-2)]

    def fresh(self):
        return [self.window("5h", 300, 12, "available", minutes_ago=5, reset_minutes=240),
                self.window("Weekly", 10080, 34, "available", minutes_ago=90, reset_days=7)]

    def drawn(self, row):
        display = Display()
        draw_sources(display, Palette(display), [row], 0, 3, status="warning")
        HomeLayoutTests().assert_layout(display)
        texts = [text for text, *_ in display.rows]
        self.assertTrue(all(ord(char) < 128 for text in texts for char in text), texts)
        return texts

    def test_source_rows_carry_the_hint(self):
        for hint in (None, "Run claude in a terminal"):
            self.assertEqual(self.rows_for(hint, self.unmeasured())[0]["hint"], hint)

    def test_five_hint_states_replace_the_reset_note(self):
        cases = (("Run claude in a terminal", self.unmeasured()),
                 ("Waiting for a Claude reply", self.unmeasured()),
                 ("Awaiting refresh", self.expired()),
                 ("Reply in claude to refresh", self.stale_unexpired()),
                 ("No report 6d - run claude", self.stale_unexpired()))
        for hint, windows in cases:
            with self.subTest(hint=hint):
                texts = self.drawn(self.rows_for(hint, windows)[0])
                self.assertIn(hint, texts)
                self.assertNotIn("last known", texts)
                self.assertFalse(any(" resets " in text for text in texts))

    def test_expired_window_shows_dash_and_an_empty_bar_never_the_old_value(self):
        row = self.rows_for("Awaiting refresh", self.expired())[0]
        self.assertEqual([w["expired"] for w in row["windows"]], [True, True])
        texts = self.drawn(row)
        self.assertEqual(texts.count("--"), 2)
        self.assertNotIn("12%", texts)
        self.assertNotIn("34%", texts)
        self.assertNotIn("0%", texts)
        display = Display()
        draw_sources(display, Palette(display), [row], 0, 3)
        bars = [pen for pen, rect in display.rectangles if rect[2] > 20]
        self.assertEqual(set(bars), {TRACK})  # tracks only, never a filled bar

    def test_mixed_expired_only_the_stale_window_goes_unknown(self):
        windows = [self.window("5h", 300, 34, "stale", minutes_ago=3 * 24 * 60,
                               reset_days=-1),
                   self.window("Weekly", 10080, 21, "available", minutes_ago=5,
                               reset_days=7)]
        row = self.rows_for(None, windows, status="ok")[0]
        self.assertEqual(row["binding"], 1)
        texts = self.drawn(row)
        self.assertEqual(texts.count("--"), 1)
        self.assertIn("21%", texts)
        self.assertNotIn("34%", texts)
        self.assertTrue(any(text.startswith("Wk resets ") for text in texts), texts)
        self.assertFalse(any("last known" in text for text in texts))

    def test_expired_window_does_not_choose_note_over_stale_unexpired_window(self):
        windows = [self.window("5h", 300, 88, "stale", minutes_ago=3 * 24 * 60,
                               reset_days=-1),
                   self.window("Weekly", 10080, 21, "stale", minutes_ago=6 * 24 * 60,
                               reset_days=2)]
        row = self.rows_for(None, windows, status="warning")[0]
        self.assertEqual(row["binding"], 1)
        texts = self.drawn(row)
        self.assertTrue(any(text.startswith("last known ") for text in texts), texts)
        self.assertFalse(any(" resets " in text for text in texts))

    def test_all_expired_without_hint_draws_no_reset_note(self):
        texts = self.drawn(self.rows_for(None, self.expired())[0])
        self.assertFalse(any(" resets " in text or text.startswith("last known") or
                             text == "awaiting refresh" for text in texts), texts)

    def test_hint_takes_note_slot_even_when_a_window_is_fresh(self):
        texts = self.drawn(self.rows_for("Check Claude", self.fresh(), status="warning")[0])
        self.assertIn("Check Claude", texts)
        self.assertFalse(any(" resets " in text for text in texts))

    def test_fresh_window_keeps_todays_reset_note_without_a_hint(self):
        row = self.rows_for(None, self.fresh(), status="ok")[0]
        self.assertEqual(row["binding"], 1)
        texts = self.drawn(row)
        self.assertTrue(any(text.startswith("Wk resets") for text in texts))
        self.assertNotIn("last known", texts)
        self.assertIn("12%", texts)

    def test_hint_wins_the_name_line_when_they_cannot_both_fit(self):
        row = self.rows_for("No report 6d - run claude", self.stale_unexpired())[0]
        row["name"] = "Claude personal account"
        texts = self.drawn(row)
        self.assertTrue(texts[0].endswith("~"))  # the hint wins; the name clips
        self.assertIn("No report 6d - run claude", texts)

    def test_the_cache_hints_are_drawn_in_full(self):
        cases = ("Reset passed - open /usage", "Quota 6d old - open /usage",
                 "Quota 400d old - open /usage", "Quota 1000d old",
                 "In use - quota 2h old", "Run /usage in Claude Code")
        for hint in cases:
            with self.subTest(hint=hint):
                texts = self.drawn(self.rows_for(hint, self.stale_unexpired())[0])
                self.assertIn(hint, texts)
                self.assertFalse(any(" resets " in text for text in texts))


class HomeResetBadgeTests(unittest.TestCase):
    """Phase 9: a small badge counts the banked resets on the Home row."""

    def payload(self, reset_credits, name="Codex", hint=None, windows=None):
        if windows is None:
            windows = [{"label": "Weekly", "period_minutes": 10080, "used_percent": 14,
                        "state": "available",
                        "resets_at": (HOME_NOW + timedelta(days=7)).isoformat(),
                        "observed_at": iso_past(minutes=5)}]
        quota = {"windows": windows, "hint": hint}
        if reset_credits is not None:
            quota["reset_credits"] = reset_credits
        return {"updated_at": HOME_NOW.isoformat(), "card_order": "accounts",
                "sources": [{"id": "c", "type": "codex", "name": name,
                             "status": "ok", "quota": quota}]}

    def row(self, *args, **kwargs):
        return source_rows(self.payload(*args, **kwargs))[0]

    def drawn(self, row):
        display = Display()
        draw_sources(display, Palette(display), [row], 0, 3, status="ok")
        HomeLayoutTests().assert_layout(display)
        return display

    def badge_row(self, display):
        found = [index for index, entry in enumerate(display.rows)
                 if entry[0].startswith("R") and entry[0][1:2].isdigit()]
        self.assertEqual(len(found), 1, display.rows)
        return found[0]

    def test_no_badge_when_the_count_is_not_reported(self):
        self.assertIsNone(self.row({"available": None, "expires_at": []})["reset_badge"])
        self.assertIsNone(self.row(None)["reset_badge"])

    def test_no_badge_for_zero_resets(self):
        self.assertIsNone(self.row({"available": 0, "expires_at": []})["reset_badge"])

    def test_a_dim_badge_counts_the_resets(self):
        badge = self.row({"available": 3, "expires_at": []})["reset_badge"]
        self.assertEqual(badge["text"], "R3")
        self.assertFalse(badge["amber"])

    def test_amber_within_seventy_two_hours(self):
        expiry = (HOME_NOW + timedelta(hours=72)).isoformat()
        badge = self.row({"available": 3, "expires_at": [expiry]})["reset_badge"]
        self.assertEqual(badge["text"], "R3 3d")
        self.assertTrue(badge["amber"])

    def test_dim_beyond_seventy_two_hours(self):
        expiry = (HOME_NOW + timedelta(hours=73)).isoformat()
        badge = self.row({"available": 3, "expires_at": [expiry]})["reset_badge"]
        self.assertEqual(badge["text"], "R3")
        self.assertFalse(badge["amber"])

    def test_the_time_left_uses_the_home_units(self):
        expiry = (HOME_NOW + timedelta(hours=5)).isoformat()
        badge = self.row({"available": 2, "expires_at": [expiry]})["reset_badge"]
        self.assertEqual(badge["text"], "R2 5h")
        expiry = (HOME_NOW + timedelta(days=2)).isoformat()
        badge = self.row({"available": 2, "expires_at": [expiry]})["reset_badge"]
        self.assertEqual(badge["text"], "R2 2d")

    def test_the_earliest_expiry_decides(self):
        expiry = (HOME_NOW + timedelta(days=4)).isoformat()
        soon = (HOME_NOW + timedelta(hours=10)).isoformat()
        badge = self.row({"available": 3, "expires_at": [expiry, soon]})["reset_badge"]
        self.assertEqual(badge["text"], "R3 10h")
        self.assertTrue(badge["amber"])

    def test_the_badge_is_drawn_dim_when_nothing_expires_soon(self):
        display = self.drawn(self.row({"available": 3, "expires_at": []}))
        index = self.badge_row(display)
        self.assertEqual(display.rows[index][0], "R3")
        self.assertEqual(display.text_pens[index], INK_DIM)

    def test_the_badge_is_amber_within_seventy_two_hours(self):
        expiry = (HOME_NOW + timedelta(hours=40)).isoformat()
        display = self.drawn(self.row({"available": 3, "expires_at": [expiry]}))
        index = self.badge_row(display)
        self.assertEqual(display.rows[index][0], "R3 40h")
        self.assertEqual(display.text_pens[index], AMBER)

    def test_no_badge_is_drawn_for_zero_or_unknown(self):
        for credits in ({"available": 0, "expires_at": []},
                         {"available": None, "expires_at": []}):
            with self.subTest(credits=credits):
                display = self.drawn(self.row(credits))
                self.assertFalse(any(entry[0].startswith("R") for entry in display.rows))

    def test_the_badge_is_skipped_when_the_name_fills_the_row(self):
        row = self.row({"available": 3, "expires_at": []},
                       name="A very long Codex account name here")
        display = self.drawn(row)
        self.assertFalse(any(entry[0].startswith("R3") for entry in display.rows))

    def test_the_hint_keeps_the_space(self):
        row = self.row({"available": 3, "expires_at": []}, name="Codex personal",
                       hint="No report 6d - run claude")
        display = self.drawn(row)
        texts = [entry[0] for entry in display.rows]
        self.assertIn("No report 6d - run claude", texts)
        self.assertFalse(any(entry[0].startswith("R3") for entry in display.rows))


class HomeOrderTests(unittest.TestCase):
    """Home follows the saved account order, like the swipe cards do."""

    def window(self, percent=14):
        return {"label": "Weekly", "period_minutes": 10080, "used_percent": percent,
                "state": "available",
                "resets_at": (HOME_NOW + timedelta(days=3)).isoformat(),
                "observed_at": iso_past(minutes=5)}

    def payload(self, order, card_order="accounts"):
        """A status payload whose sources are quota accounts or local ones."""
        sources = []
        for name, kind in order:
            if kind == "quota":
                # The author's own services: Go reports three windows, Claude two.
                count = {"Go": 3, "Claude": 2}.get(name, 1)
                sources.append({"id": name.lower(), "type": "claude", "name": name,
                                "status": "ok",
                                "quota": {"windows": [self.window() for _ in range(count)]}})
            else:
                sources.append({"id": name.lower(), "type": "litellm", "name": name,
                                "status": "ok",
                                "metrics": {"requests_today": 12, "tokens_today": 340}})
        payload = {"updated_at": HOME_NOW.isoformat(), "sources": sources}
        if card_order is not None:
            payload["card_order"] = card_order
        return payload

    def drawn(self, order, scroll=0, card_order="accounts"):
        rows = source_rows(self.payload(order, card_order))
        display = Display()
        draw_sources(display, Palette(display), rows, 0, 3, scroll=scroll)
        HomeLayoutTests().assert_layout(display)
        return rows, display

    def name_positions(self, display, names):
        """The y of every account name in the list, in the order drawn.

        The overflow strip repeats the names of the hidden rows, so only the
        names drawn inside the list clip are counted.
        """
        found = []
        for index, (text, x, y, wrap, scale, font) in enumerate(display.rows):
            if text in names and display.row_clips[index] is not None:
                found.append((text, y))
        return found

    def test_the_home_rows_follow_the_saved_order(self):
        cases = [
            [("Ollama", "local"), ("Codex", "quota"), ("LiteLLM", "local"), ("Claude", "quota")],
            [("Ollama", "local"), ("LiteLLM", "local"), ("Claude", "quota")],
            [("Codex", "quota"), ("Ollama", "local"), ("Claude", "quota"), ("LiteLLM", "local")],
            [("Codex", "quota"), ("Claude", "quota")],
            [("Ollama", "local"), ("LiteLLM", "local")],
            [("Ollama", "local")],
        ]
        for order in cases:
            names = [name for name, _ in order]
            with self.subTest(order=names):
                rows, display = self.drawn(order)
                self.assertEqual([row["name"] for row in rows], names)
                found = self.name_positions(display, names)
                self.assertEqual([text for text, _ in found], names)
                positions = [y for _, y in found]
                self.assertEqual(positions, sorted(positions))

    def test_a_local_service_among_quota_rows_is_one_compact_line(self):
        _, display = self.drawn([("Ollama", "local"), ("Codex", "quota")])
        texts = [text for text, *_ in display.rows]
        self.assertIn("12 req / 340 tok", texts)      # as much as fits on the one line
        self.assertNotIn("OK", texts)                 # a healthy service shows its usage
        found = self.name_positions(display, ["Ollama", "Codex"])
        self.assertEqual([text for text, _ in found], ["Ollama", "Codex"])
        # The local row keeps its own place, right under the quota row above it.
        self.assertEqual(found[1][1], SOURCE_TOP + LOCAL_ROW)
        self.assertLess(found[1][1] - found[0][1], SOURCE_ROW)

    def test_an_all_local_list_keeps_the_two_line_rows(self):
        _, display = self.drawn([("Ollama", "local"), ("LiteLLM", "local")])
        texts = [text for text, *_ in display.rows]
        self.assertIn("OK", texts)                  # the status word
        self.assertIn("12 req / 340 tok", texts)    # the whole usage line
        found = self.name_positions(display, ["Ollama", "LiteLLM"])
        self.assertEqual(found[1][1] - found[0][1], SOURCE_ROW)

    def test_a_legacy_payload_keeps_the_quota_rows_first(self):
        order = [("Ollama", "local"), ("Codex", "quota")]
        rows = source_rows(self.payload(order, card_order=None))
        self.assertEqual([row["name"] for row in rows], ["Codex", "Ollama"])

    def mixed_order(self):
        """Six accounts: four with quota windows and two local services."""
        return [("Codex", "quota"), ("Ollama", "local"), ("Claude", "quota"),
                ("Go", "quota"), ("Ollama Cloud", "quota"), ("LiteLLM", "local")]

    def test_the_scroll_bound_uses_the_total_height_of_the_rows(self):
        order = self.mixed_order()
        rows, _ = self.drawn(order)
        quota_rows = [row for row in rows if "windows" in row]
        total = sum(quota_row_height(row) for row in quota_rows) + 2 * LOCAL_ROW
        self.assertEqual(total, 209)  # 38 + 45 + 52 + 38, and two one-line rows
        self.assertEqual(home_total_height(rows), total)
        # The list is taller than the list area, so the overflow strip takes
        # one line of it and the list scrolls behind the strip.
        layout = build_home_layout(Display(), rows, 1)
        self.assertEqual(layout.lines, 1)
        self.assertEqual(layout.region, SOURCE_HEIGHT - STRIP_LINE_HEIGHT)
        limit = scroll_limit(total, layout.region)
        self.assertEqual(layout.limit, limit)
        self.assertGreater(limit, 0)
        for offset in (0, limit // 2, limit):
            with self.subTest(offset=offset):
                self.drawn(order, scroll=offset)
        names = [name for name, _ in order]
        _, display = self.drawn(order, scroll=limit)
        found = self.name_positions(display, names)
        self.assertEqual(found[-1][0], names[-1])
        # The last row is fully inside the list at the bottom of the scroll.
        self.assertGreaterEqual(found[-1][1], SOURCE_TOP)
        self.assertLessEqual(found[-1][1] + 8, SOURCE_TOP + layout.region)
        # One pixel past the limit does not scroll further.
        _, beyond = self.drawn(order, scroll=limit + 1)
        self.assertEqual(self.name_positions(beyond, names)[-1], found[-1])

    def test_the_scrollbar_thumb_follows_the_total_height(self):
        order = self.mixed_order()
        rows, _ = self.drawn(order)
        total = home_total_height(rows)
        layout = build_home_layout(Display(), rows, 1)
        limit = scroll_limit(total, layout.region)
        _, display = self.drawn(order, scroll=limit)
        thumbs = [args for pen, args in display.rectangles
                  if pen == INK_DIM and args[0] == 235 and args[2] == 2]
        self.assertEqual(len(thumbs), 1)
        x, y, width, height = thumbs[0]
        self.assertEqual(height, max(12, layout.region * layout.region // total))
        self.assertLessEqual(y + height, SOURCE_TOP + layout.region)

    def test_the_list_is_clipped_while_its_rows_are_drawn(self):
        order = self.mixed_order()
        rows, display = self.drawn(order, scroll=10)
        layout = build_home_layout(Display(), rows, 1)
        self.assertTrue(display.rows)
        for index, entry in enumerate(display.rows):
            clip = display.row_clips[index]
            if clip is None:
                # The overflow strip is drawn outside the list clip.
                self.assertGreaterEqual(entry[2], SOURCE_TOP + layout.region, entry[0])
                continue
            self.assertEqual(clip, (14, SOURCE_TOP, 218, layout.region), entry[0])


class HomeOrderEndToEndTests(ManagementBase):
    """A real account reorder moves the Home rows as it moves the cards."""

    def accounts(self):
        """Three quota accounts and two local services, saved in this order."""
        self.disable_fake()
        rows = []
        for index, (name, kind) in enumerate((("Ollama", "local"), ("Codex", "quota"),
                                              ("LiteLLM", "local"), ("Claude", "quota"),
                                              ("Go", "quota"))):
            if kind == "quota":
                row = self.manager.save({
                    "type": "claude", "name": name, "enabled": True,
                    "config": {"settings_path": str(Path(self.temp.name) / ("settings-%d.json" % index))}})
                report = quota_report("claude", name, [
                    quota_window("a", "Weekly", 14, HOME_NOW + timedelta(days=3), HOME_NOW,
                                 period_minutes=10080)])
                self.storage.save_snapshot(
                    SourceSnapshot(row["id"], "ok", metrics={"quota": report}))
            else:
                row = self.manager.save({"type": "litellm", "name": name, "enabled": True})
                self.storage.save_snapshot(
                    SourceSnapshot(row["id"], "ok", metrics={"requests_today": 12}))
            rows.append(row)
        return rows

    def test_home_follows_a_real_account_reorder(self):
        rows = self.accounts()
        original = [row["id"] for row in self.manager.list_sources()["sources"]]
        wanted = [rows[3]["id"], rows[0]["id"], rows[4]["id"], rows[2]["id"], rows[1]["id"]]
        order = wanted + [key for key in original if key not in wanted]
        self.manager.reorder({"order": order, "expected_order": original})
        payload = self.manager.status_payload()
        self.assertEqual(payload["card_order"], "accounts")
        card_names = [card["title"] for card in payload["cards"][1:]]
        self.assertEqual(card_names, ["Claude", "Ollama", "Go", "LiteLLM", "Codex"])
        home_rows = source_rows(payload)
        self.assertEqual([row["name"] for row in home_rows], card_names)
        display = Display()
        draw_sources(display, Palette(display), home_rows, 0, len(payload["cards"]))
        HomeLayoutTests().assert_layout(display)
        found = [(text, y) for text, x, y, wrap, scale, font in display.rows
                 if text in card_names]
        self.assertEqual([text for text, _ in found], card_names)
        positions = [y for _, y in found]
        self.assertEqual(positions, sorted(positions))


class HomeScrollTests(unittest.TestCase):
    def test_list_size_controls_scroll_bounds(self):
        self.assertEqual([scroll_limit(n * ROW_HEIGHT) for n in (0, 2, 3, 4, 8)],
                         [0, 0, 0, 50, 250])

    def test_short_list_cannot_scroll(self):
        scroll = HomeScroll(2 * ROW_HEIGHT)
        scroll.update(True, 100, 180)
        scroll.update(True, 100, 80)
        self.assertEqual(scroll.offset, 0)

    def test_drag_clamps_at_both_ends(self):
        scroll = HomeScroll(4 * ROW_HEIGHT)
        scroll.update(True, 100, 180)
        scroll.update(True, 100, -100)
        self.assertEqual(scroll.offset, 50)
        scroll.update(False, 100, -100)
        scroll.update(True, 100, 70)
        scroll.update(True, 100, 300)
        self.assertEqual(scroll.offset, 0)

    def test_header_drag_and_disabled_card_do_not_scroll(self):
        for y, enabled in ((20, True), (120, False)):
            scroll = HomeScroll(4 * ROW_HEIGHT)
            scroll.update(True, 100, y, enabled)
            scroll.update(True, 100, y - 100, enabled)
            self.assertEqual(scroll.offset, 0)

    def test_horizontal_axis_stays_locked_despite_later_vertical_movement(self):
        scroll = HomeScroll(4 * ROW_HEIGHT)
        scroll.update(True, 100, 180)
        scroll.update(True, 120, 180)
        scroll.update(True, 120, 80)
        self.assertEqual(scroll.offset, 0)


if __name__ == "__main__":
    unittest.main()


class LocalPartsTests(unittest.TestCase):
    def parts(self, metrics):
        row = source_rows({"sources": [{"id": "x", "type": "litellm", "name": "X",
                                        "status": "ok", "metrics": metrics}]})[0]
        return row["parts"]

    def test_litellm_says_requests_tokens_spend_and_errors(self):
        self.assertEqual(self.parts({"requests_today": 5, "tokens_today": 340,
                                     "spend_today": 0.1234, "errors_today": 2}),
                         ["5 req", "340 tok", "$0.12", "2 err"])

    def test_zero_spend_and_no_errors_are_not_shown(self):
        self.assertEqual(self.parts({"requests_today": 5, "spend_today": 0,
                                     "errors_today": 0}), ["5 req"])

    def test_ollama_says_the_model_and_its_speed(self):
        self.assertEqual(self.parts({"loaded_model": "llama3.2:3b",
                                     "last_measured_tokens_per_sec": 41.26}),
                         ["llama3.2:3b", "41.3 t/s"])

    def test_an_idle_ollama_says_how_many_models(self):
        self.assertEqual(self.parts({"available_models": 7}), ["7 models"])

    def test_unavailable_markers_and_booleans_never_show(self):
        self.assertEqual(self.parts({"requests_today": "unavailable", "spend_today": True,
                                     "errors_today": "unavailable",
                                     "available_models": False}), [])

    def test_a_long_model_name_is_cut(self):
        self.assertEqual(len(self.parts({"loaded_model": "m" * 60})[0]), 16)


class RowAtTests(unittest.TestCase):
    def layout(self):
        rows = source_rows({"sources": [
            {"id": "q%d" % i, "type": "claude", "name": "Q%d" % i, "status": "ok",
             "quota": {"windows": [{"label": "Weekly", "period_minutes": 10080,
                                    "used_percent": 5, "state": "available",
                                    "resets_at": "2026-10-25T12:00:00+00:00",
                                    "observed_at": "2026-10-20T11:55:00+00:00"}]}}
            for i in range(5)] + [{"id": "l", "type": "litellm", "name": "L", "status": "ok",
                                   "metrics": {"requests_today": 1}}]})
        from home import HomeLayout
        return HomeLayout(rows, 1, lambda text: len(text) * 6, 200)

    def test_the_row_under_a_touch_follows_heights_and_scroll(self):
        from home import row_at
        layout = self.layout()
        height = layout.rows[0][1]
        self.assertEqual(row_at(layout, 0, 10)["id"], "q0")
        self.assertEqual(row_at(layout, 0, 4 + height)["id"], "q1")        # first pixel of row 1
        self.assertEqual(row_at(layout, 0, 4 + height - 1)["id"], "q0")    # last pixel of row 0
        self.assertEqual(row_at(layout, height, 10)["id"], "q1")           # scrolled one row
        self.assertEqual(row_at(layout, layout.limit, 4 + layout.region - 5)["id"], "l")

    def test_outside_the_list_region_is_not_a_row(self):
        from home import row_at
        layout = self.layout()
        self.assertIsNone(row_at(layout, 0, 2))                        # above the list
        self.assertIsNone(row_at(layout, 0, 4 + layout.region))        # the strip band
        self.assertIsNone(row_at(layout, 0, 235))                      # the footer


class AlertSettingsTests(unittest.TestCase):
    """The alert settings the daemon sends, and what the device does with them."""

    def test_the_defaults_are_used_when_the_key_is_missing(self):
        self.assertEqual(alert_settings({}),
                         {"quiet": False, "sound": False, "pop_to_top": True,
                          "enabled": True, "mute_hours": 4})

    def test_the_settings_are_read(self):
        payload = {"alerts": {"quiet": True, "sound": True, "pop_to_top": False,
                              "enabled": False, "mute_hours": 12}}
        self.assertEqual(alert_settings(payload),
                         {"quiet": True, "sound": True, "pop_to_top": False,
                          "enabled": False, "mute_hours": 12})

    def test_junk_is_ignored(self):
        for payload in ({"alerts": "nonsense"}, {"alerts": None}, {"alerts": []},
                        {"alerts": {"quiet": "yes", "sound": 1, "pop_to_top": None,
                                    "enabled": "no"}},
                        {"alerts": {"quiet": 1}}, "nonsense", None, 5):
            with self.subTest(payload=payload):
                self.assertEqual(alert_settings(payload),
                                 {"quiet": False, "sound": False, "pop_to_top": True,
                                  "enabled": True, "mute_hours": 4})

    def test_the_sound_is_off_unless_the_daemon_says_otherwise(self):
        """A missing, junk or old-daemon payload can never make the device chirp."""
        for payload in ({}, {"alerts": {}}, {"alerts": {"sound": None}},
                        {"alerts": {"sound": "yes"}}, {"alerts": {"sound": 1}}):
            with self.subTest(payload=payload):
                self.assertFalse(alert_settings(payload)["sound"])

    def test_the_mute_length_is_only_used_when_it_can_be(self):
        """A real whole number of hours, from 0 to 48; anything else is the default."""
        for value in (0, 1, 4, 12, 48):
            with self.subTest(value=value):
                payload = {"alerts": {"mute_hours": value}}
                self.assertEqual(alert_settings(payload)["mute_hours"], value)
        for value in (-1, 49, 100, "4", 4.5, None, True, False, [4]):
            with self.subTest(value=value):
                payload = {"alerts": {"mute_hours": value}}
                self.assertEqual(alert_settings(payload)["mute_hours"], 4)
