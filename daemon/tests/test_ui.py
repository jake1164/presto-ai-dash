"""Regression checks for single-row text on the 240px Presto framebuffer."""

import os
import sys
import unittest

import presto_path  # noqa: F401
from cards import Card, parse_cards
from home import source_rows
from net import embedded_payload
from ui import Palette, _text, draw_card, draw_sources, INK

PREVIEW_DIR = os.path.join(presto_path.REPO_ROOT, "tools", "presto_preview")
if PREVIEW_DIR not in sys.path:
    sys.path.append(PREVIEW_DIR)

from framebuffer import Framebuffer  # noqa: E402


class Display:
    def __init__(self, width=240):
        self.width = width
        self.font = "bitmap8"
        self.rows = []
        self.text_pens = []
        self.rectangles = []
        self.pen = None
        self.clip = None
        self.row_clips = []

    def get_bounds(self):
        return self.width, 240

    def create_pen(self, *rgb):
        return rgb

    def set_pen(self, pen):
        self.pen = pen

    def clear(self):
        self.rows = []
        self.text_pens = []
        self.row_clips = []
        self.rectangles = []

    def set_clip(self, x, y, width, height):
        self.clip = (x, y, width, height)

    def remove_clip(self):
        self.clip = None

    def rectangle(self, *args):
        self.rectangles.append((self.pen, args))

    def circle(self, *args):
        pass

    def set_font(self, font):
        self.font = font

    def measure_text(self, text, scale):
        # Variable glyph widths catch the old assumption that character counts fit.
        return sum(3 if char in " il:." else 6 for char in text) * scale

    def text(self, text, x, y, wrap, scale):
        self.rows.append((text, x, y, wrap, scale, self.font))
        self.text_pens.append(self.pen)
        self.row_clips.append(self.clip)


class TextLayoutTests(unittest.TestCase):
    def assert_rows_fit(self, display):
        previous_bottom = -1
        for text, x, y, wrap, scale, font in display.rows:
            display.set_font(font)
            self.assertLessEqual(display.measure_text(text, scale), wrap, text)
            self.assertLessEqual(x + wrap, display.width - 8)
            self.assertNotIn("\n", text)
            self.assertGreaterEqual(y, previous_bottom, text)
            previous_bottom = y + (8 if font == "bitmap8" else 6) * scale

    def test_sample_cards_do_not_wrap_or_overlap(self):
        cards, error = parse_cards(embedded_payload())
        self.assertIsNone(error)
        for index, card in enumerate(cards):
            with self.subTest(card=card.id):
                display = Display()
                draw_card(display, Palette(display), card, index, len(cards))
                self.assert_rows_fit(display)

    def test_long_rows_and_newlines_stay_within_bounds(self):
        display = Display()
        card = Card("dash", "W" * 80, "near_limit", "9" * 80,
                    "W" * 80 + "\nwrapped", ["W" * 80] * 6)
        draw_card(display, Palette(display), card, 0, 3)
        self.assert_rows_fit(display)
        self.assertEqual(display.rows[2][4], 2)
        self.assertTrue(display.rows[2][0].endswith("~"))

    def test_short_metric_keeps_large_type(self):
        display = Display()
        _text(display, Palette(display), "38 tok/s", 14, 48, INK,
              scale=3, min_scale=2)
        self.assertEqual(display.rows[0][0], "38 tok/s")
        self.assertEqual(display.rows[0][4], 3)

    def test_fit_uses_display_width(self):
        display = Display(width=160)
        _text(display, Palette(display), "W" * 30, 14, 48, INK)
        text, _, _, wrap, scale, _ = display.rows[0]
        self.assertEqual(wrap, 138)
        self.assertLessEqual(display.measure_text(text, scale), wrap)


class HomeRowTextTests(unittest.TestCase):
    """The compact local row keeps its name and its right-hand detail apart."""

    def rows(self, name="Local service", status="ok"):
        payload = {"updated_at": "2026-10-20T12:00:00+00:00", "card_order": "accounts",
                   "sources": [
                       {"id": "cloud", "type": "claude", "name": "Cloud", "status": "ok",
                        "quota": {"windows": [{"label": "Weekly", "period_minutes": 10080,
                                               "used_percent": 14, "state": "available",
                                               "resets_at": "2026-10-25T12:00:00+00:00",
                                               "observed_at": "2026-10-20T11:55:00+00:00"}]}},
                       {"id": "local", "type": "litellm", "name": name, "status": status,
                        "metrics": {"requests_today": 12, "tokens_today": 340}}]}
        return source_rows(payload)

    def test_the_name_makes_room_for_the_detail(self):
        display = Display()
        draw_sources(display, Palette(display), self.rows(), 0, 3)
        drawn = {text: (x, y) for text, x, y, wrap, scale, font in display.rows}
        self.assertIn("Local service", drawn)
        self.assertIn("12 req", drawn)
        name_x, name_y = drawn["Local service"]
        detail_x, detail_y = drawn["12 req"]
        self.assertEqual(name_y, detail_y)
        display.set_font("bitmap8")
        self.assertLessEqual(name_x + display.measure_text("Local service", 1), detail_x)

    def test_a_long_name_is_shortened_instead_of_overlapping(self):
        display = Display()
        draw_sources(display, Palette(display),
                     self.rows(name="A very long local service name indeed"), 0, 3)
        texts = [text for text, *_ in display.rows]
        self.assertTrue(any(text.endswith("~") for text in texts), texts)
        drawn = {text: (x, y) for text, x, y, wrap, scale, font in display.rows}
        name = [text for text in texts if text.endswith("~")][0]
        name_x, name_y = drawn[name]
        detail_x, detail_y = drawn["12 req"]
        self.assertEqual(name_y, detail_y)
        display.set_font("bitmap8")
        self.assertLessEqual(name_x + display.measure_text(name, 1), detail_x)

    def test_an_unhealthy_service_shows_its_status_instead_of_its_usage(self):
        display = Display()
        draw_sources(display, Palette(display), self.rows(status="offline"), 0, 3)
        texts = [text for text, *_ in display.rows]
        self.assertIn("OFFLINE", texts)
        self.assertNotIn("12 req", texts)


class FirmwareTextFitTests(unittest.TestCase):
    """The firmware wraps a line whose last word does not fit.

    The real framebuffer uses the device's own font metrics, so this is the
    only check that sees the text the way the Presto draws it.
    """

    def rows(self):
        payload = {"updated_at": "2026-10-20T12:00:00+00:00", "card_order": "accounts",
                   "sources": [
                       {"id": "cloud", "type": "claude", "name": "Cloud account", "status": "ok",
                        "quota": {"windows": [{"label": "Weekly", "period_minutes": 10080,
                                               "used_percent": 14, "state": "available",
                                               "resets_at": "2026-10-25T12:00:00+00:00",
                                               "observed_at": "2026-10-20T11:55:00+00:00"}]}},
                       {"id": "local", "type": "litellm", "name": "Local service", "status": "ok",
                        "metrics": {"requests_today": 12, "tokens_today": 340}}]}
        return source_rows(payload)

    def test_no_home_line_wraps_onto_a_second_line(self):
        display = Framebuffer()
        calls = []
        original_text = display.text

        def record(text, x, y, wrap=-1, scale=2, angle=0, spacing=1, fixed_width=False):
            calls.append((text, x, y, wrap, scale, display.font))
            return original_text(text, x, y, wrap, scale, angle, spacing, fixed_width)

        display.text = record
        draw_sources(display, Palette(display), self.rows(), 0, 3)
        self.assertTrue(calls)
        for text, x, y, wrap, scale, font in calls:
            if " " not in text:
                continue  # one word is never wrapped
            display.font = font
            # The firmware draws a word when it fits; the measured width is
            # one pixel narrower than the drawn one.
            self.assertLessEqual(display.measure_text(text, scale) + 1, wrap,
                                 "%r at %d,%d" % (text, x, y))


if __name__ == "__main__":
    unittest.main()
