"""The device's alert behaviour: sound, the light, and the pop-to-top.

These drive the real presto/main.py app against a fake device, with a
synthetic payload. No provider is contacted and no real reading is read.
"""

import importlib.util
import os
import sys
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import presto_path
from alerts import rgb_for_accent
from net import embedded_payload
from test_ui import Display


class DeviceAlertTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location(
            "device_alert_app_for_test",
            os.path.join(presto_path.PRESTO_DIR, "main.py"))
        self.module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {"config": SimpleNamespace(STATIC_DEMO=False)}):
            spec.loader.exec_module(self.module)
        self.leds = []
        self.device = SimpleNamespace(
            display=Display(), update=lambda: None, NUM_LEDS=7,
            set_led_rgb=lambda index, r, g, b: self.leds.append((r, g, b)),
            wifi=SimpleNamespace(isconnected=lambda: True,
                                  connect=AsyncMock(return_value=True)),
            touch=SimpleNamespace(state=False, poll=lambda: None, x=0, y=0))
        self.app = self.module.App(self.device)
        # MicroPython's sleep helper, so a cue does not really wait.
        if not hasattr(time, "sleep_ms"):
            time.sleep_ms = lambda ms: None
        self.addCleanup(lambda: delattr(time, "sleep_ms"))
        self.now = [0]
        clock = patch.object(self.module, "_now_ms", side_effect=lambda: self.now[0])
        clock.start()
        self.addCleanup(clock.stop)
        ticks = patch.object(self.module, "_ticks_diff", side_effect=lambda a, b: a - b)
        ticks.start()
        self.addCleanup(ticks.stop)
        self.tones = []
        self.app._buzzer = SimpleNamespace(
            set_tone=lambda *args: self.tones.append(args))
        self.app.client.fetch_async = AsyncMock(return_value=self.payload())

    # -- helpers ------------------------------------------------------------

    def payload(self, statuses=None, alerts=None, count=3):
        """A payload with one source per status, in the given order."""
        statuses = statuses or ["ok", "ok", "ok"]
        payload = embedded_payload()
        payload["card_order"] = "accounts"
        payload["sources"] = [
            {"id": "s%d" % index, "type": "codex", "name": "Service %d" % index,
             "status": status,
             "quota": {"windows": [{"label": "Weekly", "period_minutes": 10080,
                                    "used_percent": 10, "state": "available",
                                    "resets_at": "2026-10-25T12:00:00+00:00",
                                    "observed_at": "2026-10-20T11:55:00+00:00"}]}}
            for index, status in enumerate(statuses)]
        if alerts is not None:
            payload["alerts"] = alerts
        return payload

    async def poll(self, payload):
        self.app.client.fetch_async.return_value = payload
        await self.app.poll_once()

    def led(self):
        self.leds = []
        self.app._update_leds()
        return self.leds[0] if self.leds else None

    @property
    def chirps(self):
        """How many tones were played; a double chirp is two of them."""
        return len([tone for tone in self.tones if tone[0] > 0])

    # -- the sound ----------------------------------------------------------

    async def test_the_first_reading_is_silent(self):
        await self.poll(self.payload(["critical", "ok", "ok"]))
        self.assertEqual(self.tones, [])

    async def test_a_service_that_gets_worse_chirps_once(self):
        await self.poll(self.payload(["ok", "ok", "ok"]))
        await self.poll(self.payload(["warning", "ok", "ok"]))
        self.assertEqual(self.chirps, 1)
        # The warning persists: no second chirp.
        await self.poll(self.payload(["warning", "ok", "ok"]))
        self.assertEqual(self.chirps, 1)

    async def test_a_second_service_chirps_while_the_first_is_critical(self):
        await self.poll(self.payload(["ok", "ok", "ok"]))
        await self.poll(self.payload(["critical", "ok", "ok"]))
        # A critical service plays a double chirp: two tones.
        self.assertEqual(self.chirps, 2)
        await self.poll(self.payload(["critical", "warning", "ok"]))
        self.assertEqual(self.chirps, 3)

    async def test_a_recovery_is_silent(self):
        await self.poll(self.payload(["ok", "ok", "ok"]))
        await self.poll(self.payload(["critical", "ok", "ok"]))
        await self.poll(self.payload(["ok", "ok", "ok"]))
        self.assertEqual(self.chirps, 2)

    async def test_quiet_hours_silence_the_sound(self):
        await self.poll(self.payload(["ok", "ok", "ok"],
                                     alerts={"quiet": True, "sound": True,
                                             "pop_to_top": True}))
        await self.poll(self.payload(["critical", "ok", "ok"],
                                     alerts={"quiet": True, "sound": True,
                                             "pop_to_top": True}))
        self.assertEqual(self.tones, [])
        self.assertTrue(self.app.quiet)

    async def test_the_sound_switch_silences_the_sound(self):
        await self.poll(self.payload(["ok", "ok", "ok"],
                                     alerts={"quiet": False, "sound": False,
                                             "pop_to_top": True}))
        await self.poll(self.payload(["critical", "ok", "ok"],
                                     alerts={"quiet": False, "sound": False,
                                             "pop_to_top": True}))
        self.assertEqual(self.tones, [])

    # -- the light ----------------------------------------------------------

    async def test_the_light_shows_the_worst_service(self):
        await self.poll(self.payload(["ok", "critical", "ok"]))
        self.assertEqual(self.led(), rgb_for_accent("red", self.app.led_brightness))

    async def test_in_use_is_never_shown_over_a_warning(self):
        await self.poll(self.payload(["active", "warning", "ok"]))
        self.assertEqual(self.led(), rgb_for_accent("yellow", self.app.led_brightness))

    async def test_quiet_hours_dim_the_light(self):
        await self.poll(self.payload(["ok", "ok", "ok"]))
        bright = self.led()
        await self.poll(self.payload(["ok", "ok", "ok"],
                                     alerts={"quiet": True, "sound": True,
                                             "pop_to_top": True}))
        dim = self.led()
        self.assertLess(dim[1], bright[1])

    # -- the payload --------------------------------------------------------

    async def test_a_junk_alert_setting_is_ignored(self):
        await self.poll(self.payload(["ok", "ok", "ok"], alerts="nonsense"))
        self.assertFalse(self.app.quiet)
        self.assertTrue(self.app.alert_sound)
        self.assertTrue(self.app.pop_to_top)

    async def test_a_missing_alert_setting_keeps_the_defaults(self):
        await self.poll(self.payload(["ok", "ok", "ok"]))
        self.assertFalse(self.app.quiet)
        self.assertTrue(self.app.alert_sound)
        self.assertTrue(self.app.pop_to_top)

    # -- the pop to the top -------------------------------------------------

    async def test_home_scrolls_to_a_service_that_got_worse(self):
        await self.poll(self.payload(["ok"] * 8))
        self.assertEqual(self.app.home_scroll.offset, 0)
        await self.poll(self.payload(["ok", "ok", "ok", "ok", "ok", "critical",
                                       "ok", "ok"]))
        # The alarm must persist before the list moves.
        self.now[0] = 10_000
        self.app._poll_popup()
        self.assertEqual(self.app.home_scroll.offset, 0)
        # Half a minute later the list moves to the alarmed service.
        self.now[0] = 45_000
        self.app._poll_popup()
        self.assertGreater(self.app.home_scroll.offset, 0)
        # The service's row is on screen.
        rows = self.app.live_rows
        row = next(row for row in rows if row["id"] == "s5")
        self.assertIsNotNone(row)

    async def test_the_list_does_not_move_while_the_user_is_touching_it(self):
        await self.poll(self.payload(["ok"] * 8))
        await self.poll(self.payload(["ok"] * 7 + ["critical"]))
        self.now[0] = 45_000
        self.device.touch.state = True
        self.app._poll_touch()
        self.app._poll_popup()
        self.assertEqual(self.app.home_scroll.offset, 0)

    async def test_nothing_moves_on_a_card(self):
        await self.poll(self.payload(["ok"] * 8))
        await self.poll(self.payload(["ok"] * 7 + ["critical"]))
        self.now[0] = 45_000
        self.app.index = 1  # a card, not Home
        self.app._poll_popup()
        self.assertEqual(self.app.home_scroll.offset, 0)


if __name__ == "__main__":
    unittest.main()
