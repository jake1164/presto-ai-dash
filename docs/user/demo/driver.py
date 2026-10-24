"""Runs the real Presto app in the browser, on the scripted demo.

This file runs under MicroPython (WebAssembly), from the documentation page and
from tools/check_demo_wasm.mjs, which is how the same code is tested. It holds
no app logic: the screens, gestures, timers and alerts are the device's own
modules in /lib, and App._tick() is the pass the device runs every 20 ms. What
is here is only the stand-in for the hardware (a screen buffer, a touch queue,
LEDs) and for the daemon (a client that reads the sample data the build wrote).

JavaScript provides, as globals the page defines:
    stepJson(i)       the i-th step of demo.json, as a JSON string
    takeTouch()       the next touch event as "kind,x,y", or "" for none
    pushFrame(bytes)  a finished 240x240 RGB frame
    setLed(r, g, b)   the colour of the first LED
    checkFrame(scene, card, bytes)   only for render_screens (the Node test)
"""

import sys
import json
import time

if "/lib" not in sys.path:
    # Emscripten's own /home would otherwise shadow the app's home module.
    sys.path.insert(0, "/lib")

import asyncio  # noqa: E402
import js  # noqa: E402
import net  # noqa: E402
import ai_dash_app as main  # noqa: E402
from framebuffer import Framebuffer  # noqa: E402


def run_now(coro):
    """Run a coroutine that never waits to its end.

    The demo's client answers at once, so nothing it awaits ever suspends.
    A coroutine that does suspend is a bug here, not something to spin on.
    """
    try:
        for _ in range(1000):
            coro.send(None)
    except StopIteration as done:
        return done.value
    raise RuntimeError("a demo coroutine suspended")


# The app starts card-detail fetches with create_task. There is no event loop
# to hand them to, and none is needed: they finish at once.
asyncio.create_task = run_now


class Touch(object):
    """The touch panel: the page queues events, the app reads one per pass."""

    def __init__(self):
        self.state = False
        self.x = 0
        self.y = 0

    def poll(self):
        event = js.takeTouch()
        if event:
            kind, x, y = str(event).split(",")
            self.x = int(x)
            self.y = int(y)
            self.state = kind != "up"


class Wifi(object):
    def isconnected(self):
        return True

    async def connect(self, timeout=10, retries=1):
        return True


class Presto(object):
    NUM_LEDS = 7

    def __init__(self, display):
        self.display = display
        self.touch = Touch()
        self.wifi = Wifi()
        self.leds = [(0, 0, 0)] * self.NUM_LEDS
        self.frames = 0

    def set_led_rgb(self, index, r, g, b):
        self.leds[index] = (r, g, b)

    def update(self):
        self.frames += 1
        js.pushFrame(bytes(self.display.pixels))
        red, green, blue = self.leds[0]
        js.setLed(red, green, blue)


class DemoClient(object):
    """The app's client, answering from the build's demo.json."""

    def __init__(self, loop_seconds, step_seconds, count):
        self.loop_seconds = loop_seconds
        self.step_seconds = step_seconds
        self.count = count
        self.second = 0.0
        # The "Stale" switch: every request fails while set, as a lost daemon.
        self.simulate_failure = False
        # The bell, per service: in memory, and nowhere else.
        self.alerts = {}

    def advance(self, seconds):
        self.second = (self.second + seconds) % self.loop_seconds

    def jump(self, second):
        self.second = float(second) % self.loop_seconds

    def _step(self):
        index = int(self.second // self.step_seconds) % self.count
        return json.loads(js.stepJson(index))

    def _refuse_if_failing(self):
        if self.simulate_failure:
            error = net.NetworkUnavailable("simulated by the page")
            # The offline screen shows this, as it shows a real client's reason.
            error.reason = "simulated loss"
            raise error

    async def fetch_async(self):
        self._refuse_if_failing()
        payload = self._step()["payload"]
        for source in payload.get("sources", ()):
            if isinstance(source, dict) and source.get("id") in self.alerts:
                source["alerts"] = self.alerts[source["id"]]
        return payload

    async def fetch_card(self, card_id):
        self._refuse_if_failing()
        return self._step()["details"].get(card_id)

    async def post_alert(self, service, enabled):
        self._refuse_if_failing()
        enabled = bool(enabled)
        self.alerts[service] = enabled
        return {"ok": True, "alerts": enabled, "master": True}


class Driver(object):
    """One running app. The page calls tick() about every 20 ms."""

    def __init__(self, loop_seconds, step_seconds, count):
        self.display = Framebuffer()
        self.presto = Presto(self.display)
        self.client = DemoClient(loop_seconds, step_seconds, count)
        self.app = main.App(self.presto)
        self.app.client = self.client
        self.app.started_at = self.app.uptime.ms()
        self.next_poll = time.ticks_ms()

    def poll_soon(self):
        """Poll on the next tick: after a scene jump or the Stale switch."""
        self.next_poll = time.ticks_ms()

    def tick(self, elapsed_ms):
        app = self.app
        self.client.advance(elapsed_ms / 1000.0)
        now = time.ticks_ms()
        if time.ticks_diff(now, self.next_poll) >= 0:
            try:
                good = run_now(app.poll_once())
            except Exception as exc:
                app._poll_failed("Poll error", type(exc).__name__)
                good = False
            wait = app.poll_interval if good else app.retry_interval
            self.next_poll = time.ticks_add(now, wait * 1000)
        app._tick()


driver = None


def start(loop_seconds, step_seconds, count):
    global driver
    driver = Driver(loop_seconds, step_seconds, count)


def tick(elapsed_ms):
    driver.tick(elapsed_ms)


def jump(second):
    driver.client.jump(second)
    driver.poll_soon()


def set_failure(failing):
    driver.client.simulate_failure = bool(failing)
    driver.poll_soon()


def render_screens(scenes, step_seconds):
    """Draw Home and every card for each scene, as build_demo_site.py does.

    scenes is a list of (name, start second). Each finished screen goes to the
    page's checkFrame(scene, card id, pixels), which hashes it. Only the Node
    smoke test calls this.
    """
    display = Framebuffer()
    presto = Presto(display)
    presto.update = lambda: None
    app = main.App(presto)
    app._maybe_load_detail = lambda: None
    for name, start in scenes:
        step = json.loads(js.stepJson(int(start // step_seconds)))
        app._set_payload(step["payload"])
        app.card_details.clear()
        for card in app.cards:
            detail = step["details"].get(card.id)
            if detail is None:
                continue
            app.card_details[card.id] = {"tiles": list(detail.get("tiles") or ()),
                                         "lines": list(detail.get("lines") or ()),
                                         "at": 0}
        for number, card in enumerate(app.cards):
            app.index = number
            app.home_scroll.offset = 0
            app._render()
            js.checkFrame(name, card.id, bytes(display.pixels))
