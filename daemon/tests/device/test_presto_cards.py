"""Cards v2: the hierarchy, the quota rows, the stale chip and the ticks.

A card used to be a column of same-size grey capitals, with "(stale)" on
every line, absolute reset times, and a weekly/monthly line that was cut
off. These tests pin the new layout down: dim labels and bright values,
staleness said once, the unavailable lines folded into one, the relative
reset with the absolute time after it, the quota rows drawn from the
structured data, the ticks only when the daemon sends the two percentages,
and nothing overflowing or overlapping on any card.
"""

import json
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

import presto_path  # noqa: F401
from cards import (format_booleans, format_money, is_unavailable_line,
                   parse_cards, tidy_line)
from home import source_rows
import ui
from ui import Palette, draw_card

PREVIEW_DIR = os.path.join(presto_path.REPO_ROOT, "tools", "presto_preview")
if PREVIEW_DIR not in sys.path:
    sys.path.append(PREVIEW_DIR)

from framebuffer import Framebuffer  # noqa: E402

FIXTURE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fixtures",
                       "devices.json")
NOW = datetime(2026, 10, 20, 12, tzinfo=timezone.utc)


class RecordingFramebuffer(Framebuffer):
    """The real bitmap fonts, and a record of every rectangle drawn."""

    def __init__(self, *args, **kwargs):
        Framebuffer.__init__(self, *args, **kwargs)
        self.rectangles = []
        self.calls = []

    def rectangle(self, x, y, w, h):
        self.rectangles.append((self.pen, (x, y, w, h)))
        Framebuffer.rectangle(self, x, y, w, h)

    def text(self, text, x, y, wrap=-1, scale=2, angle=0, spacing=1,
             fixed_width=False):
        self.calls.append((text, x, y, wrap, scale, self.font, self.pen))
        Framebuffer.text(self, text, x, y, wrap, scale, angle, spacing,
                         fixed_width)


def fixture_payload():
    with open(FIXTURE, encoding="utf-8") as handle:
        return json.load(handle)


def card_of(payload, card_id):
    cards, error = parse_cards(payload)
    assert error is None
    for card in cards:
        if card.id == card_id:
            return card
    raise AssertionError("no card " + card_id)


def row_of(payload, source_id):
    for row in source_rows(payload) or ():
        if row.get("id") == source_id:
            return row
    return None


def draw(payload, card_id, quota=True, **kwargs):
    """Draw one card with the real fonts and return the display."""
    card = card_of(payload, card_id)
    row = row_of(payload, card_id)
    display = RecordingFramebuffer()
    draw_card(display, Palette(display), card, 0, 7,
              quota=(row.get("windows") if quota and row else None),
              marks=(row.get("marks") if quota and row else None), **kwargs)
    return display


def texts(display):
    return [call[0] for call in display.calls]


def boxes(display):
    """The box of every drawn text, with the real font metrics."""
    result = []
    for text, x, y, wrap, scale, font, _pen in display.calls:
        display.font = font
        width = display.measure_text(text, scale)
        height = (8 if font == "bitmap8" else 6) * scale
        result.append((text, x, y, width, height))
    return result


class CardTextTests(unittest.TestCase):
    """The text of a card, before it is drawn."""

    def test_money_has_two_decimals(self):
        self.assertEqual(format_money("spend $0.5000"), "spend $0.50")
        self.assertEqual(format_money("$12.5"), "$12.50")
        self.assertEqual(format_money("$100"), "$100")
        self.assertEqual(format_money("$0.0000"), "$0.00")

    def test_a_small_amount_is_not_rounded_away(self):
        self.assertEqual(format_money("$0.000020"), "$0.00002")

    def test_a_boolean_is_a_word(self):
        self.assertEqual(format_booleans("Database: true"), "Database: ok")
        self.assertEqual(format_booleans("Database: false"), "Database: no")
        self.assertEqual(format_booleans("Reachable: yes"), "Reachable: yes")

    def test_the_stale_marker_is_not_on_the_lines(self):
        self.assertEqual(tidy_line("5h: 12% (stale)"), "5h: 12%")

    def test_the_line_is_ascii(self):
        self.assertEqual(tidy_line("5 available · version 0.12.1"),
                         "5 available - version 0.12.1")

    def test_an_unavailable_line_is_recognised(self):
        self.assertTrue(is_unavailable_line("Secondary: unavailable"))
        self.assertFalse(is_unavailable_line("Secondary reset: 10-25 12:00Z"))


class CardLayoutTests(unittest.TestCase):
    """Every card draws inside the screen, without overlaps."""

    def assert_card_fits(self, payload, card_id, **kwargs):
        display = draw(payload, card_id, **kwargs)
        self.assertTrue(display.calls)
        for text, x, y, wrap, scale, font, _pen in display.calls:
            display.font = font
            self.assertLessEqual(display.measure_text(text, scale) + 1, wrap,
                                 "%r wraps" % text)
            self.assertLessEqual(x + display.measure_text(text, scale),
                                 display.width - 8, "%r is too wide" % text)
            self.assertTrue(all(ord(char) < 128 for char in text), repr(text))
        drawn = boxes(display)
        for index, (text, x, y, width, height) in enumerate(drawn):
            self.assertLessEqual(y + height, 210, "%r is below the card" % text)
            for other, ox, oy, ow, oh in drawn[index + 1:]:
                overlap = (x < ox + ow and ox < x + width and
                           y < oy + oh and oy < y + height)
                self.assertFalse(overlap, "%r overlaps %r" % (text, other))
        return display

    def test_every_card_type_fits(self):
        payload = fixture_payload()
        for card_id in ("dash", "go", "cx", "cl", "oc", "ol", "li"):
            with self.subTest(card=card_id):
                self.assert_card_fits(payload, card_id)

    def test_a_long_name_and_long_values_fit(self):
        payload = fixture_payload()
        payload["sources"][2]["name"] = "A very long account name indeed"
        for source in payload["sources"]:
            for win in source.get("quota", {}).get("windows", []):
                win["label"] = "A very long window label"
        payload["cards"][3]["title"] = "A very long service title indeed"
        payload["cards"][3]["primary"] = "12345% (stale)"
        payload["cards"][3]["secondary"] = "A very long secondary line of text"
        for card_id in ("go", "cl"):
            with self.subTest(card=card_id):
                self.assert_card_fits(payload, card_id)

    def test_the_bars_never_cover_the_text(self):
        payload = fixture_payload()
        display = draw(payload, "cl")
        for _text, x, y, width, height in boxes(display):
            for _pen, (rx, ry, rw, rh) in display.rectangles:
                if rw < 100 or rh > 8:
                    continue  # not a bar: the clear, a chip or a marker
                overlap = (x < rx + rw and rx < x + width and
                           y < ry + rh and ry < y + height)
                self.assertFalse(overlap, "a bar covers the text")


class StaleChipTests(unittest.TestCase):
    """Staleness is said once, as a chip beside the status."""

    def test_the_stale_chip_is_drawn_once(self):
        payload = fixture_payload()
        display = draw(payload, "cl")
        stale = [text for text in texts(display) if text == "STALE"]
        self.assertEqual(len(stale), 1)

    def test_no_line_says_stale(self):
        payload = fixture_payload()
        display = draw(payload, "cl")
        for text in texts(display):
            if text == "STALE":
                continue
            self.assertNotIn("stale", text.lower(), text)

    def test_a_fresh_card_has_no_stale_chip(self):
        payload = fixture_payload()
        for source in payload["sources"]:
            for win in source.get("quota", {}).get("windows", []):
                if win.get("state") == "stale":
                    win["state"] = "available"
        for card in payload["cards"]:
            card["primary"] = card["primary"].replace(" (stale)", "")
            card["secondary"] = card["secondary"].replace(" (stale)", "")
        display = draw(payload, "cl")
        stale = [text for text in texts(display) if text == "STALE"]
        self.assertEqual(stale, [])


class UnavailableFoldingTests(unittest.TestCase):
    """The lines that say "unavailable" are folded into one dim line."""

    def test_the_secondary_window_becomes_one_dim_line(self):
        payload = fixture_payload()
        display = draw(payload, "cx")
        self.assertIn("no secondary window", texts(display))
        self.assertEqual(texts(display).count("no secondary window"), 1)

    def test_no_unavailable_line_is_left(self):
        payload = fixture_payload()
        display = draw(payload, "cx")
        for text in texts(display):
            self.assertNotIn("unavailable", text.lower(), text)


class QuotaCardTests(unittest.TestCase):
    """A quota card draws its windows; without them it falls back to text."""

    def test_the_windows_are_drawn_as_rows(self):
        payload = fixture_payload()
        display = draw(payload, "cl")
        self.assertIn("12%", texts(display))
        self.assertIn("66%", texts(display))
        self.assertIn("5h", texts(display))
        self.assertIn("Wk", texts(display))

    def test_the_reset_is_relative_with_the_absolute_time_after_it(self):
        payload = fixture_payload()
        display = draw(payload, "cl")
        self.assertIn("resets in 3h", texts(display))
        self.assertIn("10-20 15:00Z", texts(display))
        relative = [x for text, x, _y, _w, _s, _f, _p in display.calls
                    if text == "resets in 3h"][0]
        absolute = [x for text, x, _y, _w, _s, _f, _p in display.calls
                    if text == "10-20 15:00Z"][0]
        self.assertGreater(absolute, relative, "the absolute time comes after")

    def test_without_the_structured_data_the_card_falls_back_to_text(self):
        payload = fixture_payload()
        card = card_of(payload, "cl")
        display = RecordingFramebuffer()
        draw_card(display, Palette(display), card, 0, 7)
        self.assertIn(card.primary, texts(display))
        self.assertNotIn("12%", texts(display))

    def test_no_fact_is_lost(self):
        """Every line of the daemon's card is still on the screen."""
        payload = fixture_payload()
        # The summary card: the tiles are fetched separately, so the card
        # shows its lines.
        for card in payload["cards"]:
            card.pop("tiles", None)
        for card_id in ("go", "cx", "cl"):
            with self.subTest(card=card_id):
                card = card_of(payload, card_id)
                row = row_of(payload, card_id)
                display = draw(payload, card_id)
                shown = texts(display)
                labels = [str(window.get("label") or "").split(" ")[0]
                          for window in row["windows"]]
                for line in card.detail:
                    if line in shown:
                        continue
                    if line.endswith("unavailable"):
                        # Folded into the one dim line that says so.
                        self.assertTrue(any("window" in text for text in shown),
                                        line)
                        continue
                    # A per-window reset line: the quota row shows it, with
                    # the relative time as well.
                    self.assertTrue(any(line.startswith(label + " reset: ")
                                        for label in labels), line)
                    self.assertTrue(any("resets in" in text for text in shown))

    def test_the_values_are_bright_and_the_labels_are_dim(self):
        payload = fixture_payload()
        display = draw(payload, "cl")
        colours = {}
        for text, _x, _y, _w, _s, _f, pen in display.calls:
            colours[text] = pen
        self.assertNotEqual(colours.get("12%"), colours.get("5h"))


class TickMarkTests(unittest.TestCase):
    """The tick marks appear only when the daemon sends the two numbers."""

    def ticks(self, payload, card_id, marks=None):
        card = card_of(payload, card_id)
        row = row_of(payload, card_id)
        display = RecordingFramebuffer()
        draw_card(display, Palette(display), card, 0, 7,
                  quota=(row.get("windows") if row else None), marks=marks)
        bars = [rect for _pen, rect in display.rectangles
                if rect[2] > 200 and rect[3] <= 8]
        drawn = [rect for _pen, rect in display.rectangles
                 if rect[2] <= 2 and rect[3] > 2 and rect[0] > 20]
        return bars, drawn

    def test_the_marks_draw_two_ticks_per_bar(self):
        payload = fixture_payload()
        bars, marks = self.ticks(payload, "cl", [80, 95])
        self.assertEqual(len(bars), 2)
        self.assertEqual(len(marks), 2 * len(bars))

    def test_without_marks_there_are_no_ticks(self):
        payload = fixture_payload()
        _bars, marks = self.ticks(payload, "cl", None)
        self.assertEqual(marks, [])

    def test_junk_marks_draw_no_ticks(self):
        payload = fixture_payload()
        for junk in (["eighty", 95], [80], "80,95", [0, 0], [-5, 120], [80, "x"]):
            with self.subTest(marks=junk):
                _bars, marks = self.ticks(payload, "cl", junk)
                self.assertEqual(marks, [], junk)


if __name__ == "__main__":
    unittest.main()
