"""Quota-preview semantics, actual app input handling, and layout bounds."""

import importlib.util
import os
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import presto_path
from home import DEMO_QUOTAS, HomeScroll, quota_label, quota_width, scroll_limit
from net import embedded_payload
from test_ui import Display
from ui import PANEL, DAILY, WEEKLY, Palette, draw_home


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
                self.assertEqual(bars[:2], [(PANEL, (14, 76, 218, 6)),
                                            (PANEL, (14, 84, 218, 6))])
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
        with patch.object(self.module, "_now_ms", return_value=0):
            self.app._set_payload(embedded_payload())

    def touch(self, state, x, y, moment):
        self.device.touch.state = state
        self.device.touch.x = x
        self.device.touch.y = y
        with patch.object(self.module, "_now_ms", return_value=moment):
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


class HomeScrollTests(unittest.TestCase):
    def test_list_size_controls_scroll_bounds(self):
        self.assertEqual([scroll_limit(n) for n in (0, 2, 3, 4, 8)], [0, 0, 0, 50, 250])

    def test_short_list_cannot_scroll(self):
        scroll = HomeScroll(2)
        scroll.update(True, 100, 180)
        scroll.update(True, 100, 80)
        self.assertEqual(scroll.offset, 0)

    def test_drag_clamps_at_both_ends(self):
        scroll = HomeScroll(4)
        scroll.update(True, 100, 180)
        scroll.update(True, 100, -100)
        self.assertEqual(scroll.offset, 50)
        scroll.update(False, 100, -100)
        scroll.update(True, 100, 70)
        scroll.update(True, 100, 300)
        self.assertEqual(scroll.offset, 0)

    def test_header_drag_and_disabled_card_do_not_scroll(self):
        for y, enabled in ((20, True), (120, False)):
            scroll = HomeScroll(4)
            scroll.update(True, 100, y, enabled)
            scroll.update(True, 100, y - 100, enabled)
            self.assertEqual(scroll.offset, 0)

    def test_horizontal_axis_stays_locked_despite_later_vertical_movement(self):
        scroll = HomeScroll(4)
        scroll.update(True, 100, 180)
        scroll.update(True, 120, 180)
        scroll.update(True, 120, 80)
        self.assertEqual(scroll.offset, 0)


if __name__ == "__main__":
    unittest.main()
