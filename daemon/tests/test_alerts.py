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
    alert_rank,
    chirp_count,
    cue_for_status,
    is_valid_accent,
    led_brightness,
    led_pattern,
    rgb_for_accent,
    rgb_for_status,
    worst_accent,
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
    """The tracker decides when a cue plays, per service."""

    def test_first_reading_is_silent(self):
        """The device must not chirp at the user the moment it boots."""
        t = AlertTracker()
        self.assertEqual(t.update_one("a", "critical"), CUE_NONE)

    def test_escalation_chirps(self):
        t = AlertTracker()
        t.update_one("a", "ok")
        self.assertEqual(t.update_one("a", "warning"), CUE_CHIRP)

    def test_escalation_to_critical_double_chirps(self):
        t = AlertTracker()
        t.update_one("a", "warning")
        self.assertEqual(t.update_one("a", "critical"), CUE_DOUBLE_CHIRP)

    def test_no_repeat_while_status_holds(self):
        t = AlertTracker()
        t.update_one("a", "ok")
        t.update_one("a", "warning")
        self.assertEqual(t.update_one("a", "warning"), CUE_NONE)
        self.assertEqual(t.update_one("a", "warning"), CUE_NONE)

    def test_recovery_is_silent(self):
        t = AlertTracker()
        t.update_one("a", "warning")
        self.assertEqual(t.update_one("a", "ok"), CUE_NONE)

    def test_de_escalation_is_silent(self):
        t = AlertTracker()
        t.update_one("a", "critical")
        self.assertEqual(t.update_one("a", "warning"), CUE_NONE)

    def test_a_second_service_turning_bad_still_speaks(self):
        """One service being critical must not silence another one."""
        t = AlertTracker()
        t.update({"a": "ok", "b": "ok"})
        cues = t.update({"a": "critical", "b": "warning"})
        self.assertEqual(sorted(cues), [("a", CUE_DOUBLE_CHIRP), ("b", CUE_CHIRP)])

    def test_a_second_service_going_critical_chirps(self):
        t = AlertTracker()
        t.update({"a": "ok", "b": "ok"})
        t.update({"a": "critical", "b": "warning"})
        cues = t.update({"a": "critical", "b": "critical"})
        self.assertEqual(cues, [("b", CUE_DOUBLE_CHIRP)])

    def test_each_service_is_tracked_on_its_own(self):
        t = AlertTracker()
        t.update_one("a", "warning")
        # b has no history yet, so its first reading is a baseline.
        self.assertEqual(t.update_one("b", "critical"), CUE_NONE)
        self.assertEqual(t.update_one("a", "warning"), CUE_NONE)

    def test_mute_suppresses_everything(self):
        t = AlertTracker()
        t.update_one("a", "ok")
        t.toggle_mute()
        self.assertEqual(t.update_one("a", "critical"), CUE_NONE)

    def test_unmute_restores_sound(self):
        t = AlertTracker()
        t.update_one("a", "ok")
        t.toggle_mute()
        t.update_one("a", "critical")
        t.toggle_mute()
        self.assertEqual(t.update_one("a", "critical"), CUE_NONE)
        self.assertEqual(t.update_one("a", "offline"), CUE_DOUBLE_CHIRP)

    def test_toggle_mute_returns_new_state(self):
        t = AlertTracker()
        self.assertTrue(t.toggle_mute())
        self.assertFalse(t.toggle_mute())

    def test_reset_clears_history(self):
        t = AlertTracker()
        t.update_one("a", "ok")
        t.reset()
        self.assertEqual(t.update_one("a", "critical"), CUE_NONE)

    def test_last_status_tracks(self):
        t = AlertTracker()
        t.update_one("a", "ok")
        t.update_one("a", "warning")
        self.assertEqual(t.last_status, "warning")

    def test_last_status_is_the_worst_of_the_services(self):
        t = AlertTracker()
        t.update({"a": "warning", "b": "critical"})
        self.assertEqual(t.last_status, "critical")

    def test_quiet_hours_silence_the_sound(self):
        t = AlertTracker()
        t.update_one("a", "ok")
        t.quiet = True
        self.assertTrue(t.silenced)
        self.assertEqual(t.update_one("a", "critical"), CUE_NONE)

    def test_quiet_hours_do_not_touch_the_users_own_mute(self):
        t = AlertTracker()
        t.quiet = True
        self.assertFalse(t.muted)
        t.quiet = False
        self.assertFalse(t.silenced)

    def test_an_update_that_is_not_a_mapping_does_nothing(self):
        t = AlertTracker()
        self.assertEqual(t.update("critical"), [])
        self.assertEqual(t.update(None), [])


class TestAlertRank(unittest.TestCase):
    def test_rate_limited_is_worse_than_critical(self):
        """At 100% a service is rate limited, which is worse than critical."""
        self.assertGreater(alert_rank("rate_limited"), alert_rank("critical"))

    def test_offline_is_the_worst(self):
        self.assertGreater(alert_rank("offline"), alert_rank("rate_limited"))

    def test_in_use_is_below_a_warning(self):
        self.assertLess(alert_rank("active"), alert_rank("warning"))

    def test_an_unknown_status_is_the_worst(self):
        self.assertEqual(alert_rank("banana"), alert_rank("offline"))


class TestLedPriority(unittest.TestCase):
    def test_red_wins_over_everything(self):
        for status in ("critical", "rate_limited", "offline"):
            self.assertEqual(worst_accent(["ok", status, "warning"]), "red")

    def test_yellow_wins_over_blue_and_green(self):
        for status in ("warning", "near_limit"):
            self.assertEqual(worst_accent(["ok", status, "active"]), "yellow")

    def test_in_use_is_never_shown_over_a_warning(self):
        self.assertEqual(worst_accent(["active", "ok"]), "blue")
        self.assertEqual(worst_accent(["active", "near_limit"]), "yellow")

    def test_all_good_is_green(self):
        self.assertEqual(worst_accent(["ok", "ok"]), "green")

    def test_an_unknown_status_is_red(self):
        self.assertEqual(worst_accent(["ok", "banana"]), "red")

    def test_no_status_at_all_is_green(self):
        self.assertEqual(worst_accent([]), "green")

    def test_quiet_hours_dim_the_light_but_keep_the_colour(self):
        bright = led_brightness(1.0)
        dim = led_brightness(1.0, quiet=True)
        self.assertLess(dim, bright)
        self.assertEqual(rgb_for_accent("red", dim)[0], rgb_for_accent("red", 1.0)[0] * 0.4)


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
