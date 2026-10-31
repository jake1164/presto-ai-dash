"""The Presto app survives a bad payload, a long uptime and a failing draw.

Three promises from the code review:

* a payload with a value of the wrong type never ends the poller or the app,
  and never leaves old numbers on screen looking current (the poller is the
  only thing that notices a lost daemon),
* touch, the alarm pop-to-top and the offline screen keep working across the
  rollover of ``time.ticks_ms`` after 12.4 days, and
* an error while drawing is shown once and the app carries on.

The wrap-around tests use the real arithmetic of the Presto's ticks: a counter
that rolls over at 2**30 and a ``ticks_diff`` that is only right for readings
less than 2**29 apart.
"""

import asyncio
import copy
import json
import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import presto_path  # noqa: F401
from device.test_presto_lazy import FIXTURE, load_app
from device.test_ui import Display

from cards import clean_id, parse_cards, parse_lines, tidy_line
from gestures import GestureRecogniser, TAP
from popup import PopToTop
from ticks import Uptime

RANGE = 2 ** 30
HALF = 2 ** 29


def ticks_diff(a, b):
    """MicroPython's time.ticks_diff on a port whose ticks wrap at 2**30."""
    return ((a - b + HALF) % RANGE) - HALF


class Clock:
    """A tick counter that wraps like the Presto's, driven by the test."""

    def __init__(self, start=0):
        self.total = start

    def ticks(self):
        return self.total % RANGE

    def advance(self, ms):
        self.total += ms


def make_app(module, clock=None):
    device = SimpleNamespace(
        display=Display(), update=lambda: None, NUM_LEDS=7,
        set_led_rgb=lambda index, r, g, b: None,
        wifi=SimpleNamespace(isconnected=lambda: True, connect=AsyncMock(return_value=True)),
        touch=SimpleNamespace(state=False, poll=lambda: None, x=120, y=120))
    app = module.App(device)
    clock = clock or Clock()
    for name, fake in (("_now_ms", clock.ticks), ("_ticks_diff", ticks_diff)):
        patcher = patch.object(module, name, side_effect=fake)
        patcher.start()
    return app, device, clock


class PatchedModule(unittest.TestCase):
    def setUp(self):
        self.module = load_app()
        self.addCleanup(patch.stopall)


# -- a payload with values of the wrong type ---------------------------------

HOSTILE = [None, True, 0, -1, 10 ** 30, 1.5, float("nan"), float("inf"), "", "x" * 300,
           "é中", [], ["a"], [1, 2], {}, {"a": 1}, [[]], [{}]]
FIXTURES = ("devices.json", "lazy.json", "long.json", "alerts_off.json", "master_off.json")


def leaves(node, path=()):
    if isinstance(node, dict):
        for key, value in node.items():
            yield from leaves(value, path + (key,))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from leaves(value, path + (index,))
    yield path, node


def pattern(path):
    return ".".join("[]" if isinstance(part, int) else part for part in path)


def first_of_each_shape(payload):
    """One path for every distinct key pattern, so a list's items are not all tried."""
    seen, chosen = set(), []
    for path, _ in leaves(payload):
        if path and pattern(path) not in seen:
            seen.add(pattern(path))
            chosen.append(path)
    return chosen


def parent_and_key(root, path):
    node = root
    for key in path[:-1]:
        node = node[key]
    return node, path[-1]


class HostilePayloadTests(PatchedModule):
    """Every value, one at a time, replaced by a value of the wrong type."""

    def run_payload(self, payload):
        app, _device, _clock = make_app(self.module)
        app._set_payload(payload)
        for _ in range(max(1, len(app.cards))):
            app._render()
            app._next_card()
        app._render()

    def test_no_single_wrong_value_stops_the_app_from_drawing(self):
        failures = []
        tried = 0
        for name in FIXTURES:
            path_to = os.path.join(os.path.dirname(FIXTURE), name)
            with open(path_to, encoding="utf-8") as handle:
                payload = json.load(handle)
            for path in first_of_each_shape(payload):
                for bad in HOSTILE:
                    mutated = copy.deepcopy(payload)
                    parent, key = parent_and_key(mutated, path)
                    parent[key] = copy.deepcopy(bad)
                    tried += 1
                    try:
                        with patch("builtins.print"):
                            self.run_payload(mutated)
                    except Exception as exc:
                        failures.append("%s %s <- %r: %s" % (name, pattern(path), bad,
                                                              type(exc).__name__))
        self.assertGreater(tried, 1000)
        self.assertEqual(failures[:10], [], "%d of %d combinations raised" % (len(failures), tried))

    def test_no_single_missing_value_stops_the_app_from_drawing(self):
        failures = []
        with open(FIXTURE, encoding="utf-8") as handle:
            payload = json.load(handle)
        for path in first_of_each_shape(payload):
            parent, key = parent_and_key(payload, path)
            if not isinstance(parent, dict):
                continue
            mutated = copy.deepcopy(payload)
            del parent_and_key(mutated, path)[0][key]
            try:
                with patch("builtins.print"):
                    self.run_payload(mutated)
            except Exception as exc:
                failures.append("%s: %s" % (pattern(path), type(exc).__name__))
        self.assertEqual(failures, [])

    def test_these_payloads_that_crashed_the_app_before_are_handled(self):
        with open(FIXTURE, encoding="utf-8") as handle:
            base = json.load(handle)
        cases = {
            "a card id that is a list": ("cards", 0, "id", ["a"]),
            "a source id that is a dict": ("sources", 0, "id", {"a": 1}),
            "a period that is text": ("sources", 2, "quota", "windows", 0, "period_minutes", "x" * 20),
            "an infinite error count": ("sources", 2, "metrics", "errors_today", float("inf")),
        }
        for label, spec in cases.items():
            with self.subTest(label):
                payload = copy.deepcopy(base)
                node = payload
                for key in spec[:-2]:
                    node = node[key]
                node[spec[-2]] = spec[-1]
                with patch("builtins.print"):
                    self.run_payload(payload)

    def test_the_source_list_may_be_any_wrong_type(self):
        with open(FIXTURE, encoding="utf-8") as handle:
            base = json.load(handle)
        for bad in (True, 5, "text", {"a": 1}, None):
            with self.subTest(bad=bad):
                payload = copy.deepcopy(base)
                payload["sources"] = bad
                with patch("builtins.print"):
                    self.run_payload(payload)


class IdAndLineTests(unittest.TestCase):
    def test_only_text_is_an_id(self):
        self.assertEqual(clean_id("cl"), "cl")
        for bad in (None, "", 5, True, ["a"], {"a": 1}, 1.5):
            self.assertIsNone(clean_id(bad))

    def test_a_card_never_takes_an_id_or_a_title_that_is_not_text(self):
        cards, error = parse_cards({"cards": [{"id": ["a"], "title": {"b": 1}, "status": "ok"}]})
        self.assertIsNone(error)
        self.assertEqual(cards[0].id, "unknown")
        self.assertEqual(cards[0].title, "Unknown")

    def test_lines_are_text_only_tidy_and_capped(self):
        raw = ["Spend 0.5", 5, None, ["x"], {"a": 1}, "Alerts: True"] + ["x"] * 100
        lines = parse_lines(raw)
        self.assertEqual(lines[:2], [tidy_line("Spend 0.5"), tidy_line("Alerts: True")])
        self.assertEqual(len(lines), 40)
        self.assertTrue(all(isinstance(line, str) for line in lines))
        self.assertEqual(parse_lines("not a list"), [])
        self.assertEqual(parse_lines(None), [])


class HostileCardDetailTests(PatchedModule, unittest.IsolatedAsyncioTestCase):
    """The lazily fetched tiles and lines are as untrusted as the poll."""

    async def settle(self):
        for _ in range(5):
            await asyncio.sleep(0)

    async def open_card_with(self, detail):
        app, device, clock = make_app(self.module)
        with open(FIXTURE, encoding="utf-8") as handle:
            payload = json.load(handle)
        app.client.fetch_card = AsyncMock(return_value=detail)
        app._set_payload(payload)
        app.index = 1
        app._needs_redraw = True
        app._render()
        await self.settle()
        app._render()
        return app

    async def test_wrong_types_in_the_detail_are_cleaned_and_drawn(self):
        for detail in (
                {"tiles": ["x"], "lines": [5]},
                {"tiles": [5, None, {"label": [], "value": {}}], "lines": [None, ["a"]]},
                {"tiles": "text", "lines": {"a": 1}},
                {"tiles": [{"label": "ok", "value": "1", "tone": ["bad"]}], "lines": ["fine"]},
                {"tiles": {"label": "x"}, "lines": 7},
                {"tiles": [{"label": "x" * 99, "value": "y" * 99}], "lines": ["z" * 999]}):
            with self.subTest(detail=detail):
                app = await self.open_card_with(detail)
                kept = app.card_details[app._current_card().id]
                self.assertTrue(all(isinstance(line, str) for line in kept["lines"]))
                for tile in kept["tiles"]:
                    self.assertEqual(set(tile), {"label", "value", "tone"})


# -- a bad poll, and a failing draw -------------------------------------------

class ContainmentTests(PatchedModule, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.app, self.device, self.clock = make_app(self.module)
        with open(FIXTURE, encoding="utf-8") as handle:
            self.payload = json.load(handle)
        self.app._set_payload(copy.deepcopy(self.payload))
        self.app._render()

    async def test_a_payload_the_app_cannot_use_is_refused_and_the_old_screen_kept(self):
        before = (self.app.cards, self.app.live_rows, self.app.index)
        self.app.client.fetch_async = AsyncMock(return_value=copy.deepcopy(self.payload))
        with patch.object(self.app, "_apply_alerts", side_effect=TypeError("boom")), \
                patch("builtins.print"):
            self.assertFalse(await self.app.poll_once())
        self.assertEqual((self.app.cards, self.app.live_rows, self.app.index), before)
        self.assertEqual(self.app.connection_error[0], "Bad data")
        self.assertEqual(self.app.connection_error[1], "TypeError")

    async def test_a_good_poll_after_a_refused_one_recovers(self):
        self.app.client.fetch_async = AsyncMock(return_value=copy.deepcopy(self.payload))
        with patch.object(self.app, "_apply_alerts", side_effect=TypeError("boom")), \
                patch("builtins.print"):
            await self.app.poll_once()
        self.assertTrue(await self.app.poll_once())
        self.assertIsNone(self.app.connection_error)

    async def test_the_poller_survives_a_poll_that_raises(self):
        calls = []

        async def poll():
            calls.append(1)
            if len(calls) < 3:
                raise ValueError("unexpected")
            return True

        async def stop_after_three(_seconds):
            if len(calls) >= 3:
                raise asyncio.CancelledError()

        with patch.object(self.app, "poll_once", side_effect=poll), \
                patch.object(self.module.asyncio, "sleep", side_effect=stop_after_three), \
                patch("builtins.print"):
            with self.assertRaises(asyncio.CancelledError):
                await self.app._network_loop()
        self.assertEqual(len(calls), 3)

    async def test_the_main_loop_survives_a_draw_that_raises_and_says_so_once(self):
        calls = []

        def render():
            calls.append(1)
            if len(calls) <= 3:
                raise TypeError("bad draw")

        async def idle():
            await asyncio.sleep(3600)

        messages = []
        with patch.object(self.app, "_render", side_effect=render), \
                patch.object(self.module, "draw_message",
                             side_effect=lambda *args, **kwargs: messages.append(args[3])), \
                patch.object(self.app, "_network_loop", side_effect=idle), \
                patch("builtins.print"):
            task = asyncio.ensure_future(self.app._run_live())
            try:
                for _ in range(3):  # three passes, each ending in the same error
                    self.app._needs_redraw = True
                    await asyncio.sleep(0.06)
                self.assertFalse(task.done(), "the loop ended")
                self.assertEqual(messages, ["TypeError"])  # said once, not three times
                self.assertEqual(self.app._loop_error, "TypeError")
                self.app._needs_redraw = True  # the next draw works
                await asyncio.sleep(0.06)
                self.assertIsNone(self.app._loop_error)
                self.assertGreaterEqual(len(calls), 4)
            finally:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

    async def test_a_stop_signal_is_not_swallowed_by_the_loop_guard(self):
        """The live preview ends the app by raising from touch.poll; that must get out."""
        class StopSignal(BaseException):
            pass

        def poll():
            raise StopSignal()

        async def idle():
            await asyncio.sleep(3600)

        self.device.touch.poll = poll
        with patch.object(self.app, "_network_loop", side_effect=idle):
            with self.assertRaises(StopSignal):
                await asyncio.wait_for(self.app._run_live(), 5)

    def test_the_previews_stop_signal_is_not_an_ordinary_exception(self):
        import live
        self.assertTrue(issubclass(live.Stop, BaseException))
        self.assertFalse(issubclass(live.Stop, Exception))

    async def test_a_different_error_is_shown_again(self):
        shown = []
        with patch.object(self.module, "draw_message",
                          side_effect=lambda *args, **kwargs: shown.append(args[3])), \
                patch("builtins.print"):
            self.app._loop_failed(TypeError("x"))
            self.app._loop_failed(TypeError("x"))
            self.app._loop_failed(ValueError("y"))
        self.assertEqual(shown, ["TypeError", "ValueError"])


# -- the rollover of the ticks -------------------------------------------------

class UptimeTests(unittest.TestCase):
    def test_it_counts_from_the_first_reading(self):
        clock = Clock(start=12345)
        uptime = Uptime(clock.ticks, ticks_diff)
        self.assertEqual(uptime.ms(), 0)
        clock.advance(1500)
        self.assertEqual(uptime.ms(), 1500)
        self.assertEqual(uptime.seconds(), 1)

    def test_it_keeps_counting_through_the_rollover(self):
        clock = Clock(start=RANGE - 5000)
        uptime = Uptime(clock.ticks, ticks_diff)
        uptime.ms()
        for _ in range(10):
            clock.advance(1000)
            uptime.ms()
        self.assertEqual(uptime.ms(), 10000)
        self.assertLess(clock.ticks(), 10000)  # the raw counter did wrap

    def test_it_stays_exact_for_months_of_uptime(self):
        clock = Clock()
        uptime = Uptime(clock.ticks, ticks_diff)
        uptime.ms()
        day = 24 * 3600 * 1000
        for _ in range(200):
            clock.advance(day)
            uptime.ms()
        self.assertEqual(uptime.ms(), 200 * day)
        self.assertEqual(uptime.seconds(), 200 * 24 * 3600)
        self.assertIsInstance(uptime.seconds(), int)

    def test_building_it_reads_no_clock(self):
        def no_clock():
            raise AssertionError("read at construction")
        Uptime(no_clock, ticks_diff)


class DiagnosticsStateTests(PatchedModule):
    """One probe function feeds the offline screen and the diagnostics header."""

    def test_the_offline_lines_stay_the_same(self):
        app, _device, clock = make_app(self.module)
        app._diag_lines()  # the first reading starts the uptime
        clock.advance(3600 * 1000)
        lines = app._diag_lines()
        self.assertEqual(lines[0], "fails 0  up 1h00m")
        self.assertIn("wifi up", lines[1])
        for line in lines:
            self.assertTrue(line.isascii())

    def test_the_state_carries_what_the_header_needs(self):
        app, _device, _clock = make_app(self.module)
        state = app._diag_state()
        self.assertEqual(state["fails"], 0)
        self.assertEqual(state["n"], 0)
        self.assertEqual(state["drops"], 0)
        self.assertIn("up", state)
        # Nothing has failed yet, so there is no error to report.
        self.assertNotIn("err", state)
        self.assertNotIn("st", state)

    def test_the_failure_count_is_never_reset(self):
        app, _device, _clock = make_app(self.module)
        app._note_failure("boom")
        app._note_failure("boom")
        self.assertEqual(app.fail_count, 2)
        self.assertEqual(app.fail_total, 2)
        # A good poll clears the consecutive count, never the total.
        app.fail_count = 0
        self.assertEqual(app.fail_total, 2)
        self.assertEqual(app._diag_state()["n"], 2)

    def test_the_client_is_told_where_to_get_the_diagnostics(self):
        app, _device, _clock = make_app(self.module)
        self.assertIsNotNone(app.client.diag_provider)
        self.assertEqual(app.client.diag_provider, app._diag_state)

    def test_a_probe_that_fails_does_not_break_the_state(self):
        app, device, _clock = make_app(self.module)

        def broken():
            raise RuntimeError("no radio here")

        device.wifi.isconnected = broken
        state = app._diag_state()
        self.assertNotIn("wifi", state)
        self.assertIn("fails", state)


class RolloverBehaviourTests(PatchedModule):
    def test_raw_ticks_would_break_the_debounce_across_the_wrap(self):
        """Why the app uses an uptime counter: subtracting raw ticks is wrong."""
        recogniser = GestureRecogniser(debounce_ms=180)
        before = RANGE - 5000
        recogniser.update(True, 100, 100, before)
        self.assertEqual(recogniser.update(False, 100, 100, before + 60), [TAP])
        after = (before + 60 + 20000) % RANGE
        recogniser.update(True, 100, 100, after)
        self.assertEqual(recogniser.update(False, 100, 100, after + 60), [])  # dead

    def tap(self, app, device, clock):
        device.touch.state = True
        app._poll_touch()
        clock.advance(60)
        device.touch.state = False
        return app._poll_touch()

    def test_touch_works_before_and_after_the_wrap(self):
        app, device, clock = make_app(self.module, Clock(RANGE - 5000))
        self.assertEqual(self.tap(app, device, clock), [TAP])
        clock.advance(20000)
        self.assertLess(clock.ticks(), 20000)
        self.assertEqual(self.tap(app, device, clock), [TAP])

    def test_touch_still_works_a_week_and_a_wrap_later(self):
        app, device, clock = make_app(self.module, Clock(RANGE - 10 ** 6))
        self.assertEqual(self.tap(app, device, clock), [TAP])
        for _ in range(30):
            clock.advance(24 * 3600 * 1000)
            app.uptime.ms()  # the loop reads it many times a second
        self.assertEqual(self.tap(app, device, clock), [TAP])

    def test_an_alarm_that_waits_through_the_wrap_still_pops(self):
        clock = Clock(RANGE - 10000)
        uptime = Uptime(clock.ticks, ticks_diff)
        popup = PopToTop()
        popup.observe("cl", "ok", uptime.seconds())
        clock.advance(1000)
        popup.observe("cl", "critical", uptime.seconds())
        # The alarmed row is below the visible region, so the list must move.
        rows = [({"id": "row%d" % number}, 40, False) for number in range(8)]
        layout = SimpleNamespace(rows=rows + [({"id": "cl"}, 40, False)], region=100, limit=400)
        action = None
        for _ in range(60):
            clock.advance(1000)
            action = popup.poll(uptime.seconds(), layout, 0) or action
        self.assertLess(clock.ticks(), 60000)  # the wrap happened inside the wait
        self.assertIsNotNone(action)
        self.assertEqual(action["kind"], "scroll_to")

    def test_the_offline_screen_survives_a_long_outage(self):
        app, _device, clock = make_app(self.module)
        app.uptime.ms()
        app.last_good_at = app.uptime.ms()
        app.connection_error = ("Daemon offline", "refused", False)
        app.failed_polls = 2
        self.assertIsNone(app._connection_screen())  # a fresh reading: no screen yet
        for day in range(1, 14):
            clock.advance(24 * 3600 * 1000)
            screen = app._connection_screen()
            self.assertEqual(screen, ("Daemon offline", "refused"), "after %d days" % day)
        self.assertLess(clock.ticks(), 13 * 24 * 3600 * 1000)  # and the ticks wrapped

    def test_the_uptime_line_keeps_counting_after_a_week(self):
        app, _device, clock = make_app(self.module)
        app.started_at = None
        app._diag_lines()
        for _ in range(8):
            clock.advance(24 * 3600 * 1000)
            app.uptime.ms()
        line = app._diag_lines()[0]
        self.assertIn("up 192h00m", line)


if __name__ == "__main__":
    unittest.main()
