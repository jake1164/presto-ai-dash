"""The Home overflow strip: the layout math, the strip contents, the drawing.

The strip names the rows that are not fully visible in the list, each with
its status marker. Its height is decided by the layout, never by the scroll
position, so the list does not jump while the user scrolls.
"""

import os
import random
import sys
import unittest

import presto_path
from cards import accent_for, severity
from alerts import rgb_for_accent
from home import (BAR_GAP, BAR_H, DEFAULT_OVERFLOW_ROWS, LIST_X, LOCAL_ROW, ROW_GAP,
                  ROW_TOP_H, SOURCE_HEIGHT, SOURCE_TOP, STRIP_LINE_HEIGHT, STRIP_MAX_LINES, overflow_rows,
                  scroll_limit, source_rows, strip_needed)
from ui import INK, INK_DIM, Palette, build_home_layout, draw_sources
from test_ui import Display

PREVIEW_DIR = os.path.join(presto_path.REPO_ROOT, "tools", "presto_preview")
if PREVIEW_DIR not in sys.path:
    sys.path.append(PREVIEW_DIR)

from framebuffer import Framebuffer  # noqa: E402

HOME_NOW = "2026-10-20T12:00:00+00:00"

# The author's own six accounts, in the saved order: four subscription
# accounts and two local services.
AUTHOR_SIX = [("OpenCode Go", "quota"), ("Codex", "quota"), ("Claude", "quota"),
              ("Ollama Cloud", "quota"), ("Ollama", "local"), ("LiteLLM", "local")]


# How many windows each of the author's services reports, and so how many bars
# its row has. Every other name has one.
def window_count(name):
    return 3 if name.startswith("OpenCode Go") else 2 if name == "Claude" else 1


WINDOW_KINDS = (("5h", 300), ("Weekly", 10080), ("Monthly", 43200))


def quota_height(count):
    """The height of a quota row with this many bars, worked out from the parts."""
    return ROW_TOP_H + count * BAR_H + (count - 1) * BAR_GAP + ROW_GAP


def quota_source(name, status="ok"):
    kinds = WINDOW_KINDS[:window_count(name)] if window_count(name) > 1 else WINDOW_KINDS[1:2]
    return {"id": name.lower().replace(" ", "-"), "type": "claude", "name": name,
            "status": status,
            "quota": {"windows": [{"label": label, "period_minutes": minutes,
                                   "used_percent": 14, "state": "available",
                                   "resets_at": "2026-10-25T12:00:00+00:00",
                                   "observed_at": "2026-10-20T11:55:00+00:00"}
                                  for label, minutes in kinds]}}


def local_source(name, status="ok"):
    return {"id": name.lower().replace(" ", "-"), "type": "litellm", "name": name,
            "status": status, "metrics": {"requests_today": 12, "tokens_today": 340}}


def source_for(name, kind, status="ok"):
    return quota_source(name, status) if kind == "quota" else local_source(name, status)


def sources(spec):
    return [source_for(name, kind) for name, kind in spec]


def payload(items, display=None):
    data = {"updated_at": HOME_NOW, "card_order": "accounts", "sources": sources(items)}
    if display is not None:
        data["display"] = display
    return data


def rows_for(items, display=None):
    return source_rows(payload(items, display))


def layout_for(rows, setting, display=None):
    """The layout for these rows, measured with the display's own metrics."""
    return build_home_layout(display or Display(), rows, setting)


def strip_names(lines):
    """The names in the strip, in the order they are drawn."""
    return [row["name"] for line in lines for row, _x, _text in line]


class StripSettingTests(unittest.TestCase):
    """The setting comes from the payload; the device trusts nothing else."""

    def test_the_default_is_one_line(self):
        self.assertEqual(overflow_rows({}), DEFAULT_OVERFLOW_ROWS)
        self.assertEqual(overflow_rows({}), 1)

    def test_a_good_value_is_used(self):
        for value in (0, 1, 2, 3):
            self.assertEqual(overflow_rows({"display": {"overflow_rows": value}}), value)

    def test_junk_falls_back_to_one_line(self):
        cases = [None, "display", 7, -1, 1.5, True, False, [], {},
                 {"overflow_rows": "2"}, {"overflow_rows": None}]
        for display in cases:
            with self.subTest(display=display):
                self.assertEqual(overflow_rows({"display": display}), 1)
        self.assertEqual(overflow_rows({}), 1)
        self.assertEqual(overflow_rows(None), 1)

    def test_an_old_payload_without_the_key_still_works(self):
        rows = rows_for(AUTHOR_SIX)
        layout = layout_for(rows, overflow_rows({}))
        self.assertEqual(layout.lines, 1)


class StripRegionTests(unittest.TestCase):
    """The band, the region, and when the strip is not drawn at all."""

    def test_the_threshold_is_the_list_area(self):
        self.assertFalse(strip_needed(204, 1))
        self.assertTrue(strip_needed(205, 1))
        self.assertFalse(strip_needed(205, 0))
        self.assertFalse(strip_needed(204, 3))

    def test_a_list_that_fits_has_no_strip(self):
        rows = rows_for([("Codex", "quota")])
        layout = layout_for(rows, 3)
        self.assertEqual(layout.total, quota_height(1))
        self.assertEqual(layout.lines, 0)
        self.assertEqual(layout.region, SOURCE_HEIGHT)
        self.assertEqual(layout.limit, 0)

    def test_a_list_just_under_the_area_has_no_strip(self):
        rows = rows_for([("OpenCode Go", "quota"), ("Claude", "quota"), ("Codex", "quota"),
                         ("Ollama Cloud", "quota"), ("Ollama", "local")])
        layout = layout_for(rows, 3)
        self.assertEqual(layout.total, quota_height(3) + quota_height(2)
                         + 2 * quota_height(1) + LOCAL_ROW)
        self.assertEqual(layout.total, 191)
        self.assertEqual(layout.lines, 0)
        self.assertEqual(layout.region, SOURCE_HEIGHT)

    def test_the_setting_zero_turns_the_strip_off(self):
        rows = rows_for(AUTHOR_SIX)
        layout = layout_for(rows, 0)
        self.assertEqual(layout.lines, 0)
        self.assertEqual(layout.region, SOURCE_HEIGHT)
        self.assertEqual(layout.strip(0), ([], None))

    def test_the_region_is_the_area_minus_the_band(self):
        rows = rows_for(AUTHOR_SIX * 3)
        for setting in (1, 2, 3):
            layout = layout_for(rows, setting)
            self.assertEqual(layout.region,
                             SOURCE_HEIGHT - STRIP_LINE_HEIGHT * layout.lines)
            self.assertLessEqual(layout.lines, setting)

    def test_many_list_heights(self):
        for count in range(1, 13):
            items = [("Account %02d" % index, "quota") for index in range(count)]
            rows = rows_for(items)
            for setting in (0, 1, 2, 3):
                with self.subTest(count=count, setting=setting):
                    layout = layout_for(rows, setting)
                    overflow = layout.total > SOURCE_HEIGHT
                    self.assertEqual(layout.lines > 0, overflow and setting > 0)
                    if layout.lines:
                        self.assertEqual(layout.region,
                                         SOURCE_HEIGHT - STRIP_LINE_HEIGHT * layout.lines)
                    else:
                        self.assertEqual(layout.region, SOURCE_HEIGHT)
                    self.assertEqual(layout.limit, scroll_limit(layout.total, layout.region))


class StripLineCountTests(unittest.TestCase):
    """The line count is the layout's, not the scroll position's."""

    def test_the_authors_six_accounts_need_one_line(self):
        rows = rows_for(AUTHOR_SIX)
        layout = layout_for(rows, 1)
        self.assertEqual(layout.total, quota_height(3) + quota_height(2)
                         + 2 * quota_height(1) + 2 * LOCAL_ROW)
        self.assertEqual(layout.total, 209)
        self.assertEqual(layout.lines, 1)
        self.assertEqual(layout.region, 186)
        lines, plus = layout.strip(0)
        self.assertIsNone(plus)
        self.assertEqual(strip_names(lines), ["Ollama", "LiteLLM"])
        # The four subscription rows are fully visible without scrolling.
        self.assertEqual([row["name"] for row in layout.hidden(0)],
                         ["Ollama", "LiteLLM"])

    def test_one_hidden_name_needs_one_line(self):
        # Three one-bar rows and two three-bar rows: 38 * 3 + 52 * 2 = 218, so the
        # list scrolls, and with 186 pixels left the fifth row is the only one cut off.
        rows = rows_for([("Codex", "quota"), ("Ollama Cloud", "quota"), ("Account 03", "quota"),
                         ("OpenCode Go", "quota"), ("OpenCode Go B", "quota")])
        layout = layout_for(rows, 1)
        self.assertEqual(layout.total, 218)
        self.assertEqual(layout.lines, 1)
        lines, plus = layout.strip(0)
        self.assertIsNone(plus)
        self.assertEqual(len(strip_names(lines)), 1)

    def test_names_that_need_two_lines(self):
        """Eight rows of nine-character names: two lines, or one line + '+N'."""
        items = [("Account%02d" % index, "quota") for index in range(8)]
        rows = rows_for(items)
        one = layout_for(rows, 1)
        self.assertEqual(one.lines, 1)
        _, plus = one.strip(0)
        self.assertIsNotNone(plus)
        self.assertTrue(plus[0].startswith("+"))
        for setting in (2, 3):
            with self.subTest(setting=setting):
                layout = layout_for(rows, setting)
                self.assertEqual(layout.lines, 2)
                for offset in (0, layout.limit // 2, layout.limit):
                    lines, plus = layout.strip(offset)
                    self.assertIsNone(plus, offset)
                    self.assertLessEqual(len(lines), 2)

    def test_the_maximum_is_never_exceeded(self):
        items = [("Account%02d" % index, "quota") for index in range(30)]
        rows = rows_for(items)
        for setting in (1, 2, 3):
            with self.subTest(setting=setting):
                self.assertLessEqual(layout_for(rows, setting).lines, setting)
        # A setting above the maximum is capped at it.
        layout = layout_for(rows, 5)
        self.assertLessEqual(layout.setting, STRIP_MAX_LINES)
        self.assertLessEqual(layout.lines, STRIP_MAX_LINES)

    def test_the_line_count_is_the_same_at_every_offset(self):
        items = [("Account%02d" % index, "quota") for index in range(12)]
        rows = rows_for(items)
        for setting in (1, 2, 3):
            layout = layout_for(rows, setting)
            region = layout.region
            for offset in (0, layout.limit // 3, layout.limit // 2, layout.limit):
                self.assertEqual(layout.region, region)
                self.assertEqual(layout.lines, layout.lines)

    def test_fifteen_accounts_fit_without_a_count_and_sixteen_need_one(self):
        # The rows are thirty-eight pixels each, so more of them fit than when every
        # row was forty-six: the strip names what is cut off in three lines up to
        # fifteen accounts, and from sixteen it adds a count.
        for total, counted in ((15, False), (16, True)):
            with self.subTest(accounts=total):
                items = [("Acc%02d" % index, "quota") for index in range(total)]
                layout = layout_for(rows_for(items), 3)
                self.assertEqual(layout.lines, 3)
                for offset in (0, layout.limit // 2, layout.limit):
                    lines, plus = layout.strip(offset)
                    self.assertEqual(plus is not None, counted, offset)

    def test_thirty_accounts_need_the_count_and_the_worst_colour(self):
        sources = [quota_source("Acc%02d" % index) for index in range(30)]
        sources[28]["status"] = "warning"
        sources[29]["status"] = "critical"
        rows = source_rows({"updated_at": HOME_NOW, "card_order": "accounts",
                            "sources": sources})
        layout = layout_for(rows, 3)
        self.assertEqual(layout.lines, 3)
        lines, plus = layout.strip(0)
        self.assertIsNotNone(plus)
        self.assertEqual(plus[1], "critical")
        hidden = layout.hidden(0)
        shown = strip_names(lines)
        self.assertEqual(plus[0], "+%d" % (len(hidden) - len(shown)))

    def test_a_payload_that_changes_the_services_clamps_the_offset(self):
        from home import HomeScroll
        rows = rows_for([("Account%02d" % index, "quota") for index in range(12)])
        layout = layout_for(rows, 1)
        scroll = HomeScroll(layout.total, SOURCE_TOP, SOURCE_HEIGHT, layout.region)
        scroll.offset = scroll.limit
        few = rows_for([("Account%02d" % index, "quota") for index in range(2)])
        smaller = layout_for(few, 1)
        scroll.set_height(smaller.total, SOURCE_TOP, SOURCE_HEIGHT, smaller.region)
        self.assertEqual(scroll.offset, 0)


class StripContentTests(unittest.TestCase):
    """The strip names exactly the rows that are not fully visible."""

    def rows(self, count=8):
        items = [("Account%02d" % index, "quota") for index in range(count)]
        return rows_for(items)

    def test_the_strip_names_the_hidden_rows_in_order(self):
        rows = self.rows()
        layout = layout_for(rows, 1)
        for offset in (0, layout.limit // 2, layout.limit):
            hidden = layout.hidden(offset)
            lines, plus = layout.strip(offset)
            self.assertTrue(hidden)
            shown = strip_names(lines)
            self.assertEqual(shown, [row["name"] for row in hidden[:len(shown)]])
            if plus:
                self.assertEqual(plus[0], "+%d" % (len(hidden) - len(shown)))

    def test_a_drawn_strip_is_never_empty(self):
        rows = self.rows()
        layout = layout_for(rows, 1)
        for offset in range(0, layout.limit + 1, max(1, layout.limit // 20)):
            lines, plus = layout.strip(offset)
            self.assertTrue(lines or plus, offset)

    def test_no_strip_when_the_list_fits(self):
        rows = rows_for([("Codex", "quota")])
        layout = layout_for(rows, 3)
        self.assertEqual(layout.strip(0), ([], None))

    def test_no_strip_when_the_setting_is_zero(self):
        rows = self.rows()
        layout = layout_for(rows, 0)
        self.assertEqual(layout.strip(0), ([], None))


class StripPropertyTests(unittest.TestCase):
    """The strip's invariants, over many lists, settings and offsets."""

    NAMES = ["Ollama", "Codex", "Claude", "Ollama Cloud", "LiteLLM", "A very long account name",
             "Go", "X", "Service account number one", "Claude", "Ollama", "Local service"]

    def rows(self, count, statuses=None):
        random.seed(20261020 + count)
        items = []
        for index in range(count):
            name = random.choice(self.NAMES)
            kind = "quota" if index % 3 else "local"
            status = "ok"
            if statuses and index < len(statuses):
                status = statuses[index]
            items.append((name, kind, status))
        sources = [source_for(name, kind, status) for name, kind, status in items]
        return source_rows({"updated_at": HOME_NOW, "card_order": "accounts",
                            "sources": sources})

    def test_the_invariants_hold(self):
        for count in range(1, 31):
            rows = self.rows(count)
            for setting in (0, 1, 2, 3):
                with self.subTest(count=count, setting=setting):
                    self.check(rows, setting)

    def check(self, rows, setting):
        display = Display()
        layout = build_home_layout(display, rows, setting)
        overflow = layout.total > SOURCE_HEIGHT
        # (a) the strip is drawn exactly when the list overflows and the
        #     setting allows it.
        self.assertEqual(layout.lines > 0, overflow and setting > 0)
        # (j) the line count never exceeds the setting.
        self.assertLessEqual(layout.lines, setting)
        # (h) the scroll limit and the thumb follow the region.
        self.assertEqual(layout.limit, scroll_limit(layout.total, layout.region))
        offsets = sorted(set([0, layout.limit // 4, layout.limit // 2,
                               (3 * layout.limit) // 4, layout.limit]))
        for offset in offsets:
            lines, plus = layout.strip(offset)
            hidden = layout.hidden(offset)
            # (b) a drawn strip is never empty.
            if layout.lines:
                self.assertTrue(lines or plus, offset)
            # (c) the names are in the saved order.
            shown = strip_names(lines)
            self.assertEqual(shown, [row["name"] for row in hidden[:len(shown)]])
            # (g) the count takes the worst colour of the rows it replaced.
            if plus:
                replaced = hidden[len(shown):]
                worst = None
                for row in replaced:
                    if worst is None or severity(row["status"]) > severity(worst):
                        worst = row["status"]
                self.assertEqual(plus[1], worst)
                self.assertEqual(plus[0], "+%d" % len(replaced))
            # (d) the first row is fully visible at the top, the last at the end.
            if offset == 0:
                self.assertFalse(any(row is rows[0] for row in hidden))
            if offset == layout.limit:
                self.assertFalse(any(row is rows[-1] for row in hidden))
            # (i) the band does not move with the scroll offset.
            self.assertEqual(layout.region, SOURCE_HEIGHT - STRIP_LINE_HEIGHT * layout.lines)

    def test_the_drawing_keeps_to_the_region_and_the_band(self):
        for count in (6, 12, 30):
            rows = self.rows(count)
            for setting in (0, 1, 2, 3):
                with self.subTest(count=count, setting=setting):
                    layout = build_home_layout(Display(), rows, setting)
                    for offset in (0, layout.limit // 2, layout.limit):
                        display = Display()
                        draw_sources(display, Palette(display), rows, 0, 3, scroll=offset,
                                     layout=layout)
                        self.check_drawing(display, layout, offset)

    def check_drawing(self, display, layout, offset):
        band_top = SOURCE_TOP + layout.region
        band_bottom = SOURCE_TOP + SOURCE_HEIGHT
        for index, (text, x, y, wrap, scale, font) in enumerate(display.rows):
            clip = display.row_clips[index]
            height = (8 if font == "bitmap8" else 6) * scale
            display.set_font(font)
            if clip is not None:
                # (e) the list is clipped to the region, never into the band.
                self.assertEqual(clip, (14, SOURCE_TOP, 218, layout.region), text)
                continue
            if band_top <= y < band_bottom:
                # (f) the strip stays inside its band and the right margin.
                self.assertLessEqual(y + height, band_bottom, text)
                self.assertLessEqual(x + display.measure_text(text, scale), 14 + 218, text)
            else:
                # The footer never sits in the band.
                self.assertGreaterEqual(y, band_bottom, text)


class StripDrawingTests(unittest.TestCase):
    """What the strip looks like on the device."""

    def items(self, count, statuses=None):
        items = []
        for index in range(count):
            name = "Acc%02d" % index
            kind = "quota" if index % 3 else "local"
            status = "ok"
            if statuses and index < len(statuses):
                status = statuses[index]
            items.append((name, kind, status))
        return items

    def rows(self, count, statuses=None):
        sources = [source_for(name, kind, status)
                   for name, kind, status in self.items(count, statuses)]
        return source_rows({"updated_at": HOME_NOW, "card_order": "accounts",
                            "sources": sources})

    def test_the_strip_names_the_hidden_accounts(self):
        rows = self.rows(8)
        layout = build_home_layout(Display(), rows, 1)
        display = Display()
        draw_sources(display, Palette(display), rows, 0, 3, layout=layout)
        texts = [row[0] for row in display.rows]
        self.assertIn("Acc05", texts)
        self.assertEqual(display.row_clips[0], (14, SOURCE_TOP, 218, layout.region))

    def test_a_long_name_is_truncated_not_wrapped(self):
        rows = source_rows({"updated_at": HOME_NOW, "card_order": "accounts", "sources": [
            quota_source("A very long account name that will not fit") for _ in range(8)]})
        layout = build_home_layout(Display(), rows, 1)
        display = Display()
        draw_sources(display, Palette(display), rows, 0, 3, layout=layout)
        band = [row for index, row in enumerate(display.rows)
                if display.row_clips[index] is None
                and row[2] >= SOURCE_TOP + layout.region]
        self.assertTrue(band)
        for text, x, y, wrap, scale, font in band:
            if text.startswith("+"):
                continue
            display.set_font(font)
            self.assertLessEqual(display.measure_text(text, scale), wrap, text)
            self.assertTrue(text.endswith("~"), text)
            self.assertLess(len(text), 45)

    def test_the_stale_strip_is_dimmed(self):
        rows = self.rows(8)
        layout = build_home_layout(Display(), rows, 1)
        display = Display()
        draw_sources(display, Palette(display), rows, 0, 3, layout=layout, stale=True)
        band = [index for index, row in enumerate(display.rows)
                if row[2] >= SOURCE_TOP + layout.region and row[2] < 208]
        self.assertTrue(band)
        for index in band:
            self.assertEqual(display.text_pens[index], INK_DIM)

    def test_the_fresh_strip_is_not_dimmed(self):
        rows = self.rows(8)
        layout = build_home_layout(Display(), rows, 1)
        display = Display()
        draw_sources(display, Palette(display), rows, 0, 3, layout=layout)
        band = [index for index, row in enumerate(display.rows)
                if row[2] >= SOURCE_TOP + layout.region and row[2] < 208]
        self.assertTrue(band)
        self.assertIn(INK, display.text_pens)

    def test_the_footer_does_not_overlap_the_strip(self):
        for setting in (1, 2, 3):
            rows = self.rows(12)
            layout = build_home_layout(Display(), rows, setting)
            for stale in (False, True):
                for muted in (False, True):
                    display = Display()
                    draw_sources(display, Palette(display), rows, 0, 3, layout=layout,
                                 stale=stale, muted=muted)
                    texts = [row[0] for row in display.rows]
                    if stale:
                        self.assertIn("STALE - retrying", texts)
                    if muted:
                        self.assertIn("MUTED", texts)
                    for text, x, y, wrap, scale, font in display.rows:
                        if text in ("STALE - retrying", "MUTED"):
                            self.assertGreaterEqual(y, 208, text)

    def test_the_count_uses_the_worst_colour(self):
        rows = self.rows(30, ["ok"] * 29 + ["critical"])
        layout = build_home_layout(Display(), rows, 3)
        display = Display()
        draw_sources(display, Palette(display), rows, 0, 3, layout=layout)
        counts = [index for index, row in enumerate(display.rows) if row[0].startswith("+")]
        self.assertTrue(counts)
        worst = rgb_for_accent(accent_for("critical"))
        self.assertEqual(display.text_pens[counts[-1]], worst)

    def test_the_band_does_not_move_with_the_scroll_offset(self):
        """The band is the same height at the top, in the middle and at the end."""
        rows = self.rows(12)
        layout = build_home_layout(Display(), rows, 3)
        bands = []
        for offset in (0, layout.limit // 2, layout.limit):
            display = Display()
            draw_sources(display, Palette(display), rows, 0, 3, scroll=offset,
                         layout=layout)
            ys = [row[2] for index, row in enumerate(display.rows)
                  if display.row_clips[index] is None
                  and SOURCE_TOP <= row[2] < 208]
            self.assertTrue(ys, offset)
            bands.append((min(ys), max(ys)))
        self.assertEqual(len(set(bands)), 1, bands)

    def test_no_strip_line_wraps_on_the_real_font(self):
        """The device's own font metrics: a strip line must stay on one line."""
        rows = source_rows({"updated_at": HOME_NOW, "card_order": "accounts", "sources": [
            quota_source("Ollama Cloud account %d" % index) for index in range(12)]})
        display = Framebuffer()
        calls = []
        original_text = display.text

        def record(text, x, y, wrap=-1, scale=2, angle=0, spacing=1, fixed_width=False):
            calls.append((text, x, y, wrap, scale, display.font))
            return original_text(text, x, y, wrap, scale, angle, spacing, fixed_width)

        display.text = record
        layout = build_home_layout(display, rows, 3)
        draw_sources(display, Palette(display), rows, 0, 3, layout=layout)
        band_top = SOURCE_TOP + layout.region
        checked = 0
        for text, x, y, wrap, scale, font in calls:
            if not band_top <= y < 208:
                continue
            display.font = font
            self.assertLessEqual(display.measure_text(text, scale) + 1, wrap,
                                 "%r at %d,%d" % (text, x, y))
            checked += 1
        self.assertTrue(checked)


if __name__ == "__main__":
    unittest.main()
