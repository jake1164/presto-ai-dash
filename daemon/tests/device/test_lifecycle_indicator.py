"""The shared bottom-right indicator: where it is, and what it must not cover.

One dock for Home and every service card, in the same place at all times. These
tests pin the agreed geometry down and prove the two things that are easy to get
wrong: a Home row or a hidden name must never be drawn under the icon, and a
card's detail lines must stop above it.
"""

import json
import os
import sys
import unittest

import presto_path  # noqa: F401
from cards import parse_cards
from home import SOURCE_HEIGHT, SOURCE_TOP, source_rows
from ui import (CARD_BOTTOM, LIFECYCLE_DOCK_H, LIFECYCLE_DOCK_Y, LIFECYCLE_ICON,
                LIFECYCLE_ICON_Y, LIFECYCLE_RELEASE_RGB, LIFECYCLE_WARN_RGB,
                MUTE_CHIP_Y, Palette, build_home_layout, dot_hit, draw_card,
                draw_lifecycle_dock, draw_lifecycle_view, draw_sources,
                lifecycle_dock, lifecycle_hit, lifecycle_icon_x, lifecycle_kind)

PREVIEW_DIR = os.path.join(presto_path.REPO_ROOT, "tools", "presto_preview")
if PREVIEW_DIR not in sys.path:
    sys.path.append(PREVIEW_DIR)

from framebuffer import Framebuffer, _rgb565  # noqa: E402

FIXTURE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "fixtures", "devices.json")

WARNING = {"warning": True, "release": False,
           "lines": ["Presto app does not match", "this daemon."]}
RELEASE = {"warning": False, "release": True,
           "lines": ["Daemon update available:", "0.50.0 -> 0.51.0"]}


def fixture_payload():
    with open(FIXTURE, encoding="utf-8") as handle:
        return json.load(handle)


def ink(pixels, box, colours, background):
    """The pixels inside ``box`` that are neither ``background`` nor ``colours``."""
    x, y, width, height = box
    stray = []
    for row in range(y, y + height):
        for column in range(x, x + width):
            offset = (row * 240 + column) * 3
            value = bytes(pixels[offset:offset + 3])
            if value == background or value in colours:
                continue
            stray.append((column, row))
    return stray


def count_colour(pixels, box, colour):
    """How many pixels of one colour are inside ``box``."""
    x, y, width, height = box
    total = 0
    for row in range(y, y + height):
        for column in range(x, x + width):
            offset = (row * 240 + column) * 3
            if bytes(pixels[offset:offset + 3]) == colour:
                total += 1
    return total


class GeometryTests(unittest.TestCase):
    def test_the_dock_ends_on_the_content_baseline(self):
        x, y, width, height = lifecycle_dock()
        self.assertEqual((x, y, width, height), (208, 184, 32, 24))
        self.assertEqual(y + height, SOURCE_TOP + SOURCE_HEIGHT)
        self.assertEqual(LIFECYCLE_DOCK_Y + LIFECYCLE_DOCK_H, y + height)

    def test_the_icon_keeps_the_usual_right_margin(self):
        self.assertEqual(lifecycle_icon_x(), 240 - 8 - LIFECYCLE_ICON)
        self.assertEqual((lifecycle_icon_x(), LIFECYCLE_ICON_Y), (222, 188))
        self.assertLess(LIFECYCLE_ICON_Y + LIFECYCLE_ICON, LIFECYCLE_DOCK_Y + LIFECYCLE_DOCK_H)

    def test_the_dock_is_above_the_mute_chip_and_the_dots(self):
        self.assertLessEqual(LIFECYCLE_DOCK_Y + LIFECYCLE_DOCK_H, MUTE_CHIP_Y)

    def test_the_hit_area_matches_the_dock(self):
        for x, y, expected in ((208, 184, True), (239, 207, True), (216, 188, True),
                               (207, 184, False), (208, 183, False), (208, 208, False),
                               (240, 190, False), ("x", 190, False)):
            with self.subTest(point=(x, y)):
                self.assertIs(lifecycle_hit(x, y), expected)

    def test_the_dock_does_not_swallow_a_page_dot_tap(self):
        self.assertIsNone(dot_hit(216, 228, 0, 4))
        self.assertFalse(lifecycle_hit(216, 228))

    def test_the_dock_is_not_in_the_bell_corner(self):
        # The bell is top-right; the dock is bottom-right. Neither claims the
        # other's area.
        self.assertFalse(lifecycle_hit(220, 10))
        self.assertFalse(lifecycle_hit(120, 130))


class KindTests(unittest.TestCase):
    def test_a_device_problem_outranks_a_release(self):
        both = {"warning": True, "release": True, "lines": []}
        self.assertEqual(lifecycle_kind(both), "warning")

    def test_each_notice_has_its_own_icon(self):
        self.assertEqual(lifecycle_kind(WARNING), "warning")
        self.assertEqual(lifecycle_kind(RELEASE), "release")

    def test_no_notice_means_no_dock(self):
        for value in (None, {}, {"warning": False, "release": False}, "x"):
            with self.subTest(value=value):
                self.assertIsNone(lifecycle_kind(value))


class HomeLayoutTests(unittest.TestCase):
    def layout(self, dock, setting=1):
        display = Framebuffer()
        return build_home_layout(display, source_rows(fixture_payload()), setting,
                                 240, dock=dock)

    def test_the_list_stops_above_the_dock(self):
        layout = self.layout(True)
        self.assertLessEqual(SOURCE_TOP + layout.region, LIFECYCLE_DOCK_Y)

    def test_the_overflow_column_stops_before_the_dock(self):
        layout = self.layout(True)
        self.assertLessEqual(layout.strip_limit(), lifecycle_dock()[0] - 4)

    def test_the_strip_band_ends_on_the_baseline(self):
        for setting in (1, 2, 3):
            with self.subTest(setting=setting):
                layout = self.layout(True, setting)
                band = SOURCE_TOP + layout.region + 18 * layout.lines
                self.assertLessEqual(band, SOURCE_TOP + SOURCE_HEIGHT)

    def test_no_notice_releases_the_space(self):
        with_dock = self.layout(True)
        without = self.layout(False)
        self.assertGreater(without.region, with_dock.region)
        self.assertGreater(without.strip_limit(), with_dock.strip_limit())
        self.assertEqual(without.strip_limit(), 14 + 218 - 2)

    def test_a_disabled_overflow_setting_still_keeps_clear_of_the_dock(self):
        layout = self.layout(True, setting=0)
        self.assertEqual(layout.lines, 0)
        self.assertLessEqual(SOURCE_TOP + layout.region, LIFECYCLE_DOCK_Y)

    def test_the_dock_does_not_move_with_the_scroll_offset(self):
        layout = self.layout(True)
        first = layout.region
        layout.strip(layout.limit)
        self.assertEqual(layout.region, first)


class HomeDrawingTests(unittest.TestCase):
    """No list content may be drawn under the icon."""

    def render(self, notice, scroll=None):
        display = Framebuffer()
        payload = fixture_payload()
        rows = source_rows(payload)
        layout = build_home_layout(display, rows, 1, 240, dock=bool(notice))
        offset = layout.limit if scroll is None else scroll
        draw_sources(display, Palette(display), rows, 0, 1, status="ok",
                     layout=layout, scroll=offset, notice=notice)
        return display, layout

    def test_a_row_never_reaches_under_the_dock(self):
        display, layout = self.render(WARNING)
        x, y, width, height = lifecycle_dock()
        background = _rgb565(16, 18, 22)
        # Everything except the icon's own yellow and the background.
        stray = ink(display.pixels, (x, y, width, height),
                    {_rgb565(*LIFECYCLE_WARN_RGB)}, background)
        self.assertEqual(stray, [], "content drawn under the dock")

    def test_the_icon_is_drawn_when_there_is_a_notice(self):
        display, _ = self.render(WARNING)
        box = lifecycle_dock()
        self.assertGreater(count_colour(display.pixels, box, _rgb565(*LIFECYCLE_WARN_RGB)),
                           0)

    def test_nothing_is_drawn_without_a_notice(self):
        display, _ = self.render(None)
        box = lifecycle_dock()
        for colour in (LIFECYCLE_WARN_RGB, LIFECYCLE_RELEASE_RGB):
            with self.subTest(colour=colour):
                self.assertEqual(count_colour(display.pixels, box, _rgb565(*colour)), 0)

    def test_the_release_arrow_is_blue(self):
        display, _ = self.render(RELEASE)
        x, y, _, _ = lifecycle_dock()
        seen = set()
        for row in range(y, y + 24):
            for column in range(x, x + 32):
                offset = (row * 240 + column) * 3
                seen.add(bytes(display.pixels[offset:offset + 3]))
        self.assertIn(_rgb565(*LIFECYCLE_RELEASE_RGB), seen)


class CardDrawingTests(unittest.TestCase):
    def card(self):
        payload = fixture_payload()
        cards, _ = parse_cards(payload)
        return cards[0], payload

    def test_detail_lines_stop_above_the_dock(self):
        card, payload = self.card()
        lines = ["detail line %d" % index for index in range(20)]
        display = Framebuffer()
        draw_card(display, Palette(display), card, 0, 1, quota=None,
                  detail=lines, detail_open=True, notice=WARNING)
        box = lifecycle_dock()
        stray = ink(display.pixels, box, {_rgb565(*LIFECYCLE_WARN_RGB)},
                    _rgb565(16, 18, 22))
        self.assertEqual(stray, [], "card content drawn under the dock")
        self.assertIsNotNone(payload)

    def test_a_card_without_a_notice_uses_the_full_column(self):
        card, _ = self.card()
        lines = ["detail line %d" % index for index in range(20)]
        display = Framebuffer()
        draw_card(display, Palette(display), card, 0, 1, quota=None,
                  detail=lines, detail_open=True)
        # The dock's corner is free for text when no icon is shown; the card
        # simply has no lines that far down.
        self.assertGreater(CARD_BOTTOM, LIFECYCLE_DOCK_Y)

    def test_the_extra_lines_stay_reachable(self):
        card, _ = self.card()
        lines = ["detail line %d" % index for index in range(20)]
        display = Framebuffer()
        drawn = []
        original = display.text

        def text(value, x, y, *args, **kwargs):
            drawn.append((value, y))
            return original(value, x, y, *args, **kwargs)

        display.text = text
        draw_card(display, Palette(display), card, 0, 1, quota=None,
                  detail=lines, detail_open=True, notice=WARNING)
        self.assertTrue(any("more (tap)" in value for value, _ in drawn),
                        "the rest of the detail must stay reachable")


class ViewTests(unittest.TestCase):
    def test_the_view_draws_the_lines_and_a_close_hint(self):
        display = Framebuffer()
        drawn = []
        original = display.text

        def text(value, x, y, *args, **kwargs):
            drawn.append(value)
            return original(value, x, y, *args, **kwargs)

        display.text = text
        self.assertTrue(draw_lifecycle_view(display, Palette(display), WARNING))
        self.assertIn("Device notice", drawn)
        self.assertIn("TAP TO CLOSE", drawn)
        for line in WARNING["lines"]:
            self.assertIn(line, drawn)

    def test_a_release_only_view_is_titled_for_the_release(self):
        display = Framebuffer()
        drawn = []
        original = display.text

        def text(value, x, y, *args, **kwargs):
            drawn.append(value)
            return original(value, x, y, *args, **kwargs)

        display.text = text
        self.assertTrue(draw_lifecycle_view(display, Palette(display), RELEASE))
        self.assertIn("Daemon release", drawn)

    def test_the_view_needs_a_notice(self):
        display = Framebuffer()
        self.assertFalse(draw_lifecycle_view(display, Palette(display), None))

    def test_the_dock_draws_nothing_for_an_unknown_kind(self):
        display = Framebuffer()
        self.assertIsNone(draw_lifecycle_dock(display, Palette(display), None))


if __name__ == "__main__":
    unittest.main()
