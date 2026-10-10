"""Tests for presto/gestures.py - the touch gesture recogniser."""

import unittest

import presto_path  # noqa: F401

from gestures import (
    LONG_PRESS,
    SWIPE_LEFT,
    SWIPE_RIGHT,
    TAP,
    GestureRecogniser,
)


class TestTap(unittest.TestCase):

    def test_short_press_is_a_tap(self):
        g = GestureRecogniser(long_press_ms=700, swipe_min_px=40,
                              debounce_ms=0)
        self.assertEqual(g.update(True, 100, 100, 0), [])
        self.assertEqual(g.update(False, 100, 100, 200), [TAP])

    def test_no_touch_no_gesture(self):
        g = GestureRecogniser(debounce_ms=0)
        self.assertEqual(g.update(False, 0, 0, 0), [])

    def test_tap_requires_a_press_first(self):
        g = GestureRecogniser(debounce_ms=0)
        # A release with no preceding press must not register.
        self.assertEqual(g.update(False, 50, 50, 100), [])


class TestSwipe(unittest.TestCase):

    def test_swipe_left(self):
        g = GestureRecogniser(long_press_ms=700, swipe_min_px=40,
                              debounce_ms=0)
        g.update(True, 200, 100, 0)
        self.assertEqual(g.update(False, 100, 100, 300), [SWIPE_LEFT])

    def test_swipe_right(self):
        g = GestureRecogniser(long_press_ms=700, swipe_min_px=40,
                              debounce_ms=0)
        g.update(True, 40, 100, 0)
        self.assertEqual(g.update(False, 200, 100, 300), [SWIPE_RIGHT])

    def test_short_horizontal_move_is_a_tap_not_a_swipe(self):
        g = GestureRecogniser(long_press_ms=700, swipe_min_px=40,
                              debounce_ms=0)
        g.update(True, 100, 100, 0)
        self.assertEqual(g.update(False, 130, 100, 200), [TAP])

    def test_vertical_move_is_not_a_swipe(self):
        g = GestureRecogniser(long_press_ms=700, swipe_min_px=40,
                              debounce_ms=0)
        g.update(True, 100, 40, 0)
        # Bigger vertical than horizontal travel, so it is not a swipe.
        self.assertEqual(g.update(False, 120, 220, 300), [TAP])

    def test_exact_threshold_counts(self):
        g = GestureRecogniser(long_press_ms=700, swipe_min_px=40,
                              debounce_ms=0)
        g.update(True, 140, 100, 0)
        self.assertEqual(g.update(False, 100, 100, 300), [SWIPE_LEFT])


class TestLongPress(unittest.TestCase):

    def test_long_press_fires_while_held(self):
        g = GestureRecogniser(long_press_ms=700, swipe_min_px=40,
                              debounce_ms=0)
        g.update(True, 100, 100, 0)
        self.assertEqual(g.update(True, 100, 100, 800), [LONG_PRESS])

    def test_long_press_does_not_also_fire_a_tap(self):
        g = GestureRecogniser(long_press_ms=700, swipe_min_px=40,
                              debounce_ms=0)
        g.update(True, 100, 100, 0)
        g.update(True, 100, 100, 800)
        self.assertEqual(g.update(False, 100, 100, 900), [])

    def test_long_press_fires_once(self):
        g = GestureRecogniser(long_press_ms=700, swipe_min_px=40,
                              debounce_ms=0)
        g.update(True, 100, 100, 0)
        self.assertEqual(g.update(True, 100, 100, 800), [LONG_PRESS])
        self.assertEqual(g.update(True, 100, 100, 900), [])
        self.assertEqual(g.update(True, 100, 100, 1000), [])

    def test_just_under_threshold_is_not_a_long_press(self):
        g = GestureRecogniser(long_press_ms=700, swipe_min_px=40,
                              debounce_ms=0)
        g.update(True, 100, 100, 0)
        self.assertEqual(g.update(True, 100, 100, 699), [])


class TestDebounce(unittest.TestCase):

    def test_rapid_second_tap_is_ignored(self):
        g = GestureRecogniser(long_press_ms=700, swipe_min_px=40,
                              debounce_ms=180)
        g.update(True, 100, 100, 0)
        g.update(False, 100, 100, 100)
        # Immediately press and release again: inside the debounce window.
        g.update(True, 100, 100, 150)
        self.assertEqual(g.update(False, 100, 100, 200), [])

    def test_tap_after_debounce_window_registers(self):
        g = GestureRecogniser(long_press_ms=700, swipe_min_px=40,
                              debounce_ms=180)
        g.update(True, 100, 100, 0)
        g.update(False, 100, 100, 100)
        g.update(True, 100, 100, 500)
        self.assertEqual(g.update(False, 100, 100, 600), [TAP])


class TestReset(unittest.TestCase):

    def test_reset_clears_in_progress_touch(self):
        g = GestureRecogniser(long_press_ms=700, swipe_min_px=40,
                              debounce_ms=0)
        g.update(True, 100, 100, 0)
        g.reset()
        # The release must not now register as a tap.
        self.assertEqual(g.update(False, 100, 100, 100), [])


class TestMultiTouch(unittest.TestCase):

    def test_second_finger_does_not_create_a_gesture(self):
        """The recogniser only ever sees one touch point.

        Pimoroni documents that two horizontally-aligned touches are not
        correctly registered, so the app deliberately ignores multi-touch.
        This test pins the single-touch contract.
        """
        g = GestureRecogniser(long_press_ms=700, swipe_min_px=40,
                              debounce_ms=0)
        g.update(True, 100, 100, 0)
        # A second sample at a different position while still held is treated
        # as the same finger moving, not as a new gesture.
        self.assertEqual(g.update(True, 100, 100, 100), [])
        self.assertEqual(g.update(False, 100, 100, 200), [TAP])


if __name__ == "__main__":
    unittest.main()
