"""Run the Presto app live in a browser window.

    python tools/presto_preview/live.py            # polls the daemon in presto/config.py
    python tools/presto_preview/live.py --demo     # STATIC_DEMO Home, no network
    open http://127.0.0.1:8790/

The real presto/main.py App loop runs here, with only the hardware faked:
framebuffer.py stands in for PicoGraphics, the browser's pointer is the touch
panel, and Wi-Fi is always "connected" (the daemon fetch itself is real). Each
presto.update() is a new frame. Saving any presto/*.py restarts the app with
fresh code. Not emulated: sound, real LED hardware, memory and CPU speed.
"""

import argparse
import asyncio
import collections
import json
import os
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import render  # noqa: E402
from framebuffer import Framebuffer, encode_png  # noqa: E402

WATCHED = (render.PRESTO_DIR, HERE)


class Stop(Exception):
    """Raised inside the app loop (from touch.poll) to end it for a restart."""


class FakeTouch(object):
    def __init__(self, stop):
        self.stop = stop
        self.queue = collections.deque()
        self.lock = threading.Lock()
        self.state = False
        self.x = self.y = 0

    def feed(self, events):
        with self.lock:
            for kind, x, y in events:
                # The app samples every 20 ms; collapse moves so a drag never lags.
                if kind == "move" and self.queue and self.queue[-1][0] == "move":
                    self.queue.pop()
                self.queue.append((kind, int(x), int(y)))

    def poll(self):
        if self.stop.is_set():
            raise Stop()
        with self.lock:
            if self.queue:
                kind, self.x, self.y = self.queue.popleft()
                self.state = kind != "up"


class FakeWifi(object):
    def isconnected(self):
        return True

    async def connect(self, timeout=10, retries=1):
        return True


class LivePresto(object):
    NUM_LEDS = 7

    def __init__(self, display, stop, publish):
        self.display = display
        self.touch = FakeTouch(stop)
        self.wifi = FakeWifi()
        self.leds = [(0, 0, 0)] * self.NUM_LEDS
        self._publish = publish

    def set_led_rgb(self, index, r, g, b):
        self.leds[index] = (r, g, b)

    def update(self):
        self._publish(bytes(self.display.pixels), list(self.leds[0]))


class Runner(object):
    """Owns one App thread; restarts it on demand with freshly imported code."""

    def __init__(self, demo=False):
        self.demo = demo
        self.network_fail = False
        self.cond = threading.Condition()
        self.frame = 0
        self.pixels = bytes(240 * 240 * 3)
        self.led = [0, 0, 0]
        self.error = None
        self.presto = None
        self._stop = None
        self._thread = None
        self._restart_lock = threading.Lock()

    def publish(self, pixels, led, error=None):
        with self.cond:
            if pixels is not None:
                self.pixels, self.led = pixels, led
            self.error = error
            self.frame += 1
            self.cond.notify_all()

    def snapshot(self, after, timeout=20):
        with self.cond:
            self.cond.wait_for(lambda: self.frame != after, timeout)
            return {"frame": self.frame, "led": self.led, "error": self.error,
                    "demo": self.demo, "network_fail": self.network_fail}

    def stop(self):
        if self._thread:
            self._stop.set()
            self._thread.join(5)

    def restart(self):
        with self._restart_lock:
            self.stop()
            for name in [f[:-3] for f in os.listdir(render.PRESTO_DIR) if f.endswith(".py")]:
                if name != "secrets":
                    sys.modules.pop(name, None)
            self._stop = threading.Event()
            self._thread = threading.Thread(target=self._run, args=(self._stop,), daemon=True)
            self._thread.start()

    def _run(self, stop):
        try:
            main, net = render._load_app_modules(real_sleep=True)
            from ui import Palette, draw_boot
            display = Framebuffer()
            presto = LivePresto(display, stop, self.publish)
            self.presto = presto
            draw_boot(display, Palette(display), "Starting...")
            presto.update()
            app = main.App(presto)
            app.static_demo = self.demo
            fetch = app.client.fetch_async

            async def fetch_async():
                if self.network_fail:
                    await asyncio.sleep(0.2)
                    raise net.NetworkUnavailable("simulated by preview")
                return await fetch()

            app.client.fetch_async = fetch_async
            app.run()
        except Stop:
            pass
        except BaseException:
            self.publish(None, None, traceback.format_exc())


def watch(runner):
    def mtimes():
        stamps = {}
        for folder in WATCHED:
            for name in os.listdir(folder):
                if name.endswith(".py"):
                    try:
                        stamps[name] = os.stat(os.path.join(folder, name)).st_mtime
                    except OSError:
                        pass
        return stamps

    seen = mtimes()
    while True:
        time.sleep(0.5)
        current = mtimes()
        if current != seen:
            seen = current
            runner.restart()


def make_handler(runner):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            parts = urlsplit(self.path)
            query = parse_qs(parts.query)
            if parts.path == "/":
                return self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
            if parts.path == "/frame.png":
                scale = 2 if query.get("scale") == ["2"] else 1
                with runner.cond:
                    pixels = runner.pixels
                return self._send(200, encode_png(pixels, 240, 240, scale), "image/png")
            if parts.path == "/state":
                after = int(query.get("after", ["-1"])[0])
                return self._json(runner.snapshot(after))
            self._send(404, b"not found", "text/plain")

        def do_POST(self):
            try:
                length = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(min(length, 65536)) or b"{}")
            except ValueError:
                return self._send(400, b"bad json", "text/plain")
            path = urlsplit(self.path).path
            if path == "/touch" and isinstance(body, list):
                presto = runner.presto
                if presto:
                    presto.touch.feed([e for e in body if isinstance(e, list) and len(e) == 3 and
                                       e[0] in ("down", "move", "up")])
                return self._json({"ok": True})
            if path == "/control" and isinstance(body, dict):
                if "network_fail" in body:
                    runner.network_fail = bool(body["network_fail"])
                    runner.publish(runner.pixels, runner.led, runner.error)
                if "demo" in body and bool(body["demo"]) != runner.demo:
                    runner.demo = bool(body["demo"])
                    runner.restart()
                if body.get("restart"):
                    runner.restart()
                return self._json({"ok": True})
            self._send(404, b"not found", "text/plain")

        def _json(self, data):
            self._send(200, json.dumps(data).encode(), "application/json")

        def _send(self, status, body, content_type):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    return Handler


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Presto Live</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="icon" href="data:,">
<style>
:root { --led: rgb(0,0,0); }
body { margin: 0; padding: 16px; background: #0b0d10; color: #c9d1d9;
  font: 14px system-ui, sans-serif; display: flex; flex-direction: column; align-items: center; }
#bezel { padding: 22px; background: #1b1d21; border-radius: 26px;
  box-shadow: 0 0 34px 4px var(--led); transition: box-shadow .3s; position: relative; }
#screen { display: block; width: 480px; height: 480px; max-width: calc(100vw - 76px);
  max-height: calc(100vw - 76px); image-rendering: pixelated; touch-action: none;
  cursor: pointer; user-select: none; -webkit-user-drag: none; background: #101216; }
#error { display: none; position: absolute; inset: 22px; overflow: auto; margin: 0; padding: 12px;
  background: rgba(60,0,0,.92); color: #ffd7d7; font: 12px ui-monospace, monospace; white-space: pre-wrap; }
.controls { margin-top: 18px; display: flex; flex-wrap: wrap; gap: 14px; align-items: center; justify-content: center; }
button { font: inherit; background: #21262d; color: #c9d1d9; border: 1px solid #30363d;
  border-radius: 6px; padding: 6px 12px; cursor: pointer; }
.hint, #status { color: #8b949e; font-size: 12px; margin-top: 10px; text-align: center; }
</style></head><body>
<div id="bezel"><img id="screen" alt="Presto screen" draggable="false" src="/frame.png"><pre id="error"></pre></div>
<div class="controls">
  <label><input type="checkbox" id="demo"> Static demo</label>
  <label><input type="checkbox" id="netfail"> Simulate network failure</label>
  <button id="restart" type="button">Restart app</button>
</div>
<div class="hint">Drag to swipe or scroll &middot; click to tap &middot; hold 0.7s to mute.
  Keys: &larr; &rarr; cards, &uarr; &darr; scroll, Enter tap, M mute.</div>
<div id="status">connecting&hellip;</div>
<script>
(() => {
  const screen = document.getElementById("screen");
  const errorBox = document.getElementById("error");
  const status = document.getElementById("status");
  let frame = -1, sending = Promise.resolve(), down = false;

  const post = (path, body) => fetch(path, {method: "POST",
    headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)});
  // Serialise touch posts so samples arrive in order.
  const send = (events) => (sending = sending.then(() => post("/touch", events)).catch(() => {}));

  function point(event) {
    const box = screen.getBoundingClientRect();
    const clamp = (v) => Math.max(0, Math.min(239, Math.round(v)));
    return [clamp((event.clientX - box.left) * 240 / box.width),
            clamp((event.clientY - box.top) * 240 / box.height)];
  }
  screen.addEventListener("pointerdown", (e) => {
    screen.setPointerCapture(e.pointerId); down = true; send([["down", ...point(e)]]);
  });
  screen.addEventListener("pointermove", (e) => { if (down) send([["move", ...point(e)]]); });
  const lift = (e) => { if (down) { down = false; send([["up", ...point(e)]]); } };
  screen.addEventListener("pointerup", lift);
  screen.addEventListener("pointercancel", lift);

  const wait = (ms) => new Promise((r) => setTimeout(r, ms));
  async function stroke(x0, y0, x1, y1) {
    const steps = 6, events = [["down", x0, y0]];
    for (let i = 1; i <= steps; i++)
      events.push(["move", Math.round(x0 + (x1 - x0) * i / steps), Math.round(y0 + (y1 - y0) * i / steps)]);
    events.push(["up", x1, y1]);
    await send(events);
  }
  // For Playwright: await presto.gesture("left") etc.
  window.presto = {
    gesture: async (name) => {
      if (name === "left") return stroke(190, 130, 50, 130);
      if (name === "right") return stroke(50, 130, 190, 130);
      if (name === "up") return stroke(120, 180, 120, 80);    // scroll list down
      if (name === "down") return stroke(120, 80, 120, 180);  // scroll list up
      if (name === "tap") return send([["down", 120, 130], ["up", 120, 130]]);
      if (name === "long") { await send([["down", 120, 130]]); await wait(900); return send([["up", 120, 130]]); }
      throw new Error("unknown gesture " + name);
    },
    state: () => fetch("/state?after=-1").then((r) => r.json()),
  };
  const keys = {ArrowLeft: "left", ArrowRight: "right", ArrowUp: "down", ArrowDown: "up", Enter: "tap", m: "long"};
  document.addEventListener("keydown", (e) => {
    if (keys[e.key] && e.target.tagName !== "INPUT") { e.preventDefault(); presto.gesture(keys[e.key]); }
  });

  document.getElementById("demo").addEventListener("change", (e) => post("/control", {demo: e.target.checked}));
  document.getElementById("netfail").addEventListener("change", (e) => post("/control", {network_fail: e.target.checked}));
  document.getElementById("restart").addEventListener("click", () => post("/control", {restart: true}));

  async function loop() {
    for (;;) {
      try {
        const state = await (await fetch("/state?after=" + frame)).json();
        if (state.frame !== frame) {
          frame = state.frame;
          const next = new Image();
          next.src = "/frame.png?n=" + frame;
          await next.decode().catch(() => {});
          screen.src = next.src;
          screen.dataset.frame = frame;
          document.body.style.setProperty("--led", "rgb(" + state.led.join(",") + ")");
        }
        errorBox.style.display = state.error ? "block" : "none";
        errorBox.textContent = state.error || "";
        document.getElementById("demo").checked = state.demo;
        document.getElementById("netfail").checked = state.network_fail;
        status.textContent = (state.demo ? "static demo" : "live daemon") + " · frame " + frame +
          (state.network_fail ? " · network failing" : "");
      } catch (e) {
        status.textContent = "preview server unreachable; retrying";
        await wait(1000);
      }
    }
  }
  loop();
})();
</script>
</body></html>
"""


def main_cli(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--demo", action="store_true", help="STATIC_DEMO Home; no network")
    parser.add_argument("--port", type=int, default=8790)
    args = parser.parse_args(argv)
    runner = Runner(demo=args.demo)
    runner.restart()
    threading.Thread(target=watch, args=(runner,), daemon=True).start()
    # Loopback only: the live dash shows account names and quotas.
    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(runner))
    server.daemon_threads = True
    print("Presto live preview on http://127.0.0.1:%d/" % args.port)
    server.serve_forever()


if __name__ == "__main__":
    main_cli()
