"""Regression checks for single-row text on the 240px Presto framebuffer."""

import unittest

import presto_path  # noqa: F401
from cards import Card, parse_cards
from net import embedded_payload
from ui import Palette, _text, draw_card, INK


class Display:
    def __init__(self, width=240):
        self.width = width
        self.font = "bitmap8"
        self.rows = []
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


if __name__ == "__main__":
    unittest.main()
