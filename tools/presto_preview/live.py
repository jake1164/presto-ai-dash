"""Run the Presto app live in a browser window.

    python tools/presto_preview/live.py            # polls the daemon in presto/config.py
    python tools/presto_preview/live.py --demo     # the scripted demo, no daemon
    python tools/presto_preview/live.py --open     # also open the default browser
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
from framebuffer import Framebuffer  # noqa: E402
from png import encode_png  # noqa: E402
from presto_bundle import sync_status  # noqa: E402

WATCHED = (render.PRESTO_DIR, HERE)


class Stop(BaseException):
    """Raised inside the app loop (from touch.poll) to end it for a restart.

    A BaseException, like KeyboardInterrupt: the app's loop guards catch
    ``Exception`` so that a bad frame never ends the app, and this must not be
    mistaken for one.
    """


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


def daemon_settings(config_path):
    """The daemon address and token to poll, taken from a daemon configuration.

    Returns ``(settings, note)``. ``settings`` uses the app's own setting names,
    or is None when the file cannot be used; ``note`` says what the person needs
    to know, or is None. Neither ever holds a token that was not already in the
    file, and ``note`` never repeats it.
    """
    from presto_ai_dash.config import ConfigError, load_config
    try:
        daemon = load_config(config_path)["daemon"]
    except ConfigError as exc:
        return None, ("cannot use %s (%s); it keeps the daemon address from presto/config.py"
                      % (os.path.basename(config_path), exc))
    host = (daemon.get("host") or "").strip()
    if host in ("", "0.0.0.0", "::", "[::]"):
        # A bind-everything address is not one to connect to; the preview runs
        # on the same computer as the daemon it follows.
        host = "127.0.0.1"
    settings = {"DAEMON_HOST": host, "DAEMON_PORT": int(daemon["port"])}
    token = daemon.get("shared_token") or ""
    if token:
        settings["SHARED_TOKEN"] = token
        return settings, None
    return settings, ("daemon.shared_token is not set, so the daemon invents a new token at "
                      "every start and the preview cannot know it. Set daemon.shared_token in "
                      "the daemon configuration; until then it keeps the token from "
                      "presto/config.py")


class Runner(object):
    """Owns one App thread; restarts it on demand with freshly imported code."""

    def __init__(self, demo=False, scene=None, daemon=None, source=None):
        self.demo = demo
        # Settings that replace the app's own daemon address and token, and the
        # name of the file they came from, for the line under the screen.
        self.daemon = dict(daemon or {})
        self.daemon_source = source or "daemon configuration"
        self.daemon_label = None
        from demo_client import DEFAULT_SCENE
        self.scene = scene or DEFAULT_SCENE
        self.network_fail = False
        # A display fixture for the shared bottom-right indicator. "none" is
        # the quiet case: the daemon sends nothing and no icon is drawn.
        self.lifecycle = "none"
        self.cond = threading.Condition()
        self.frame = 0
        self.pixels = bytes(240 * 240 * 3)
        self.led = [0, 0, 0]
        self.error = None
        self.presto = None
        self._stop = None
        self._thread = None
        self._restart_lock = threading.Lock()
        self.code_revision = None

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
            from demo_client import SCENES
            from lifecycle_fixtures import STATES
            return {"frame": self.frame, "led": self.led, "error": self.error,
                    "demo": self.demo, "network_fail": self.network_fail,
                    "scene": self.scene, "scenes": list(SCENES),
                    "lifecycle": self.lifecycle, "lifecycles": list(STATES),
                    "code_revision": self.code_revision,
                    "device_sync": sync_status(self.code_revision),
                    "daemon": self.daemon_label}

    def _label(self, config):
        """Where the app is polling: the address and which file named it. No token."""
        host = getattr(config, "DAEMON_HOST", None)
        if not host:
            return None
        port = getattr(config, "DAEMON_PORT", 8787)
        try:
            port = int(port)
        except (TypeError, ValueError):
            pass
        return {"address": "%s:%s" % (host, port),
                "source": self.daemon_source if self.daemon else "presto/config.py"}

    def set_scene(self, name):
        """Jump the demo loop to a scene. Restarts, so the new offset applies."""
        self.scene = name
        if self.demo:
            self.restart()

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
            for name, value in self.daemon.items():
                setattr(main.config, name, value)
            self.daemon_label = None if self.demo else self._label(main.config)
            self.code_revision = main.__presto_revision__
            from ui import Palette, draw_boot
            display = Framebuffer()
            presto = LivePresto(display, stop, self.publish)
            self.presto = presto
            draw_boot(display, Palette(display), "Starting...")
            presto.update()
            app = main.App(presto)
            if self.demo:
                # The demo never contacts the daemon: its client answers from
                # the daemon's own demo module, in this process, starting at
                # the chosen scene.
                from demo_client import DemoClient, scene_offset
                app.client = DemoClient(offset=scene_offset(self.scene))
            fetch = app.client.fetch_async

            async def fetch_async():
                if self.network_fail:
                    await asyncio.sleep(0.2)
                    raise net.NetworkUnavailable("simulated by preview")
                payload = await fetch()
                if self.lifecycle != "none":
                    # A fixture, not a device record: it only adds the two
                    # extension objects the real app already parses.
                    from lifecycle_fixtures import apply
                    payload = apply(payload, self.lifecycle)
                return payload

            app.client.fetch_async = fetch_async
            app.run()
        except Stop:
            pass
        except BaseException:
            self.publish(None, None, traceback.format_exc())


def offline_png():
    """The Presto's connection-error screen, drawn by the device's own code.

    Mirrors App._render for an unreachable daemon, so the page can show it when
    this server is gone and nothing can be rendered.
    """
    render._load_app_modules(real_sleep=True)  # puts presto/ on sys.path
    from alerts import rgb_for_accent
    from ui import Palette, draw_message
    display = Framebuffer()
    draw_message(display, Palette(display), "Daemon offline", "Check daemon and LAN address",
                 accent_rgb=rgb_for_accent("red"),
                 detail=["Retrying...", "RESET returns to carousel"])
    return encode_png(display.pixels, 240, 240, 1)


def watch(runner):
    def mtimes():
        stamps = {}
        for folder in WATCHED:
            for name in os.listdir(folder):
                if name.endswith(".py"):
                    try:
                        path = os.path.join(folder, name)
                        stat = os.stat(path)
                        stamps[path] = (stat.st_mtime_ns, stat.st_size)
                    except OSError:
                        pass
        return stamps

    seen = mtimes()
    receipt_seen = sync_status(runner.code_revision)
    while True:
        time.sleep(0.5)
        current = mtimes()
        if current != seen:
            seen = current
            runner.restart()
        receipt = sync_status(runner.code_revision)
        if receipt != receipt_seen:
            receipt_seen = receipt
            runner.publish(runner.pixels, runner.led, runner.error)


def request_refusal(host, origin, content_type, port, method, names=None):
    """Why a request must be refused, or ``None`` when it may proceed.

    The preview holds the real device token and drives the real daemon, so a
    web page the developer happens to visit must not be able to drive it:
    only the preview's own address is answered (which defeats DNS rebinding,
    where a hostile name resolves to 127.0.0.1), a browser's cross-site
    request is turned away by its Origin, and a write must be JSON, which a
    cross-site form or ``text/plain`` request cannot be.
    """
    names = tuple(names) if names else ("127.0.0.1:%d" % port, "localhost:%d" % port)
    if (host or "").lower() not in names:
        return "unexpected Host"
    if origin is not None and origin not in tuple("http://" + name for name in names):
        return "foreign Origin"
    if method == "POST" and (content_type or "").split(";")[0].strip().lower() != "application/json":
        return "a POST must be application/json"
    return None


def make_handler(runner, names=None):
    class Handler(BaseHTTPRequestHandler):
        def _refused(self):
            reason = request_refusal(self.headers.get("Host"), self.headers.get("Origin"),
                                     self.headers.get("Content-Type"),
                                     self.server.server_address[1], self.command, names)
            if reason:
                self._send(403, reason.encode("ascii"), "text/plain")
            return bool(reason)

        def do_GET(self):
            if self._refused():
                return
            parts = urlsplit(self.path)
            query = parse_qs(parts.query)
            if parts.path == "/":
                return self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
            if parts.path == "/favicon.svg":
                with open(os.path.join(HERE, "favicon.svg"), "rb") as handle:
                    return self._send(200, handle.read(), "image/svg+xml")
            if parts.path == "/frame.png":
                scale = 2 if query.get("scale") == ["2"] else 1
                with runner.cond:
                    pixels = runner.pixels
                return self._send(200, encode_png(pixels, 240, 240, scale), "image/png")
            if parts.path == "/offline.png":
                return self._send(200, offline_png(), "image/png")
            if parts.path == "/state":
                after = int(query.get("after", ["-1"])[0])
                return self._json(runner.snapshot(after))
            self._send(404, b"not found", "text/plain")

        def do_POST(self):
            if self._refused():
                return
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
                if "scene" in body:
                    from demo_client import SCENES
                    name = str(body["scene"])
                    if name in SCENES:
                        runner.set_scene(name)
                if "lifecycle" in body:
                    from lifecycle_fixtures import STATES
                    name = str(body["lifecycle"])
                    if name in STATES and name != runner.lifecycle:
                        runner.lifecycle = name
                        # Restart so the next poll carries the fixture at once.
                        runner.restart()
                if body.get("restart"):
                    runner.restart()
                return self._json({"ok": True})
            self._send(404, b"not found", "text/plain")

        def _json(self, data):
            self._send(200, json.dumps(data).encode(), "application/json")

        def _send(self, status, body, content_type):
            try:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
            except (ConnectionError, TimeoutError):
                # The browser closed or refreshed mid-response; nothing to do.
                pass

        def log_message(self, *args):
            pass

    return Handler


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Presto Live</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="icon" type="image/svg+xml" href="/favicon.svg">
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
label, button { cursor: help; }
button { font: inherit; background: #21262d; color: #c9d1d9; border: 1px solid #30363d;
  border-radius: 6px; padding: 6px 12px; cursor: pointer; }
.hint, #status { color: #8b949e; font-size: 12px; margin-top: 10px; text-align: center; }
</style></head><body>
<div id="bezel"><img id="screen" alt="Presto screen" draggable="false" src="/frame.png"><pre id="error"></pre></div>
<div class="controls">
  <label id="demo-label" title="Runs the app on the scripted demo instead of the daemon: it never contacts anything. The same sample accounts the daemon's demo mode and the published demo page show, with the device's own screens. Untick to go back to live data."><input type="checkbox" id="demo"> Demo</label>
  <label id="scene-label" title="Jumps the demo to one of its scenes. The loop keeps playing from there, so you can watch a scene progress instead of waiting for it.">Scene
    <select id="scene"></select></label>
  <label id="lifecycle-label" title="Adds one of the agreed indicator states to the status payload as a display fixture: the same objects the daemon sends, parsed by the real app. Nothing is written and no device state is claimed.">Notice
    <select id="lifecycle"></select></label>
  <label id="netfail-label" title="Makes every fetch fail, as if the daemon were off or unreachable. The app keeps its last good data marked 'STALE - retrying', then after 30 seconds switches to the red 'Daemon offline' screen, exactly as the device does. Untick to watch it recover. Your real daemon is not affected."><input type="checkbox" id="netfail"> Simulate network failure</label>
  <button id="restart" type="button" title="Restarts the app from boot, back on Home, with the current presto/*.py code. Saving a presto file already does this automatically, so use it to reset the app's state, such as scroll position, mute, or a stuck screen.">Restart app</button>
</div>
<div class="hint">Drag to swipe or scroll &middot; click to tap &middot; hold 0.7s to mute.
  Keys: &larr; &rarr; cards, &uarr; &darr; scroll, Enter tap, M mute.</div>
<div id="status">connecting&hellip;</div>
<div id="code-status" class="hint" role="status">Shared app code: checking&hellip;</div>
<script>
(() => {
  const screen = document.getElementById("screen");
  const errorBox = document.getElementById("error");
  const status = document.getElementById("status");
  let frame = -1, sending = Promise.resolve(), down = false, offlineUrl = null;
  // Keep the device's offline screen in the page: once the server is gone nothing can render it.
  async function cacheOffline() {
    try {
      offlineUrl = URL.createObjectURL(await (await fetch("/offline.png")).blob());
    } catch (e) {}
  }

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
    lifecycle: (name) => post("/control", {lifecycle: name}),
  };
  const keys = {ArrowLeft: "left", ArrowRight: "right", ArrowUp: "down", ArrowDown: "up", Enter: "tap", m: "long"};
  document.addEventListener("keydown", (e) => {
    if (keys[e.key] && e.target.tagName !== "INPUT") { e.preventDefault(); presto.gesture(keys[e.key]); }
  });

  document.getElementById("demo").addEventListener("change", (e) => post("/control", {demo: e.target.checked}));
  document.getElementById("scene").addEventListener("change", (e) => post("/control", {scene: e.target.value}));
  document.getElementById("lifecycle").addEventListener("change", (e) => post("/control", {lifecycle: e.target.value}));
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
        if (!offlineUrl) cacheOffline();
        document.getElementById("demo").checked = state.demo;
        const scene = document.getElementById("scene");
        if (state.scenes && scene.options.length !== state.scenes.length) {
          scene.replaceChildren(...state.scenes.map((name) => {
            const option = document.createElement("option");
            option.value = name; option.textContent = name; return option;
          }));
        }
        scene.value = state.scene;
        scene.disabled = !state.demo;
        const notice = document.getElementById("lifecycle");
        if (state.lifecycles && notice.options.length !== state.lifecycles.length) {
          notice.replaceChildren(...state.lifecycles.map((name) => {
            const option = document.createElement("option");
            option.value = name; option.textContent = name; return option;
          }));
        }
        notice.value = state.lifecycle || "none";
        document.getElementById("netfail").checked = state.network_fail;
        const where = state.demo ? "" : (state.daemon
          ? " · polling " + state.daemon.address + " (" + state.daemon.source + ")"
          : " · no daemon address set");
        status.textContent = (state.demo ? "demo (sample data) · " + state.scene
          : "live daemon" + where) + " · frame " + frame +
          (state.network_fail ? " · network failing" : "");
        const sync = state.device_sync || {status: "not_checked"};
        const revision = (state.code_revision || "unknown").slice(0, 12);
        const labels = {not_checked: "Device not checked — run Check Presto sync",
          unverified: "USB sync not verified — run Check Presto sync",
          different: "Device code differs — run Sync Presto",
          source_changed: "App changed since USB verification — run Sync Presto",
          matched: "Device files matched at last USB verification"};
        document.getElementById("code-status").textContent = "Shared app " + revision + " · " +
          labels[sync.status] + (sync.checked_at ? " · " + new Date(sync.checked_at).toLocaleString() : "");
      } catch (e) {
        if (offlineUrl) screen.src = offlineUrl;
        frame = -1;  // redraw from the live frame when the server returns
        document.body.style.setProperty("--led", "rgb(89,14,10)");
        status.textContent = "preview server unreachable; retrying";
        await wait(1000);
      }
    }
  }
  cacheOffline();
  loop();
})();
</script>
</body></html>
"""


def open_tabs(first, preview_url, wait=30):
    """Open other pages, then the preview, so tab order and focus are fixed."""
    try:
        import socket
        import webbrowser
        from urllib.parse import urlsplit

        def show(url):
            # Say what happened: a silent failure here looks like "nothing opens".
            print("opening %s -> %s" % (url, "ok" if webbrowser.open(url) else "NO BROWSER FOUND"),
                  flush=True)

        for url in first:
            parts = urlsplit(url)
            end = time.monotonic() + wait
            while time.monotonic() < end:
                try:
                    socket.create_connection((parts.hostname, parts.port or 80), 1).close()
                    break
                except OSError:
                    time.sleep(0.5)
            else:
                print("%s did not answer within %ds; opening it anyway" % (url, wait), flush=True)
            show(url)
            time.sleep(1)  # let the browser take this tab before the next
        show(preview_url)
    except BaseException:
        traceback.print_exc()


def start_server(runner, host, port, names=None):
    """The preview's HTTP server, bound and ready for ``serve_forever``.

    ``names`` are the Host values it answers (default: loopback on ``port``).
    Raises OSError when the port is taken, with the same exclusive binding as
    the daemon, so no two previews overlap.
    """
    daemon_src = os.path.join(render.REPO_ROOT, "daemon", "src")
    if os.path.isdir(daemon_src) and daemon_src not in sys.path:
        sys.path.append(daemon_src)
    from presto_ai_dash.api import _ExclusiveHTTPServer
    server = _ExclusiveHTTPServer((host, port), make_handler(runner, names))
    server.daemon_threads = True
    return server


def main_cli(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--demo", action="store_true",
                        help="start with Demo on: the scripted sample data, no daemon")
    parser.add_argument("--port", type=int, default=8790)
    parser.add_argument("--config", metavar="PATH",
                        help="a daemon configuration (dash.toml): poll that daemon's address "
                             "and token instead of the ones in presto/config.py")
    parser.add_argument("--open", action="store_true", help="open the page in the default browser")
    parser.add_argument("--open-first", action="append", default=[], metavar="URL",
                        help="with --open, open this URL first, once its port accepts "
                             "connections (repeatable); the preview opens last, in front")
    args = parser.parse_args(argv)
    daemon = None
    if args.config:
        daemon, note = daemon_settings(args.config)
        if daemon:
            print("Presto live preview polls the daemon at %s:%d (from %s)"
                  % (daemon["DAEMON_HOST"], daemon["DAEMON_PORT"],
                     os.path.basename(args.config)), flush=True)
        if note:
            print("Presto live preview: " + note, flush=True)
    runner = Runner(demo=args.demo, daemon=daemon,
                    source=os.path.basename(args.config) if daemon else None)
    runner.restart()
    threading.Thread(target=watch, args=(runner,), daemon=True).start()
    # Loopback only: the live dash shows account names and quotas.
    # Use the same exclusive binding as the daemon: no overlapping PSL processes.
    try:
        server = start_server(runner, "127.0.0.1", args.port)
    except OSError:
        runner.stop()
        raise SystemExit("PSL is already running or its port is unavailable; use the existing preview.")
    url = "http://127.0.0.1:%d/" % args.port
    print("Presto live preview on " + url, flush=True)
    if args.open:
        threading.Thread(target=open_tabs, args=(args.open_first, url), daemon=True).start()
    server.serve_forever()


if __name__ == "__main__":
    main_cli()
