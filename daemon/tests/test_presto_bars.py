"""Home bars, the card bell and the alerts-off mark: geometry and colour.

The bars of a Home row used to share a fixed area, so a three-window service
got thinner bars than a one-window one, on a track that was almost the same
colour as the background. These tests pin the numbers down: every bar is the
same thickness and the row is as tall as its bars need, the contrast of the
track, the colour of a stale window, the smallest fill of a measured value,
the space between two rows, and the size of the bell and of the alerts-off
mark.
"""

import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

import presto_path  # noqa: F401
from home import (BAR_GAP, BAR_H, ROW_GAP, ROW_TOP_H, SOURCE_TOP, bar_fill,
                  quota_row_height, source_rows)
from test_ui import Display
import ui
from ui import (ALERT_MARK, BACKGROUND, BELL_HIT, BELL_ICON, DAILY,
                INK, INK_DIM, LIMIT, MONTHLY, Palette, TRACK, WEEKLY,
                bell_hit, bar_heights, dim_rgb, draw_sources)

NOW = datetime(2026, 10, 20, 12, tzinfo=timezone.utc)


def relative_luminance(rgb):
    """The WCAG relative luminance of an sRGB colour."""
    channels = []
    for value in rgb:
        part = value / 255.0
        channels.append(part / 12.92 if part <= 0.03928
                        else ((part + 0.055) / 1.055) ** 2.4)
    return (0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2])


def contrast(first, second):
    """The WCAG contrast ratio between two colours."""
    one = relative_luminance(first)
    other = relative_luminance(second)
    lighter, darker = max(one, other), min(one, other)
    return (lighter + 0.05) / (darker + 0.05)


def window(label, period, percent, state="available", minutes_ago=1,
           reset_days=3):
    return {"label": label, "period_minutes": period, "used_percent": percent,
            "state": state, "resets_at": (NOW + timedelta(days=reset_days)).isoformat(),
            "observed_at": (NOW - timedelta(minutes=minutes_ago)).isoformat()}


def payload_for(windows, name="Service"):
    return {"updated_at": NOW.isoformat(), "card_order": "accounts",
            "sources": [{"id": "svc", "type": "claude", "name": name,
                         "status": "ok", "quota": {"windows": windows}}]}


def drawn_rows(windows, stale=False):
    display = Display()
    rows = source_rows(payload_for(windows))
    draw_sources(display, Palette(display), rows, 0, 3, status="ok", stale=stale)
    return display


def window_list(count):
    labels = (("5h", 300, 12), ("Weekly", 10080, 34), ("Monthly", 43200, 5),
              ("Daily", 1440, 20), ("Extra", 600, 8), ("More", 700, 9))
    return [window(label, period, percent) for label, period, percent in labels[:count]]


class BarHeightTests(unittest.TestCase):
    """Every bar is the same thickness, whatever the service; the row grows to fit."""

    def test_one_two_three_and_four_windows_get_the_same_six_pixel_bar(self):
        self.assertEqual(BAR_H, 6)
        for count in (1, 2, 3, 4):
            with self.subTest(count=count):
                self.assertEqual(bar_heights(count), [BAR_H] * count)

    def test_many_windows_thin_out_together_and_stay_equal(self):
        for count in (5, 6, 8):
            with self.subTest(count=count):
                heights = bar_heights(count)
                self.assertEqual(len(set(heights)), 1)
                self.assertLess(heights[0], BAR_H)
                self.assertGreaterEqual(heights[0], 2)

    def test_the_row_is_as_tall_as_its_bars(self):
        for count, expected in ((1, 40), (2, 47), (3, 54)):
            with self.subTest(count=count):
                row = source_rows(payload_for(window_list(count)))[0]
                self.assertEqual(quota_row_height(row), expected)
                self.assertEqual(expected, ROW_TOP_H + count * BAR_H + (count - 1) * BAR_GAP
                                 + ROW_GAP)

    def test_a_row_with_nothing_measured_keeps_room_for_its_line_of_text(self):
        row = source_rows(payload_for([window("Weekly", 10080, None, "unavailable")]))[0]
        self.assertEqual(quota_row_height(row), ROW_TOP_H + 10 + ROW_GAP)

    def test_every_bar_on_the_screen_is_the_same_height(self):
        display = Display()
        sources = []
        for index, count in enumerate((1, 2, 3)):
            sources.append({"id": "s%d" % index, "type": "claude", "name": "Svc%d" % count,
                            "status": "ok", "quota": {"windows": window_list(count)}})
        rows = source_rows({"updated_at": NOW.isoformat(), "card_order": "accounts",
                            "sources": sources})
        draw_sources(display, Palette(display), rows, 0, 3, status="ok")
        tracks = [rect for pen, rect in display.rectangles if pen == TRACK and rect[2] > 20]
        self.assertEqual(len(tracks), 1 + 2 + 3)
        self.assertEqual({rect[3] for rect in tracks}, {BAR_H})


class TrackContrastTests(unittest.TestCase):
    """The unfilled track has to be visible against the background."""

    def test_the_track_is_at_least_one_and_a_half_to_one(self):
        self.assertGreaterEqual(contrast(TRACK, BACKGROUND), 1.5)

    def test_the_old_panel_colour_would_not_have_passed(self):
        """The fix is real: the colour it replaces is too dark to see."""
        self.assertLess(contrast(ui.PANEL, BACKGROUND), 1.5)


class StaleHueTests(unittest.TestCase):
    """A stale window keeps its own hue, dimmed, instead of going grey."""

    def bars(self, display):
        return [(pen, rect) for pen, rect in display.rectangles
                if rect[2] > 20 and rect[1] > 4]

    def test_a_stale_window_is_a_dimmed_cyan_and_a_dimmed_purple(self):
        display = drawn_rows([window("5h", 300, 12, "stale", minutes_ago=60),
                               window("Weekly", 10080, 34, "stale", minutes_ago=60)],
                              stale=False)
        fills = [pen for pen, _rect in self.bars(display)
                 if pen not in (TRACK,)]
        self.assertIn(dim_rgb(DAILY), fills)
        self.assertIn(dim_rgb(WEEKLY), fills)

    def test_a_stale_window_is_not_grey(self):
        display = drawn_rows([window("5h", 300, 12, "stale", minutes_ago=60),
                               window("Weekly", 10080, 34, "stale", minutes_ago=60)])
        fills = [pen for pen, _rect in self.bars(display) if pen != TRACK]
        self.assertNotIn(INK_DIM, fills)
        self.assertEqual(len(set(fills)), 2, "two windows must not share one colour")

    def test_a_fresh_window_keeps_the_full_colour(self):
        display = drawn_rows([window("5h", 300, 12), window("Weekly", 10080, 34)])
        fills = [pen for pen, _rect in self.bars(display) if pen != TRACK]
        self.assertIn(DAILY, fills)
        self.assertIn(WEEKLY, fills)


class MinimumFillTests(unittest.TestCase):
    """A measured value above zero is never an invisible bar."""

    def test_a_tiny_reading_still_fills_three_pixels(self):
        self.assertEqual(bar_fill(1, 200), 3)
        self.assertEqual(bar_fill(3, 200), 6)

    def test_zero_fills_nothing(self):
        self.assertEqual(bar_fill(0, 200), 0)

    def test_an_unknown_value_fills_nothing(self):
        for value in (None, "12", True, -1):
            self.assertEqual(bar_fill(value, 200), 0)

    def test_a_full_bar_is_the_whole_track(self):
        self.assertEqual(bar_fill(100, 200), 200)

    def test_the_bar_of_a_row_uses_the_minimum_fill(self):
        display = drawn_rows([window("5h", 300, 1), window("Weekly", 10080, 0)])
        fills = [(pen, rect) for pen, rect in display.rectangles
                 if rect[2] == 3 and pen != TRACK]
        self.assertTrue(fills, display.rectangles)


class RowSpacingTests(unittest.TestCase):
    """Two rows of bars never touch, and never overlap."""

    def groups(self, display):
        tracks = sorted(rect for _pen, rect in display.rectangles
                        if rect[2] > 200 and rect[1] > 4)
        grouped = []
        for rect in tracks:
            if grouped and rect[1] - grouped[-1][0][1] < 15:
                grouped[-1].append(rect)
            else:
                grouped.append([rect])
        return grouped

    def test_three_window_and_two_window_rows_keep_two_pixels_between_them(self):
        display = Display()
        payload = {"updated_at": NOW.isoformat(), "card_order": "accounts",
                   "sources": [
                       {"id": "a", "type": "claude", "name": "Three", "status": "ok",
                        "quota": {"windows": [window("5h", 300, 12),
                                              window("Weekly", 10080, 34),
                                              window("Monthly", 43200, 5)]}},
                       {"id": "b", "type": "claude", "name": "Two", "status": "ok",
                        "quota": {"windows": [window("5h", 300, 12),
                                              window("Weekly", 10080, 34)]}}]}
        rows = source_rows(payload)
        draw_sources(display, Palette(display), rows, 0, 3, status="ok")
        grouped = self.groups(display)
        self.assertEqual(len(grouped), 2, grouped)
        first, second = grouped
        # The bars of a row stay inside its own height, with the two pixels
        # of space to the next row; each row is as tall as its own bars need.
        first_h, second_h = quota_row_height(rows[0]), quota_row_height(rows[1])
        self.assertLessEqual(max(rect[1] + rect[3] for rect in first),
                             SOURCE_TOP + first_h - 2)
        self.assertLessEqual(max(rect[1] + rect[3] for rect in second),
                             SOURCE_TOP + first_h + second_h - 2)
        self.assertGreaterEqual(min(rect[1] for rect in second) -
                                max(rect[1] + rect[3] for rect in first), 2)


class BellGeometryTests(unittest.TestCase):
    """The bell is drawn about twenty pixels wide; the tap area stays big."""

    def test_the_bell_is_twenty_pixels_wide(self):
        self.assertEqual(BELL_ICON, 20)

    def test_the_drawn_bell_reaches_the_right_edge_of_its_box(self):
        display = Display()
        from cards import Card
        ui.draw_card(display, Palette(display), Card("svc", "S", "ok", "1%", "w"),
                     0, 2, alerts={"enabled": True, "master_off": False})
        bell_x = 240 - 8 - BELL_ICON
        rectangles = [rect for _pen, rect in display.rectangles
                      if rect[0] >= bell_x and rect[1] < 24]
        self.assertTrue(rectangles)
        self.assertEqual(max(rect[0] + rect[2] for rect in rectangles), bell_x + BELL_ICON)
        self.assertEqual(min(rect[0] for rect in rectangles), bell_x)

    def test_the_tap_area_is_still_the_big_corner(self):
        self.assertEqual(BELL_HIT, 48)
        self.assertTrue(bell_hit(200, 10))
        self.assertFalse(bell_hit(191, 10))
        self.assertFalse(bell_hit(200, 48))


class AlertMarkTests(unittest.TestCase):
    """The alerts-off mark on a Home row is a small crossed bell."""

    def mark_rects(self, display):
        bells = [rect for pen, rect in display.rectangles
                 if pen == INK_DIM and rect[1] < 12 and rect[3] <= 3]
        cuts = [rect for pen, rect in display.rectangles
                if pen == BACKGROUND and rect[2] == 2 and rect[3] == 2 and rect[1] < 12]
        return bells, cuts

    def test_the_mark_is_eight_pixels_wide(self):
        self.assertEqual(ALERT_MARK, 8)

    def test_the_mark_is_a_bell_with_a_slash_cut_out_of_it(self):
        display = Display()
        payload = payload_for([window("5h", 300, 12), window("Weekly", 10080, 34)])
        payload["sources"][0]["alerts"] = False
        rows = source_rows(payload)
        draw_sources(display, Palette(display), rows, 0, 3, status="ok")
        bells, cuts = self.mark_rects(display)
        self.assertTrue(bells, display.rectangles)
        self.assertTrue(cuts, "the mark must have a slash cut out of it")
        # The slash really crosses the bell.
        for rect in cuts:
            self.assertTrue(any(bell[0] < rect[0] + rect[2] and rect[0] < bell[0] + bell[2]
                                for bell in bells), (rect, bells))
        self.assertEqual(max(rect[0] + rect[2] for rect in bells) -
                         min(rect[0] for rect in bells), ALERT_MARK)

    def test_the_mark_is_dim(self):
        display = Display()
        payload = payload_for([window("5h", 300, 12), window("Weekly", 10080, 34)])
        payload["sources"][0]["alerts"] = False
        rows = source_rows(payload)
        draw_sources(display, Palette(display), rows, 0, 3, status="ok")
        bells, _cuts = self.mark_rects(display)
        self.assertTrue(bells)
        self.assertNotIn(INK, [pen for pen, _rect in display.rectangles
                               if _rect[1] < 12 and _rect[3] <= 3])


if __name__ == "__main__":
    unittest.main()


class FadedColourReadabilityTests(unittest.TestCase):
    """A stale window must stay readable and still look like its own hue."""

    @staticmethod
    def luminance(rgb):
        channel = lambda value: (value / 255) ** 2.2
        return 0.2126 * channel(rgb[0]) + 0.7152 * channel(rgb[1]) + 0.0722 * channel(rgb[2])

    def ratio(self, one, other):
        high, low = sorted((self.luminance(one), self.luminance(other)), reverse=True)
        return (high + 0.05) / (low + 0.05)

    def test_every_faded_hue_is_readable_on_the_background_and_visible_on_the_track(self):
        from ui import BACKGROUND, TRACK, DAILY, WEEKLY, MONTHLY, LIMIT
        for name, hue in (("daily", DAILY), ("weekly", WEEKLY),
                          ("monthly", MONTHLY), ("limit", LIMIT)):
            faded = dim_rgb(hue)
            self.assertGreaterEqual(self.ratio(faded, BACKGROUND), 4.0, name)
            self.assertGreaterEqual(self.ratio(faded, TRACK), 2.4, name)

    def test_faded_hues_are_still_told_apart_and_differ_from_fresh(self):
        from ui import DAILY, WEEKLY, MONTHLY
        faded = [dim_rgb(hue) for hue in (DAILY, WEEKLY, MONTHLY)]
        self.assertEqual(len(set(faded)), 3)
        for hue in (DAILY, WEEKLY, MONTHLY):
            self.assertNotEqual(dim_rgb(hue), hue)
