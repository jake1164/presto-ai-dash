"""Claude's reading age has its own colour and never hides the reset."""

from copy import deepcopy
from datetime import timedelta
import unittest

import presto_path
from alerts import rgb_for_accent
from home import source_rows, update_note
from ui import INK_DIM, Palette, draw_sources
import test_home
from test_ui import Display
from test_claude_usage_cache_stale import NOW, report
from presto_ai_dash.dashboard import device_view
from presto_ai_dash.quotas import present_quota

HOME_NOW = test_home.HOME_NOW

class UpdateAgeTests(unittest.TestCase):
    def test_activity_does_not_change_the_configured_cache_freshness(self):
        value = report(cache_stale=7200, export_age=None, cache_age=timedelta(minutes=20))
        value["local_activity"] = {"available": True, "last_request_at": NOW.isoformat()}
        before = deepcopy(value)
        shown = present_quota(value, NOW)
        self.assertEqual(shown["last_update"], {"age_seconds": 1200, "in_use": True})
        self.assertEqual(shown["windows"][0]["state"], "available")
        self.assertEqual(value, before)
        later = present_quota(value, NOW + timedelta(minutes=1))
        self.assertEqual(later["last_update"]["age_seconds"], 1260)

    def test_unknown_activity_stays_idle_and_missing_quota_has_no_age(self):
        value = report(cache_stale=7200, export_age=None)
        self.assertFalse(present_quota(value, NOW)["last_update"]["in_use"])
        for window in value["windows"]:
            window["used_percent"] = None
        self.assertNotIn("last_update", present_quota(value, NOW))

    def test_last_update_uses_the_latest_measured_reading(self):
        value = report(cache_stale=7200, cache_age=timedelta(minutes=40),
                       export_age=timedelta(minutes=2))
        self.assertEqual(present_quota(value, NOW)["last_update"]["age_seconds"], 120)

    def test_device_projection_keeps_only_the_two_public_fields(self):
        payload = {"sources": [{"quota": {"last_update": {
            "age_seconds": 900, "in_use": True, "private": "discard"}}}]}
        self.assertEqual(device_view(payload)["sources"][0]["quota"]["last_update"],
                         {"age_seconds": 900, "in_use": True})

    def row(self, age, in_use=True, **window_changes):
        window = {"label": "Weekly", "period_minutes": 10080, "used_percent": 25,
                  "state": "available", "resets_at": (HOME_NOW + timedelta(days=5)).isoformat(),
                  "observed_at": (HOME_NOW - timedelta(seconds=age)).isoformat()}
        window.update(window_changes)
        return source_rows({"updated_at": HOME_NOW.isoformat(), "sources": [
            {"id": "c", "name": "Claude", "type": "claude", "status": "active",
             "quota": {"windows": [window], "hint": "In use - quota 2h old",
                       "last_update": {"age_seconds": age, "in_use": in_use}}}]})[0]

    def test_colours_threshold_and_reset_position(self):
        for age, active, rgb in ((60, False, INK_DIM), (3900, False, INK_DIM),
                                 (899, True, rgb_for_accent("green")),
                                 (900, True, rgb_for_accent("green")),
                                 (901, True, rgb_for_accent("yellow"))):
            with self.subTest(age=age, active=active):
                display = Display()
                draw_sources(display, Palette(display), [self.row(age, active)], 0, 2)
                test_home.HomeLayoutTests().assert_layout(display)
                update = next(i for i, r in enumerate(display.rows) if r[0].startswith("Updated"))
                reset = next(r for r in display.rows if r[0].startswith("Wk resets"))
                self.assertEqual(display.text_pens[update], rgb)
                self.assertLess(display.rows[update][1], reset[1])
                self.assertNotIn("In use - quota 2h old", [r[0] for r in display.rows])

    def test_an_old_quota_keeps_its_reset_and_a_lost_connection_greys_the_age(self):
        display = Display()
        draw_sources(display, Palette(display), [self.row(7201, state="stale")], 0, 2,
                     stale=True)
        updates = [(r, pen) for r, pen in zip(display.rows, display.text_pens)
                   if r[0].startswith("Updated")]
        self.assertEqual(updates[0][1], INK_DIM)
        self.assertTrue(any(r[0].startswith("Wk resets") for r in display.rows))

    def test_expired_quota_shows_awaiting_refresh_and_never_an_old_percentage(self):
        display = Display()
        row = self.row(7201, state="stale", resets_at=(HOME_NOW - timedelta(minutes=1)).isoformat())
        draw_sources(display, Palette(display), [row], 0, 2)
        texts = [r[0] for r in display.rows]
        self.assertIn("awaiting refresh", texts)
        self.assertIn("--", texts)
        self.assertNotIn("25%", texts)

    def test_formats_and_invalid_readings(self):
        for age, label in ((0, "0m"), (59, "0m"), (60, "1m"), (3599, "59m"),
                           (3600, "1h"), (3900, "1h:05m"), (90000, "25h")):
            self.assertEqual(update_note({"age_seconds": age})["text"], "Updated " + label)
        for value in (None, {}, {"age_seconds": True}, {"age_seconds": -1},
                      {"age_seconds": float("inf")}, {"age_seconds": "60"}):
            self.assertIsNone(update_note(value))


if __name__ == "__main__":
    unittest.main()
