"""The mute that expires: the device's long-press mute and its indicator.

The device has no wall clock, so the length of a mute is counted with the
device's own tick counter, which wraps. The clock is injected here, so the
behaviour can be tested exactly, including the wrap.

These drive the real presto/ modules with a fake display. No provider is
contacted and no real reading is read.
"""

import importlib.util
import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import presto_path
from alerts import (CUE_DOUBLE_CHIRP, CUE_NONE, AlertTracker, mute_chip_text)
from cards import Card
from home import alert_settings
from ui import (MUTE_CHIP_H, MUTE_CHIP_Y, Palette, _dot_span, draw_card,
                draw_sources, mute_chip_rect)

PREVIEW_DIR = os.path.join(presto_path.REPO_ROOT, "tools", "presto_preview")
if PREVIEW_DIR not in sys.path:
    sys.path.append(PREVIEW_DIR)

from framebuffer import Framebuffer  # noqa: E402


class RecordingFramebuffer(Framebuffer):
    """The real framebuffer, with the drawn text kept for the tests."""

    def __init__(self, width=240, height=240):
        Framebuffer.__init__(self, width, height)
        self.drawn_texts = []
        original = self.text

        def record(text, x, y, wrap=-1, scale=2, angle=0, spacing=1, fixed_width=False):
            self.drawn_texts.append(str(text))
            return original(text, x, y, wrap, scale, angle, spacing, fixed_width)

        self.text = record


class Clock(object):
    """A tick counter that wraps, like the device's.

    The period is small on purpose: a mute that crosses the wrap must still
    be counted correctly.
    """

    def __init__(self, start=0, period=1 << 30):
        self.now = start
        self.period = period
        self.wraps = 0

    def __call__(self):
        return self.now

    def advance(self, milliseconds):
        self.now += milliseconds
        while self.now >= self.period:
            self.now -= self.period
            self.wraps += 1

    def diff(self, later, earlier):
        period = self.period
        return ((later - earlier + period // 2) % period) - period // 2


HOUR_MS = 60 * 60 * 1000


def tracker(hours=4, clock=None):
    """A tracker with an injected clock."""
    clock = clock or Clock()
    tracker = AlertTracker(clock=clock, ticks_diff=clock.diff)
    tracker.set_mute_hours(hours)
    return tracker


class MuteExpiryTests(unittest.TestCase):
    """A long-press mute ends by itself."""

    def test_the_mute_ends_after_exactly_the_mute_length(self):
        clock = Clock()
        t = tracker(4, clock)
        t.toggle_mute()
        self.assertTrue(t.muted)
        clock.advance(4 * HOUR_MS - 1)
        self.assertFalse(t.mute_expired(), "one millisecond early is not yet")
        self.assertTrue(t.muted)
        clock.advance(1)
        self.assertTrue(t.mute_expired())
        self.assertFalse(t.muted, "the mute lifts itself")

    def test_a_mute_of_zero_never_expires(self):
        clock = Clock()
        t = tracker(0, clock)
        t.toggle_mute()
        clock.advance(48 * HOUR_MS)
        self.assertFalse(t.mute_expired())
        self.assertTrue(t.muted, "0 means until the user presses again")

    def test_a_long_press_while_muted_unmutes_at_once(self):
        t = tracker(4)
        self.assertTrue(t.toggle_mute())
        self.assertFalse(t.toggle_mute())
        self.assertFalse(t.muted)

    def test_the_mute_survives_a_wrap_of_the_tick_counter(self):
        # The counter wraps once during the mute: the length must still be
        # counted, and the mute must still end.
        period = 5 * HOUR_MS
        clock = Clock(start=period - 1000, period=period)
        t = tracker(1, clock)
        t.toggle_mute()
        clock.advance(1 * HOUR_MS)
        self.assertEqual(clock.wraps, 1, "the counter must have wrapped")
        self.assertTrue(t.mute_expired(), "the wrap must not hide the expiry")

    def test_a_wrap_does_not_end_the_mute_early(self):
        period = 5 * HOUR_MS
        clock = Clock(start=period - 1000, period=period)
        t = tracker(4, clock)
        t.toggle_mute()
        clock.advance(1 * HOUR_MS)
        self.assertEqual(clock.wraps, 1, "the counter must have wrapped")
        self.assertFalse(t.mute_expired())
        self.assertTrue(t.muted)

    def test_changing_the_length_applies_to_the_next_mute(self):
        clock = Clock()
        t = tracker(4, clock)
        t.toggle_mute()
        t.set_mute_hours(1)
        clock.advance(2 * HOUR_MS)
        self.assertFalse(t.mute_expired(), "the running mute keeps its length")
        self.assertTrue(t.muted)

    def test_the_next_mute_uses_the_new_length(self):
        clock = Clock()
        t = tracker(4, clock)
        t.toggle_mute()
        t.toggle_mute()
        t.set_mute_hours(1)
        t.toggle_mute()
        clock.advance(1 * HOUR_MS)
        self.assertTrue(t.mute_expired())

    def test_an_unusable_length_is_the_default(self):
        t = AlertTracker()
        for value in (-1, 49, "4", 4.5, None, True):
            with self.subTest(value=value):
                self.assertEqual(t.set_mute_hours(value), 4)
        self.assertEqual(t.set_mute_hours(0), 0)
        self.assertEqual(t.set_mute_hours(48), 48)

    def test_a_tracker_without_a_clock_never_expires(self):
        t = AlertTracker()
        t.toggle_mute()
        self.assertFalse(t.mute_expired())
        self.assertTrue(t.muted)

    def test_a_known_mute_state_can_be_set(self):
        """The preview and the tests put the tracker in a known state."""
        clock = Clock()
        t = tracker(4, clock)
        t.set_mute(True, 3 * HOUR_MS)
        self.assertTrue(t.muted)
        self.assertEqual(t.mute_label(), "MUTED 3H")
        clock.advance(3 * HOUR_MS)
        self.assertTrue(t.mute_expired())
        t.set_mute(True, None)
        self.assertEqual(t.mute_label(), "MUTED 4H")
        t.set_mute(False)
        self.assertFalse(t.muted)
        self.assertIsNone(t.mute_label())

    def test_a_known_mute_state_can_show_the_minutes_left(self):
        clock = Clock()
        t = tracker(4, clock)
        t.set_mute(True, 45 * 60 * 1000)
        self.assertEqual(t.mute_label(), "MUTED 45M")

    def test_a_restart_clears_the_mute(self):
        """The mute is not stored anywhere: a new device starts unmuted."""
        t = tracker(4)
        t.toggle_mute()
        fresh = AlertTracker()
        self.assertFalse(fresh.muted)
        self.assertIsNone(fresh.mute_remaining())


class MuteAndCuesTests(unittest.TestCase):
    """A mute is silent, and it stays silent when it ends."""

    def test_no_cue_while_muted(self):
        t = tracker(4)
        t.update_one("a", "ok")
        t.toggle_mute()
        self.assertEqual(t.update_one("a", "critical"), CUE_NONE)

    def test_an_alarm_raised_during_the_mute_does_not_chirp_after_it(self):
        clock = Clock()
        t = tracker(4, clock)
        t.update_one("a", "ok")
        t.toggle_mute()
        # The service gets worse while the mute is on: no sound, but the
        # status is tracked.
        self.assertEqual(t.update_one("a", "critical"), CUE_NONE)
        clock.advance(4 * HOUR_MS)
        self.assertTrue(t.mute_expired())
        # The mute is over, but the alarm began during it: no chirp.
        self.assertEqual(t.update_one("a", "critical"), CUE_NONE)
        # A later, new alarm does sound.
        self.assertEqual(t.update_one("a", "offline"), CUE_DOUBLE_CHIRP)

    def test_the_mute_is_silent_when_it_ends(self):
        clock = Clock()
        t = tracker(4, clock)
        t.update_one("a", "ok")
        t.toggle_mute()
        clock.advance(4 * HOUR_MS)
        self.assertTrue(t.mute_expired())


class MuteChipTextTests(unittest.TestCase):
    """The chip says how long the mute has left."""

    def test_no_end_says_muted_alone(self):
        self.assertEqual(mute_chip_text(None), "MUTED")

    def test_the_time_is_rounded_up(self):
        self.assertEqual(mute_chip_text(45 * 60 * 1000), "MUTED 45M")
        self.assertEqual(mute_chip_text(3 * HOUR_MS), "MUTED 3H")
        # A second over three hours is four hours, not three.
        self.assertEqual(mute_chip_text(3 * HOUR_MS + 1000), "MUTED 4H")
        # Half a minute left is a minute, never zero.
        self.assertEqual(mute_chip_text(30 * 1000), "MUTED 1M")
        self.assertEqual(mute_chip_text(0), "MUTED")

    def test_the_label_of_a_tracker(self):
        clock = Clock()
        t = tracker(4, clock)
        self.assertIsNone(t.mute_label())
        t.toggle_mute()
        self.assertEqual(t.mute_label(), "MUTED 4H")
        clock.advance(1 * HOUR_MS)
        self.assertEqual(t.mute_label(), "MUTED 3H")
        clock.advance(3 * HOUR_MS - 60000)
        self.assertEqual(t.mute_label(), "MUTED 1M")

    def test_a_mute_with_no_end_says_muted_alone(self):
        clock = Clock()
        t = tracker(0, clock)
        t.toggle_mute()
        self.assertEqual(t.mute_label(), "MUTED")


def overlaps(one, other):
    """Whether two rectangles (x, y, w, h) share any pixel."""
    ax, ay, aw, ah = one
    bx, by, bw, bh = other
    return ax < bx + bw and bx < ax + aw and ay < by + bh and by < ay + ah


class MuteChipGeometryTests(unittest.TestCase):
    """The chip has its own slot and never covers anything else."""

    WIDTH = 240

    def chip(self, display, text="MUTED 45M"):
        return mute_chip_rect(display, text, self.WIDTH)

    def forbidden(self, total=1, stale=False):
        """The places the chip must not reach."""
        rects = {
            "title": (14, 6, self.WIDTH - 14, 16),
            "bell": (self.WIDTH - 48, 0, 48, 48),
            "status chip": (0, 24, self.WIDTH, 16),
        }
        # The page dots, in the footer band.
        visible, _first, start = _dot_span(0, total, self.WIDTH)
        if visible:
            rects["dots"] = (start - 3, 225, (visible - 1) * 12 + 7, 7)
        # The STALE line: on Home it is in the footer, on a card above it.
        rects["stale line"] = (14, 208, 120, 10)
        return rects

    def test_the_chip_is_in_the_bottom_right_corner(self):
        display = Framebuffer()
        x, y, w, h = self.chip(display)
        self.assertEqual(y, MUTE_CHIP_Y)
        self.assertEqual(h, MUTE_CHIP_H)
        self.assertEqual(x + w, self.WIDTH - 8)
        self.assertGreaterEqual(y + h, 218)

    def test_it_does_not_cover_anything_on_home(self):
        display = Framebuffer()
        chip = self.chip(display)
        for name, rect in self.forbidden(total=9).items():
            with self.subTest(what=name):
                self.assertFalse(overlaps(chip, rect),
                                 "the chip covers the %s" % name)

    def test_it_does_not_cover_anything_on_a_stale_home(self):
        display = Framebuffer()
        chip = self.chip(display)
        for name, rect in self.forbidden(total=9, stale=True).items():
            with self.subTest(what=name):
                self.assertFalse(overlaps(chip, rect),
                                 "the chip covers the %s" % name)

    def test_it_does_not_cover_anything_on_a_card(self):
        display = Framebuffer()
        chip = self.chip(display)
        for name, rect in self.forbidden(total=9).items():
            with self.subTest(what=name):
                self.assertFalse(overlaps(chip, rect),
                                 "the chip covers the %s" % name)

    def test_the_longest_names_do_not_move_the_chip(self):
        """The chip is in a fixed slot: it does not move with the content."""
        display = Framebuffer()
        rows = [{"id": "a", "type": "claude", "status": "ok",
                 "name": "A very long account name that fills the row",
                 "windows": [{"label": "Weekly", "percent": 12, "state": "available",
                              "period_minutes": 10080, "reset_in": 3600}]}]
        draw_sources(display, Palette(display), rows, 0, 3,
                     mute_text="MUTED 45M")
        chip = self.chip(display)
        for name, rect in self.forbidden(total=3).items():
            with self.subTest(what=name):
                self.assertFalse(overlaps(chip, rect))

    def test_the_chip_is_drawn_on_home_and_on_a_card(self):
        display = RecordingFramebuffer()
        palette = Palette(display)
        card = Card("claude", "Claude", "ok", "5h 12%", "Weekly 66%",
                    ["Quota via export"])
        draw_card(display, palette, card, 0, 2, muted=True,
                  mute_text="MUTED 3H")
        self.assertTrue(_has_chip(display), "the card has no mute chip")
        display = RecordingFramebuffer()
        draw_card(display, Palette(display), card, 0, 2, muted=True,
                  mute_text="MUTED 3H", alerts={"enabled": True})
        self.assertTrue(_has_chip(display))
        display = RecordingFramebuffer()
        draw_sources(display, Palette(display), [], 0, 1, muted=True,
                     mute_text="MUTED 3H")
        self.assertTrue(_has_chip(display))

    def test_nothing_is_drawn_when_the_sound_is_not_muted(self):
        display = RecordingFramebuffer()
        card = Card("claude", "Claude", "ok", "5h 12%", "Weekly 66%")
        draw_card(display, Palette(display), card, 0, 2, muted=False)
        self.assertFalse(_has_chip(display))


def _has_chip(display):
    """Whether the screen carries the mute chip's text."""
    texts = getattr(display, "drawn_texts", None)
    if texts is None:
        return False
    return any("MUTED" in text for text in texts)


class DeviceAppMuteTests(unittest.TestCase):
    """The app's own long press and the expiry in the real app."""

    def app(self):
        spec = importlib.util.spec_from_file_location(
            "device_mute_app_for_test",
            os.path.join(presto_path.PRESTO_DIR, "main.py"))
        module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {"config": SimpleNamespace()}):
            spec.loader.exec_module(module)
        display = Framebuffer()
        device = SimpleNamespace(display=display, update=lambda: None, NUM_LEDS=7,
                                 set_led_rgb=lambda *args: None,
                                 wifi=SimpleNamespace(isconnected=lambda: True),
                                 touch=SimpleNamespace(state=False, poll=lambda: None,
                                                       x=0, y=0))
        app = module.App(device)
        return module, app

    def clock(self, module):
        """The device's tick counter, injected."""
        clock = Clock()
        return patch.multiple(module, _now_ms=clock, _ticks_diff=clock.diff)

    def test_a_long_press_mutes_and_the_chip_says_how_long_is_left(self):
        module, app = self.app()
        clock = Clock()
        with patch.object(module, "_now_ms", side_effect=lambda: clock.now), \
                patch.object(module, "_ticks_diff", side_effect=clock.diff):
            app._handle_gestures(["long_press"])
            self.assertTrue(app.alerts.muted)
            self.assertEqual(app.alerts.mute_label(), "MUTED 4H")
            clock.advance(1 * HOUR_MS)
            self.assertEqual(app.alerts.mute_label(), "MUTED 3H")

    def test_the_mute_ends_by_itself_and_says_nothing(self):
        module, app = self.app()
        clock = Clock()
        with patch.object(module, "_now_ms", side_effect=lambda: clock.now), \
                patch.object(module, "_ticks_diff", side_effect=clock.diff):
            app._handle_gestures(["long_press"])
            self.assertTrue(app.alerts.muted)
            clock.advance(4 * HOUR_MS)
            self.assertTrue(app._expire_alerts())
            self.assertFalse(app.alerts.muted)
            self.assertIsNone(app.alerts.mute_label())

    def test_a_long_press_while_muted_unmutes_at_once(self):
        module, app = self.app()
        clock = Clock()
        with patch.object(module, "_now_ms", side_effect=lambda: clock.now), \
                patch.object(module, "_ticks_diff", side_effect=clock.diff):
            app._handle_gestures(["long_press"])
            app._handle_gestures(["long_press"])
            self.assertFalse(app.alerts.muted)

    def test_the_mute_length_comes_from_the_payload(self):
        module, app = self.app()
        payload = {"cards": [{"id": "dash", "title": "AI Dash", "status": "ok"}],
                   "sources": [], "alerts": {"mute_hours": 12}}
        with self.clock(module):
            app._apply_alerts(payload)
        self.assertEqual(app.alerts.mute_hours, 12)

    def test_a_junk_mute_length_keeps_the_device_default(self):
        module, app = self.app()
        payload = {"cards": [{"id": "dash", "title": "AI Dash", "status": "ok"}],
                   "sources": [], "alerts": {"mute_hours": "four"}}
        with self.clock(module):
            app._apply_alerts(payload)
        self.assertEqual(app.alerts.mute_hours, 4)

    def test_the_device_default_is_four_hours(self):
        self.assertEqual(alert_settings({})["mute_hours"], 4)


if __name__ == "__main__":
    unittest.main()
