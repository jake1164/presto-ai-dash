"""The bell on a service's card: the write, the drawing, and the tap.

These drive the real presto/main.py app against a fake device, with
synthetic payloads. No provider is contacted and no real reading is read.
"""

import asyncio
import importlib.util
import os
import sys
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import presto_path
from net import NetworkUnavailable, StatusClient, StatusError, Unauthorised
from device.test_ui import Display


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class PostAlertTests(unittest.IsolatedAsyncioTestCase):
    """presto/net.py post_alert: a bounded async write."""

    async def _server(self, answer):
        """A server that answers with these raw bytes."""
        async def handler(reader, writer):
            await reader.read(200)
            writer.write(answer)
            await writer.drain()
            writer.close()
        server = await asyncio.start_server(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        return server, port

    async def test_the_write_is_answered(self):
        body = b'{"ok": true, "alerts": false, "master": true}'
        answer = (b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                  b"Content-Length: %d\r\n\r\n%s" % (len(body), body))
        server, port = await self._server(answer)
        try:
            client = StatusClient("127.0.0.1", port, "tok", timeout=2)
            result = await client.post_alert("service1", False)
        finally:
            server.close()
        self.assertEqual(result, {"ok": True, "alerts": False, "master": True})

    async def test_the_request_names_the_service_and_sends_the_state(self):
        seen = {}

        async def handler(reader, writer):
            seen["request"] = await reader.read(400)
            answer = b'{"ok": true, "alerts": true, "master": true}'
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n%s" %
                         (len(answer), answer))
            await writer.drain()
            writer.close()

        server = await asyncio.start_server(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            client = StatusClient("127.0.0.1", port, "tok", timeout=2)
            await client.post_alert("svc-1", True)
        finally:
            server.close()
        request = seen["request"]
        self.assertIn(b"POST /api/v1/alerts/svc-1 HTTP/1.1", request)
        self.assertIn(b"Authorization: Bearer tok", request)
        self.assertIn(b'{"enabled": true}', request)
        self.assertIn(b"Content-Type: application/json", request)

    async def test_a_refused_connection_is_a_network_failure(self):
        client = StatusClient("127.0.0.1", 9, "t", timeout=1)
        with self.assertRaises(NetworkUnavailable) as caught:
            await client.post_alert("svc", False)
        # Cut at 30 characters; CPython's longer exception name cuts the stage.
        self.assertIn(caught.exception.reason.rsplit("@", 1)[-1], ("conn", "co"))

    async def test_a_stalled_server_times_out(self):
        async def handler(reader, writer):
            await reader.read(1)
            await asyncio.sleep(5)
        server = await asyncio.start_server(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            client = StatusClient("127.0.0.1", port, "t", timeout=1)
            with self.assertRaises(NetworkUnavailable) as caught:
                await client.post_alert("svc", False)
        finally:
            server.close()
        self.assertIn("Timeout", caught.exception.reason)

    async def test_a_short_body_is_a_network_failure(self):
        answer = b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\n12345"
        server, port = await self._server(answer)
        try:
            client = StatusClient("127.0.0.1", port, "t", timeout=2)
            with self.assertRaises(NetworkUnavailable) as caught:
                await client.post_alert("svc", False)
        finally:
            server.close()
        self.assertEqual(caught.exception.reason, "short body @body")

    async def test_a_refused_write_is_a_status_error(self):
        answer = b"HTTP/1.1 400 Bad Request\r\nContent-Length: 2\r\n\r\n{}"
        server, port = await self._server(answer)
        try:
            client = StatusClient("127.0.0.1", port, "t", timeout=2)
            with self.assertRaises(StatusError):
                await client.post_alert("svc", False)
        finally:
            server.close()

    async def test_a_rejected_token_is_unauthorised(self):
        answer = b"HTTP/1.1 401 Unauthorized\r\nContent-Length: 2\r\n\r\n{}"
        server, port = await self._server(answer)
        try:
            client = StatusClient("127.0.0.1", port, "t", timeout=2)
            with self.assertRaises(Unauthorised):
                await client.post_alert("svc", False)
        finally:
            server.close()

    async def test_an_unusable_service_id_is_refused_before_the_write(self):
        client = StatusClient("127.0.0.1", 9, "t", timeout=1)
        for service in ("", "a b", "a/b", "x" * 200, None):
            with self.subTest(service=service):
                with self.assertRaises(StatusError):
                    await client.post_alert(service, False)


class BellHitTests(unittest.TestCase):
    """The tap area of the bell."""

    def test_the_top_right_corner_is_the_bell(self):
        from ui import bell_hit
        self.assertTrue(bell_hit(200, 10))
        self.assertTrue(bell_hit(239, 47))
        self.assertTrue(bell_hit(192, 0))
        self.assertFalse(bell_hit(191, 10))
        self.assertFalse(bell_hit(200, 48))
        self.assertFalse(bell_hit(240, 10))
        self.assertFalse(bell_hit(10, 10))
        self.assertFalse(bell_hit(None, None))


class BellDrawingTests(unittest.TestCase):
    """The bell, its label, and the title that must not reach it."""

    def card(self, title="Service", alerts=None, message=None):
        from cards import Card
        from ui import Palette, draw_card
        display = Display()
        card = Card("svc", title, "ok", "12%", "Weekly")
        draw_card(display, Palette(display), card, 0, 2, alerts=alerts,
                  message=message)
        return display

    def test_the_label_says_what_the_bell_does(self):
        from ui import bell_label
        self.assertEqual(bell_label({"enabled": True, "master_off": False}),
                         "ALERTS ON")
        self.assertEqual(bell_label({"enabled": False, "master_off": False}),
                         "ALERTS OFF")
        self.assertEqual(bell_label({"enabled": False, "master_off": True}),
                         "ALERTS OFF (ALL)")
        self.assertIsNone(bell_label(None))

    def test_the_labels_are_ascii_and_fit_the_line(self):
        for label in ("ALERTS ON", "ALERTS OFF", "ALERTS OFF (ALL)"):
            with self.subTest(label=label):
                self.assertTrue(label.isascii())
                self.assertLessEqual(len(label), 34)

    def test_the_title_never_reaches_the_bell(self):
        display = self.card(title="A very long service name indeed",
                            alerts={"enabled": True, "master_off": False})
        bell_x = 240 - 8 - 12
        for text, x, y, _wrap, scale, _font in display.rows:
            if y < 20 and scale == 2:
                self.assertLessEqual(x + display.measure_text(text, scale),
                                     bell_x - 4, text)

    def test_the_bell_is_drawn_in_the_corner(self):
        display = self.card(alerts={"enabled": True, "master_off": False})
        rectangles = display.rectangles
        self.assertTrue(any(args[0] >= 220 and args[1] <= 18
                            for _pen, args in rectangles))

    def test_a_message_is_drawn_above_the_dots(self):
        display = self.card(message="COULD NOT SAVE - DAEMON OFFLINE")
        texts = [row for row in display.rows if row[0] == "COULD NOT SAVE - DAEMON OFFLINE"]
        self.assertEqual(len(texts), 1)
        self.assertLess(texts[0][2], 240 - 24)

    def test_the_message_is_never_wider_than_the_line(self):
        display = self.card(message="COULD NOT SAVE - DAEMON OFFLINE")
        for text, x, y, wrap, scale, _font in display.rows:
            self.assertLessEqual(display.measure_text(text, scale), wrap, text)


class BellAppTests(unittest.IsolatedAsyncioTestCase):
    """The app: the tap, the optimistic state, and the failure."""

    def setUp(self):
        spec = importlib.util.spec_from_file_location(
            "bell_app_for_test",
            os.path.join(presto_path.PRESTO_DIR, "main.py"))
        self.module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {"config": SimpleNamespace(STATIC_DEMO=False)}):
            spec.loader.exec_module(self.module)
        self.device = SimpleNamespace(
            display=Display(), update=lambda: None, NUM_LEDS=7,
            set_led_rgb=lambda index, r, g, b: None,
            wifi=SimpleNamespace(isconnected=lambda: True,
                                  connect=AsyncMock(return_value=True)),
            touch=SimpleNamespace(state=False, poll=lambda: None, x=0, y=0))
        self.app = self.module.App(self.device)
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
        self.writes = []
        self.write_result = {"ok": True, "alerts": False, "master": True}
        self.write_error = None

    # -- helpers ------------------------------------------------------------

    def payload(self, alerts=None, master=True):
        payload = {
            "app": "presto-ai-dash", "version": "0.1.0",
            "updated_at": "2026-10-20T12:00:00+00:00",
            "overall_status": "ok", "card_order": "accounts",
            "cards": [{"id": "dash", "title": "AI Dash", "status": "ok",
                       "primary": "1 live source", "secondary": "", "detail": []},
                      {"id": "svc", "title": "Service", "status": "ok",
                       "primary": "12%", "secondary": "Weekly", "detail": []}],
            "sources": [{"id": "svc", "type": "codex", "name": "Service",
                         "status": "ok", "alerts": True}],
            "alerts": {"quiet": False, "sound": True, "pop_to_top": True,
                       "enabled": master},
        }
        if alerts is not None:
            payload["sources"][0]["alerts"] = alerts
        return payload

    async def poll(self, payload=None):
        self.app._set_payload(payload or self.payload())

    def tap(self, x, y):
        """A real tap: the gesture recogniser sees the press and release."""
        self.app.gestures.reset()
        self.device.touch.x = x
        self.device.touch.y = y
        self.app._touch_x = x
        self.app._touch_y = y
        self.now[0] += 100
        self.app.gestures.update(True, x, y, self.now[0])
        self.now[0] += 50
        gestures = self.app.gestures.update(False, x, y, self.now[0])
        self.app._handle_gestures(gestures)

    def swipe(self, x, y, dx):
        self.app.gestures.reset()
        self.now[0] += 100
        self.app.gestures.update(True, x, y, self.now[0])
        self.now[0] += 50
        self.app.gestures.update(True, x + dx, y, self.now[0])
        self.now[0] += 50
        gestures = self.app.gestures.update(False, x + dx, y, self.now[0])
        self.app._handle_gestures(gestures)

    def start_write(self, service, enabled):
        """Run the write the tap started, and return what it did."""
        self.writes.append((service, enabled))

    # -- the tap ------------------------------------------------------------

    async def test_a_tap_on_the_bell_writes_the_new_state(self):
        await self.poll()
        self.app.index = 1  # the service's card
        with patch.object(self.module.App, "_start_alert_write",
                          side_effect=self.start_write):
            self.tap(225, 12)
        self.assertEqual(self.writes, [("svc", False)])
        # The state is applied at once, before the daemon answers.
        self.assertFalse(self.app._card_alerts(self.app._current_card())["enabled"])

    async def test_a_tap_elsewhere_opens_the_detail(self):
        await self.poll()
        self.app.index = 1
        with patch.object(self.module.App, "_start_alert_write",
                          side_effect=self.start_write):
            self.tap(60, 120)
        self.assertEqual(self.writes, [])
        self.assertTrue(self.app.detail_open)

    async def test_a_swipe_over_the_bell_is_not_a_tap(self):
        await self.poll()
        self.app.index = 1
        with patch.object(self.module.App, "_start_alert_write",
                          side_effect=self.start_write):
            self.swipe(225, 12, -80)
        self.assertEqual(self.writes, [])
        self.assertFalse(self.app.detail_open)

    async def test_a_swipe_that_starts_on_the_bell_is_not_a_tap(self):
        await self.poll()
        self.app.index = 1
        with patch.object(self.module.App, "_start_alert_write",
                          side_effect=self.start_write):
            self.swipe(225, 12, 80)
        self.assertEqual(self.writes, [])

    async def test_the_bell_is_not_on_home(self):
        await self.poll()
        self.app.index = 0  # the Home card
        with patch.object(self.module.App, "_start_alert_write",
                          side_effect=self.start_write):
            self.tap(225, 12)
        self.assertEqual(self.writes, [])

    async def test_a_second_tap_while_the_write_is_on_its_way_is_ignored(self):
        await self.poll()
        self.app.index = 1
        self.app.alert_inflight.add("svc")
        with patch.object(self.module.App, "_start_alert_write",
                          side_effect=self.start_write):
            self.tap(225, 12)
        self.assertEqual(self.writes, [])
        self.assertFalse(self.app.detail_open)

    async def test_with_the_master_off_the_tap_says_why(self):
        await self.poll(self.payload(master=False))
        self.app.index = 1
        with patch.object(self.module.App, "_start_alert_write",
                          side_effect=self.start_write):
            self.tap(225, 12)
        self.assertEqual(self.writes, [])
        self.assertEqual(self.app.alert_message[0], "ALERTS OFF FOR ALL - SEE ADMIN")
        self.assertLessEqual(len(self.app.alert_message[0]), 34)

    async def test_a_failed_write_reverts_and_says_so(self):
        await self.poll()
        self.app.index = 1
        self.app.client.post_alert = AsyncMock(side_effect=OSError("no route"))

        async def go():
            self.tap(225, 12)
            await asyncio.sleep(0)
            await asyncio.sleep(0)

        await go()
        self.assertEqual(self.app.alert_message[0], "COULD NOT SAVE - DAEMON OFFLINE")
        self.assertLessEqual(len(self.app.alert_message[0]), 34)
        # The optimistic state is gone: the card shows what the daemon says.
        self.assertTrue(self.app._card_alerts(self.app._current_card())["enabled"])

    async def test_a_successful_write_keeps_the_new_state(self):
        await self.poll()
        self.app.index = 1
        self.app.client.post_alert = AsyncMock(
            return_value={"ok": True, "alerts": False, "master": True})

        async def go():
            self.tap(225, 12)
            await asyncio.sleep(0)
            await asyncio.sleep(0)

        await go()
        self.assertFalse(self.app._card_alerts(self.app._current_card())["enabled"])
        self.assertIsNone(self.app.alert_message)

    async def test_the_optimistic_state_is_held_and_then_expires(self):
        await self.poll()
        self.app.index = 1
        self.app.client.post_alert = AsyncMock(
            return_value={"ok": True, "alerts": False, "master": True})

        async def go():
            self.tap(225, 12)
            await asyncio.sleep(0)
            await asyncio.sleep(0)

        await go()
        # A payload that does not confirm it keeps the optimistic state.
        await self.poll(self.payload(alerts=True))
        self.assertFalse(self.app._card_alerts(self.app._current_card())["enabled"])
        # Twenty seconds later the payload wins again.
        self.now[0] += 21000
        self.app._expire_alerts()
        self.assertTrue(self.app._card_alerts(self.app._current_card())["enabled"])


class DeviceAlertLogicTests(unittest.IsolatedAsyncioTestCase):
    """Only the services whose alerts are on take part."""

    def setUp(self):
        spec = importlib.util.spec_from_file_location(
            "bell_logic_app_for_test",
            os.path.join(presto_path.PRESTO_DIR, "main.py"))
        self.module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {"config": SimpleNamespace(STATIC_DEMO=False)}):
            spec.loader.exec_module(self.module)
        self.device = SimpleNamespace(
            display=Display(), update=lambda: None, NUM_LEDS=7,
            set_led_rgb=lambda index, r, g, b: None,
            wifi=SimpleNamespace(isconnected=lambda: True,
                                  connect=AsyncMock(return_value=True)),
            touch=SimpleNamespace(state=False, poll=lambda: None, x=0, y=0))
        self.app = self.module.App(self.device)
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

    def payload(self, alerts, status="critical"):
        return {
            "app": "presto-ai-dash", "version": "0.1.0",
            "updated_at": "2026-10-20T12:00:00+00:00",
            "overall_status": "ok", "card_order": "accounts",
            "cards": [{"id": "dash", "title": "AI Dash", "status": "ok",
                       "primary": "1 live source", "secondary": "", "detail": []},
                      {"id": "svc", "title": "Service", "status": status,
                       "primary": "96%", "secondary": "Weekly", "detail": []}],
            "sources": [{"id": "svc", "type": "codex", "name": "Service",
                         "status": status, "alerts": alerts}],
            "alerts": {"quiet": False, "sound": True, "pop_to_top": True,
                       "enabled": True},
        }

    @property
    def chirps(self):
        return len([tone for tone in self.tones if tone[0] > 0])

    async def test_a_service_with_its_alerts_off_does_not_chirp(self):
        self.app._set_payload(self.payload(False, status="ok"))
        self.app._set_payload(self.payload(False, status="critical"))
        self.assertEqual(self.chirps, 0)

    async def test_a_service_that_gets_worse_chirps_when_its_alerts_are_on(self):
        """The control: the same change with the alerts on does sound."""
        self.app._set_payload(self.payload(True, status="ok"))
        self.app._set_payload(self.payload(True, status="critical"))
        self.assertEqual(self.chirps, 2, "a critical service plays two tones")

    async def test_a_service_with_its_alerts_off_does_not_colour_the_light(self):
        from alerts import rgb_for_accent
        self.app._set_payload(self.payload(False))
        self.app._update_leds()
        self.assertNotEqual(self.app.overall_accent, "red")

    async def test_turning_the_alerts_off_stops_contributing_at_once(self):
        self.app._set_payload(self.payload(True))
        self.assertEqual(self.app.overall_accent, "red")
        self.app._set_payload(self.payload(False))
        self.assertNotEqual(self.app.overall_accent, "red")

    async def test_turning_the_alerts_back_on_does_not_chirp_for_an_old_alarm(self):
        # The service gets worse while its alerts are off: the alarm
        # already exists, so turning them back on must not chirp for it.
        self.app._set_payload(self.payload(True, status="ok"))
        self.app._set_payload(self.payload(False, status="critical"))
        before = self.chirps
        self.app._set_payload(self.payload(True, status="critical"))
        self.assertEqual(self.chirps, before,
                         "an alarm that already existed must not chirp again")

    async def test_a_pending_pop_goes_when_the_alerts_are_turned_off(self):
        self.app._set_payload(self.payload(True, status="ok"))
        self.app._set_payload(self.payload(False, status="critical"))
        self.assertEqual(self.app.popup._pending, {})


class HomeMarkTests(unittest.TestCase):
    """The small mark on a Home row whose service has its alerts off."""

    def rows(self, alerts):
        payload = {"updated_at": "2026-10-20T12:00:00+00:00", "card_order": "accounts",
                   "sources": [{"id": "svc", "type": "codex", "name": "Codex",
                                "status": "ok", "alerts": alerts,
                                "quota": {"windows": [
                                    {"label": "Weekly", "period_minutes": 10080,
                                     "used_percent": 42, "state": "available"}]}}]}
        return home_rows(payload)

    def test_the_mark_is_drawn_only_when_the_alerts_are_off(self):
        from ui import Palette, draw_sources
        for alerts, expected in ((False, True), (True, False)):
            with self.subTest(alerts=alerts):
                display = Display()
                rows = self.rows(alerts)
                draw_sources(display, Palette(display), rows, 0, 1, status="ok")
                marked = any(pen == (140, 140, 140) and args[2] == 6
                             for pen, args in display.rectangles)
                self.assertEqual(marked, expected)

    def test_the_mark_never_overlaps_the_name_or_the_note(self):
        from ui import Palette, draw_sources
        display = Display()
        rows = self.rows(False)
        draw_sources(display, Palette(display), rows, 0, 1, status="ok")
        # The name is drawn on the first line; the mark follows it.
        names = [row for row in display.rows if row[0] == "Codex"]
        self.assertEqual(len(names), 1)
        marks = [args for _pen, args in display.rectangles if args[2] == 6]
        self.assertTrue(marks)
        mark_x = marks[0][0]
        self.assertGreaterEqual(mark_x, names[0][1])


def home_rows(payload):
    import home
    return home.source_rows(payload)


if __name__ == "__main__":
    unittest.main()
