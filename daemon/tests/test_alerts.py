"""Tests for presto/alerts.py - LED and sound behaviour."""

import unittest

import presto_path  # noqa: F401

from alerts import (
    CUE_CHIRP,
    CUE_DOUBLE_CHIRP,
    CUE_NONE,
    ACCENT_RGB,
    AlertTracker,
    accent_for_status,
    chirp_count,
    cue_for_status,
    is_valid_accent,
    led_pattern,
    rgb_for_accent,
    rgb_for_status,
)


class TestAccentMapping(unittest.TestCase):

    def test_every_status_maps_to_an_accent(self):
        for status in ("ok", "active", "warning", "near_limit",
                       "rate_limited", "critical", "offline"):
            self.assertIn(accent_for_status(status), ACCENT_RGB)

    def test_unknown_is_red(self):
        self.assertEqual(accent_for_status("banana"), "red")

    def test_active_is_blue(self):
        self.assertEqual(accent_for_status("active"), "blue")

    def test_ok_is_green(self):
        self.assertEqual(accent_for_status("ok"), "green")


class TestRgb(unittest.TestCase):

    def test_full_brightness_is_unscaled(self):
        self.assertEqual(rgb_for_accent("green", 1.0), ACCENT_RGB["green"])

    def test_zero_brightness_is_off(self):
        self.assertEqual(rgb_for_accent("green", 0.0), (0, 0, 0))

    def test_half_brightness_scales(self):
        r, g, b = rgb_for_accent("green", 0.5)
        self.assertEqual(r, ACCENT_RGB["green"][0] // 2)
        self.assertEqual(g, ACCENT_RGB["green"][1] // 2)

    def test_unknown_accent_is_red(self):
        self.assertEqual(rgb_for_accent("chartreuse"), ACCENT_RGB["red"])

    def test_rgb_for_status(self):
        self.assertEqual(rgb_for_status("ok", 1.0), ACCENT_RGB["green"])

    def test_values_are_ints(self):
        for accent in ACCENT_RGB:
            for value in rgb_for_accent(accent, 0.37):
                self.assertIsInstance(value, int)


class TestCues(unittest.TestCase):

    def test_ok_is_silent(self):
        self.assertEqual(cue_for_status("ok"), CUE_NONE)

    def test_active_is_silent(self):
        self.assertEqual(cue_for_status("active"), CUE_NONE)

    def test_warning_chirps_once(self):
        self.assertEqual(cue_for_status("warning"), CUE_CHIRP)

    def test_near_limit_chirps_once(self):
        self.assertEqual(cue_for_status("near_limit"), CUE_CHIRP)

    def test_critical_double_chirps(self):
        self.assertEqual(cue_for_status("critical"), CUE_DOUBLE_CHIRP)

    def test_rate_limited_double_chirps(self):
        self.assertEqual(cue_for_status("rate_limited"), CUE_DOUBLE_CHIRP)

    def test_offline_double_chirps(self):
        self.assertEqual(cue_for_status("offline"), CUE_DOUBLE_CHIRP)

    def test_unknown_is_treated_as_offline(self):
        """An unrecognised status is fail-safe: it alerts like offline."""
        self.assertEqual(cue_for_status("banana"), CUE_DOUBLE_CHIRP)

    def test_chirp_counts(self):
        self.assertEqual(chirp_count(CUE_NONE), 0)
        self.assertEqual(chirp_count(CUE_CHIRP), 1)
        self.assertEqual(chirp_count(CUE_DOUBLE_CHIRP), 2)


class TestAlertTracker(unittest.TestCase):

    def test_first_reading_is_silent(self):
        """The device must not chirp at the user the moment it boots."""
        t = AlertTracker()
        self.assertEqual(t.update("critical"), CUE_NONE)

    def test_escalation_chirps(self):
        t = AlertTracker()
        t.update("ok")
        self.assertEqual(t.update("warning"), CUE_CHIRP)

    def test_escalation_to_critical_double_chirps(self):
        t = AlertTracker()
        t.update("warning")
        self.assertEqual(t.update("critical"), CUE_DOUBLE_CHIRP)

    def test_no_repeat_while_status_holds(self):
        t = AlertTracker()
        t.update("ok")
        t.update("warning")
        self.assertEqual(t.update("warning"), CUE_NONE)
        self.assertEqual(t.update("warning"), CUE_NONE)

    def test_recovery_is_silent(self):
        t = AlertTracker()
        t.update("warning")
        self.assertEqual(t.update("ok"), CUE_NONE)

    def test_de_escalation_is_silent(self):
        t = AlertTracker()
        t.update("critical")
        self.assertEqual(t.update("warning"), CUE_NONE)

    def test_mute_suppresses_everything(self):
        t = AlertTracker()
        t.update("ok")
        t.toggle_mute()
        self.assertEqual(t.update("critical"), CUE_NONE)

    def test_unmute_restores_sound(self):
        t = AlertTracker()
        t.update("ok")
        t.toggle_mute()
        t.update("critical")
        t.toggle_mute()
        self.assertEqual(t.update("critical"), CUE_NONE)
        self.assertEqual(t.update("offline"), CUE_DOUBLE_CHIRP)

    def test_toggle_mute_returns_new_state(self):
        t = AlertTracker()
        self.assertTrue(t.toggle_mute())
        self.assertFalse(t.toggle_mute())

    def test_reset_clears_history(self):
        t = AlertTracker()
        t.update("ok")
        t.reset()
        self.assertEqual(t.update("critical"), CUE_NONE)

    def test_last_status_tracks(self):
        t = AlertTracker()
        t.update("ok")
        t.update("warning")
        self.assertEqual(t.last_status, "warning")


class TestLedPattern(unittest.TestCase):

    def test_unmuted_uses_the_accent(self):
        self.assertEqual(led_pattern("green", 1.0), ACCENT_RGB["green"])

    def test_muted_dims(self):
        rgb = led_pattern("green", 1.0, muted=True, dim_when_muted=True)
        self.assertNotEqual(rgb, ACCENT_RGB["green"])
        self.assertTrue(all(v < 128 for v in rgb))

    def test_muted_without_dimming_keeps_the_accent(self):
        self.assertEqual(
            led_pattern("green", 1.0, muted=True, dim_when_muted=False),
            ACCENT_RGB["green"])

    def test_is_valid_accent(self):
        self.assertTrue(is_valid_accent("green"))
        self.assertFalse(is_valid_accent("nope"))


if __name__ == "__main__":
    unittest.main()
