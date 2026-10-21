"""The device's lazy card detail: fetch, cache, ignore, fall back, free.

Plan item 1d, tier 3: the summary the device polls no longer carries each
card's lines. When a card is opened the device asks for that card's detail
over the same authenticated API, draws the tiles as a grid, and keeps the
lines reachable. A response for a card that is no longer open is dropped, a
failure falls back to the summary text, and the parsed detail is freed when
the card closes.
"""

import importlib.util
import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import presto_path  # noqa: F401
from test_ui import Display

FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures",
                       "devices.json")


def load_app():
    """The real presto/main.py, with the device's own modules."""
    spec = importlib.util.spec_from_file_location(
        "lazy_app_for_test", os.path.join(presto_path.PRESTO_DIR, "main.py"))
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {"config": SimpleNamespace(STATIC_DEMO=False)}):
        spec.loader.exec_module(module)
    return module


class LazyAppTests(unittest.IsolatedAsyncioTestCase):
    """The app: the fetch, the cache, the ignore, the fallback, the free."""

    def setUp(self):
        self.module = load_app()
        self.device = SimpleNamespace(
            display=Display(), update=lambda: None, NUM_LEDS=7,
            set_led_rgb=lambda index, r, g, b: None,
            wifi=SimpleNamespace(isconnected=lambda: True,
                                  connect=AsyncMock(return_value=True)),
            touch=SimpleNamespace(state=False, poll=lambda: None, x=0, y=0))
        self.app = self.module.App(self.device)
        self.now = [0]
        clock = patch.object(self.module, "_now_ms", side_effect=lambda: self.now[0])
        clock.start()
        self.addCleanup(clock.stop)
        ticks = patch.object(self.module, "_ticks_diff", side_effect=lambda a, b: a - b)
        ticks.start()
        self.addCleanup(ticks.stop)
        self.fetched = []
        self.result = {"id": "cl", "tiles": [{"label": "tok 5h", "value": "1.2M",
                                              "tone": "normal"}],
                       "lines": ["Quota via export, 2 h old"]}
        self.error = None

    async def fetch_card(self, card_id):
        self.fetched.append(card_id)
        if self.error is not None:
            raise self.error
        return self.result

    def payload(self):
        return {
            "app": "presto-ai-dash", "version": "0.1.0",
            "updated_at": "2026-10-20T12:00:00+00:00",
            "overall_status": "ok", "card_order": "accounts",
            "cards": [{"id": "dash", "title": "AI Dash", "status": "ok",
                       "primary": "1 live source", "secondary": "", "detail": []},
                      {"id": "cl", "title": "Claude", "status": "ok",
                       "primary": "5h: 12%", "secondary": "Weekly: 66%",
                       "detail": []}],
            "sources": [{"id": "cl", "type": "claude", "name": "Claude",
                         "status": "ok", "alerts": True}],
            "alerts": {"quiet": False, "sound": True, "pop_to_top": True,
                       "enabled": True},
        }

    async def open_card(self):
        """The payload arrives and the Claude card is the open one."""
        self.app.client.fetch_card = self.fetch_card
        self.app._set_payload(self.payload())
        self.app.index = 1
        self.app._needs_redraw = True
        self.app._render()

    # -- the fetch -----------------------------------------------------------

    async def test_opening_a_card_fetches_its_detail(self):
        await self.open_card()
        await self._settle()
        self.assertEqual(self.fetched, ["cl"])
        self.assertIn("cl", self.app.card_details)

    async def test_the_fetch_is_not_repeated_within_the_poll_interval(self):
        await self.open_card()
        await self._settle()
        self.now[0] += 1000
        self.app._render()
        await self._settle()
        self.assertEqual(self.fetched, ["cl"])

    async def test_the_detail_is_fetched_again_after_one_poll_interval(self):
        await self.open_card()
        await self._settle()
        self.now[0] += self.app.poll_interval * 1000 + 100
        self.app._render()
        await self._settle()
        self.assertEqual(self.fetched, ["cl", "cl"])

    async def test_a_response_for_a_closed_card_is_ignored(self):
        await self.open_card()
        # The user swipes away while the answer is on its way.
        self.app.index = 0
        await self._settle()
        self.assertEqual(self.fetched, ["cl"])
        self.assertNotIn("cl", self.app.card_details)

    async def test_a_failure_falls_back_to_the_summary(self):
        self.error = RuntimeError("no daemon")
        await self.open_card()
        await self._settle()
        self.assertIn("cl", self.app.card_failed)
        self.assertNotIn("cl", self.app.card_details)

    async def test_the_detail_is_freed_when_the_card_closes(self):
        await self.open_card()
        await self._settle()
        self.assertIn("cl", self.app.card_details)
        self.app.index = 0
        self.app._free_details()
        self.assertEqual(self.app.card_details, {})

    async def test_the_home_card_is_never_fetched(self):
        self.app.client.fetch_card = self.fetch_card
        self.app._set_payload(self.payload())
        self.app.index = 0
        self.app._render()
        await self._settle()
        self.assertEqual(self.fetched, [])

    async def _settle(self):
        """Let the pending task run."""
        import asyncio
        for _ in range(4):
            await asyncio.sleep(0)
        await asyncio.sleep(0.01)


class LazyDrawingTests(unittest.TestCase):
    """The card draws the tiles as a grid and keeps the lines reachable."""

    def setUp(self):
        import json
        with open(FIXTURE, encoding="utf-8") as handle:
            self.payload = json.load(handle)

    def draw(self, **kwargs):
        from cards import parse_cards
        from ui import Palette, draw_card
        from test_presto_cards import RecordingFramebuffer

        cards, error = parse_cards(self.payload)
        self.assertIsNone(error)
        card = next(item for item in cards if item.id == "cl")
        display = RecordingFramebuffer()
        draw_card(display, Palette(display), card, 0, 7, **kwargs)
        return display

    def test_the_tiles_are_drawn_in_two_columns(self):
        tiles = [{"label": "tok 5h", "value": "1.2M", "tone": "normal"},
                 {"label": "tok 7d", "value": "19.4M", "tone": "normal"}]
        display = self.draw(tiles=tiles)
        texts = [call[0] for call in display.calls]
        self.assertIn("tok 5h", texts)
        self.assertIn("1.2M", texts)
        first = [call for call in display.calls if call[0] == "tok 5h"][0]
        second = [call for call in display.calls if call[0] == "tok 7d"][0]
        # Two columns: the second tile is to the right of the first.
        self.assertGreater(second[1], first[1] + 40)

    def test_the_label_is_above_the_value(self):
        tiles = [{"label": "tok 5h", "value": "1.2M", "tone": "normal"}]
        display = self.draw(tiles=tiles)
        label = [call for call in display.calls if call[0] == "tok 5h"][0]
        value = [call for call in display.calls if call[0] == "1.2M"][0]
        self.assertGreater(label[2], value[2] - 12)
        self.assertLess(label[2], value[2])

    def test_the_value_is_brighter_than_the_label(self):
        tiles = [{"label": "tok 5h", "value": "1.2M", "tone": "normal"}]
        display = self.draw(tiles=tiles)
        colours = {call[0]: call[6] for call in display.calls}
        self.assertNotEqual(colours["tok 5h"], colours["1.2M"])

    def test_a_loading_card_says_so_quietly(self):
        display = self.draw(tiles=[], loading=True)
        texts = [call[0] for call in display.calls]
        self.assertIn("loading detail", texts)

    def test_a_failed_card_says_the_detail_is_unavailable(self):
        display = self.draw(tiles=[], detail=[], failed=True)
        texts = [call[0] for call in display.calls]
        self.assertIn("detail unavailable", texts)

    def test_a_card_with_its_own_lines_does_not_say_they_are_missing(self):
        display = self.draw(tiles=[], failed=True)
        texts = [call[0] for call in display.calls]
        self.assertNotIn("detail unavailable", texts)

    def test_the_lines_are_drawn_below_the_tiles(self):
        tiles = [{"label": "tok 5h", "value": "1.2M", "tone": "normal"}]
        display = self.draw(tiles=tiles, detail=["Quota via export, 2 h old"])
        texts = [call[0] for call in display.calls]
        self.assertIn("Quota via export, 2 h old", texts)
        value = [call for call in display.calls if call[0] == "1.2M"][0]
        line = [call for call in display.calls
                if call[0] == "Quota via export, 2 h old"][0]
        self.assertGreater(line[2], value[2])

    def test_the_detail_view_shows_the_lines_instead_of_the_tiles(self):
        tiles = [{"label": "tok 5h", "value": "1.2M", "tone": "normal"}]
        display = self.draw(tiles=tiles, detail=["Quota via export, 2 h old"],
                            detail_open=True)
        texts = [call[0] for call in display.calls]
        self.assertNotIn("tok 5h", texts)
        self.assertIn("Quota via export, 2 h old", texts)


class NetTests(unittest.IsolatedAsyncioTestCase):
    """The card fetch on the wire: a bounded GET with the shared token."""

    async def test_an_unusable_card_id_is_refused_before_the_request(self):
        from net import StatusClient, StatusError
        client = StatusClient("127.0.0.1", 9, "t", timeout=1)
        for card_id in ("", "a b", "a/b", "x" * 200, None):
            with self.subTest(card_id=card_id):
                with self.assertRaises(StatusError):
                    await client.fetch_card(card_id)

    def test_the_status_path_asks_for_the_lazy_summary(self):
        module = load_app()
        device = SimpleNamespace(
            display=Display(), update=lambda: None, NUM_LEDS=7,
            set_led_rgb=lambda index, r, g, b: None,
            wifi=SimpleNamespace(isconnected=lambda: True,
                                  connect=AsyncMock(return_value=True)),
            touch=SimpleNamespace(state=False, poll=lambda: None, x=0, y=0))
        app = module.App(device)
        self.assertIn("cards=lazy", app.client.url)


if __name__ == "__main__":
    unittest.main()
