"""The Presto layout preview must keep rendering the real device screens."""

import json
import os
import struct
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import presto_path

PREVIEW_DIR = os.path.join(presto_path.REPO_ROOT, "tools", "presto_preview")
if PREVIEW_DIR not in sys.path:
    sys.path.append(PREVIEW_DIR)

from framebuffer import Framebuffer  # noqa: E402
import live  # noqa: E402
import net  # noqa: E402
import render  # noqa: E402
from presto_bundle import app_files, snapshot  # noqa: E402


class PreviewTests(unittest.TestCase):
    def test_source_loader_uses_snapshot_bytes_even_if_the_file_changes(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "helper.py")
            with open(path, "wb") as handle:
                handle.write(b"value = 'old'\n")
            loader = render._AppSourceLoader("helper", path, b"value = 'new'\n")
            namespace = {}
            exec(loader.get_code("helper"), namespace)
            self.assertEqual(namespace["value"], "new")

    def test_preview_entry_is_the_exact_source_used_for_device_deployment(self):
        with patch.dict(sys.modules, {"main": SimpleNamespace(App="unrelated module")}):
            app, _ = render._load_app_modules()
        deployed_entry = next(path for path, target in app_files().items()
                              if target == "/ai_dash_files/ai_dash_app.py")
        self.assertEqual(os.path.realpath(app.__file__), os.path.realpath(deployed_entry))
        self.assertTrue(callable(app.App))

    def test_measure_text_matches_firmware_fonts(self):
        display = Framebuffer()
        display.set_font("bitmap8")
        # Firmware widths: H=4, o=4, m=5, e=4, spacing 1 between glyphs.
        self.assertEqual(display.measure_text("Home", 2), (4 + 4 + 5 + 4 + 3) * 2)
        self.assertEqual(display.measure_text("", 2), 0)
        with self.assertRaises(ValueError):
            display.set_font("sans")

    def test_text_respects_clip(self):
        display = Framebuffer()
        display.set_pen(display.create_pen(255, 255, 255))
        display.set_clip(0, 0, 10, 10)
        display.text("WWWWWWWW", 0, 0, 240, 3)
        lit = [i // 3 for i in range(0, len(display.pixels), 3) if display.pixels[i]]
        self.assertTrue(lit)
        self.assertTrue(all(p % 240 < 10 and p // 240 < 10 for p in lit))

    def test_demo_payload_renders_every_card_at_panel_size(self):
        with tempfile.TemporaryDirectory() as out:
            screens = render.render("demo", out)
            # Six accounts and Home, plus Home scrolled to the end of its list.
            self.assertEqual(len(screens), 8)
            for screen in screens:
                with open(os.path.join(out, screen["file"]), "rb") as handle:
                    head = handle.read(24)
                self.assertEqual(head[:8], b"\x89PNG\r\n\x1a\n")
                self.assertEqual(struct.unpack(">II", head[16:24]), (480, 480))
            self.assertTrue(os.path.exists(os.path.join(out, "index.html")))

    def test_offline_renders_one_connection_screen(self):
        with tempfile.TemporaryDirectory() as out:
            self.assertEqual(len(render.render("demo", out, offline=True)), 1)

    def test_a_scene_can_be_chosen_and_is_recorded(self):
        import hashlib
        from demo_client import DEFAULT_SCENE, SCENES

        self.assertIn("rate_limited", SCENES)
        with tempfile.TemporaryDirectory() as calm_out:
            calm = render.render("demo", calm_out)
            with open(os.path.join(calm_out, "manifest.json")) as handle:
                self.assertEqual(json.load(handle)["state"]["scene"], DEFAULT_SCENE)
            with open(os.path.join(calm_out, calm[0]["file"]), "rb") as handle:
                calm_home = hashlib.sha256(handle.read()).hexdigest()
        with tempfile.TemporaryDirectory() as busy_out:
            busy = render.render("demo", busy_out, scene="rate_limited")
            with open(os.path.join(busy_out, "manifest.json")) as handle:
                self.assertEqual(json.load(handle)["state"]["scene"], "rate_limited")
            with open(os.path.join(busy_out, busy[0]["file"]), "rb") as handle:
                busy_home = hashlib.sha256(handle.read()).hexdigest()
        self.assertNotEqual(calm_home, busy_home, "two scenes rendered the same Home")

    def test_an_unknown_scene_is_refused(self):
        with tempfile.TemporaryDirectory() as out:
            with self.assertRaises(SystemExit):
                render.render("demo", out, scene="no-such-scene")

    def test_the_demo_cards_carry_their_tiles_and_lines(self):
        """The harness fills the lazy detail, so a card shows its tiles."""
        from cards import Card
        from demo_client import scene_offset

        class FakeApp:
            def __init__(self):
                self.cards = [Card("dash", "AI Dash", "ok"),
                              Card("demo-claude", "Claude", "ok")]
                self.card_details = {}

        app = FakeApp()
        render._fill_card_details(app, scene_offset("calm"))
        # Home's card gets lines too; only the account cards draw tiles.
        self.assertTrue(set(app.card_details) <= {"dash", "demo-claude"})
        self.assertTrue(app.card_details["demo-claude"]["tiles"])
        self.assertTrue(app.card_details["demo-claude"]["lines"])


class FramebufferImportTests(unittest.TestCase):
    """The browser demo imports framebuffer.py under MicroPython WebAssembly."""

    def test_it_needs_no_stdlib_modules_the_wasm_build_lacks(self):
        with open(os.path.join(PREVIEW_DIR, "framebuffer.py"), encoding="utf-8") as handle:
            source = handle.read()
        self.assertNotIn("import struct", source)
        self.assertNotIn("import zlib", source)
        self.assertFalse(hasattr(Framebuffer, "png"))

    def test_png_encoding_lives_in_the_cpython_only_module(self):
        import png as png_module

        data = png_module.png(Framebuffer())
        self.assertEqual(data[:8], b"\x89PNG\r\n\x1a\n")
        self.assertEqual(struct.unpack(">II", data[16:24]), (480, 480))


class DemoClientTests(unittest.TestCase):
    """The preview's demo client serves what the daemon's demo serves."""

    def client(self, second=0):
        from demo_client import DemoClient
        # A frozen clock, so the loop second is exactly the offset.
        return DemoClient(clock=lambda: 0.0, offset=second)

    def test_every_step_is_inside_the_device_budget(self):
        from presto_ai_dash import demo as daemon_demo
        from device.test_device_payload import BUDGET_BYTES, BUDGET_PER_SOURCE
        import asyncio
        import json

        for second in range(0, daemon_demo.LOOP_SECONDS, daemon_demo.STEP_SECONDS):
            payload = asyncio.run(self.client(second).fetch_async())
            self.assertIs(payload["demo"], True)
            size = len(json.dumps(payload, separators=(",", ":")))
            self.assertLessEqual(size, BUDGET_BYTES, "at %ds" % second)
            self.assertLessEqual(size / len(payload["sources"]), BUDGET_PER_SOURCE)
            for card in payload["cards"]:
                detail = asyncio.run(self.client(second).fetch_card(card["id"]))
                self.assertIsInstance(detail, dict, card["id"])
                self.assertLessEqual(len(json.dumps(detail, separators=(",", ":"))),
                                     net.CARD_RESPONSE_LIMIT)

    def test_a_bell_tap_is_remembered_in_memory_only(self):
        import asyncio

        client = self.client()
        response = asyncio.run(client.post_alert("demo-claude", False))
        self.assertEqual(response, {"ok": True, "alerts": False, "master": True})
        self.assertEqual(client.alerts, {"demo-claude": False})
        payload = asyncio.run(client.fetch_async())
        claude = next(source for source in payload["sources"]
                      if source["id"] == "demo-claude")
        self.assertIs(claude["alerts"], False)

    def test_it_never_raises_except_when_the_failure_is_simulated(self):
        import asyncio

        client = self.client()
        asyncio.run(client.fetch_async())
        asyncio.run(client.fetch_card("demo-claude"))
        asyncio.run(client.post_alert("demo-claude", True))
        client.simulate_failure = True
        for call in (client.fetch_async(), client.fetch_card("demo-claude"),
                     client.post_alert("demo-claude", True)):
            with self.assertRaises(net.NetworkUnavailable):
                asyncio.run(call)
        client.simulate_failure = False
        self.assertIs(asyncio.run(client.fetch_async())["demo"], True)

    def test_a_bell_tap_recalculates_the_demo_home(self):
        import asyncio

        client = self.client(second=54)
        asyncio.run(client.post_alert("demo-claude", False))
        payload = asyncio.run(client.fetch_async())
        self.assertEqual(payload["overall_status"], "critical")
        asyncio.run(client.post_alert("demo-ollama-cloud", False))
        payload = asyncio.run(client.fetch_async())
        self.assertEqual(payload["overall_status"], "ok")
        self.assertEqual(payload["cards"][0]["status"], "ok")
        self.assertEqual(payload["cards"][0]["accent"], "green")


class LivePreviewTests(unittest.TestCase):
    def test_offline_screen_is_the_devices_connection_error(self):
        png = live.offline_png()
        self.assertEqual(png[1:4], b"PNG")
        self.assertEqual(struct.unpack(">II", png[16:24]), (240, 240))
        self.assertGreater(len(png), 400)  # text and accent bar, not a blank frame

    def test_demo_app_runs_and_responds_to_a_swipe(self):
        runner = live.Runner(demo=True)
        self.addCleanup(runner.stop)
        runner.restart()
        state = runner.snapshot(-1, timeout=5)
        while state["frame"] < 2 and state["error"] is None:
            state = runner.snapshot(state["frame"], timeout=5)
        self.assertIsNone(state["error"])
        self.assertEqual(state["code_revision"], snapshot()["revision"])
        self.assertIn("status", state["device_sync"])
        home = runner.pixels
        runner.presto.touch.feed([("down", 200, 130), ("move", 120, 130), ("up", 40, 130)])
        # The app samples one touch event per loop pass, so the swipe lands a
        # few frames later: wait for the screen to change, with a deadline.
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and runner.pixels == home:
            state = runner.snapshot(state["frame"], timeout=5)
            self.assertIsNone(state["error"])
        self.assertNotEqual(runner.pixels, home, "a swipe never changed the screen")

    def test_a_scene_jump_restarts_the_demo_at_that_scene(self):
        from demo_client import SCENES

        runner = live.Runner(demo=True)
        self.addCleanup(runner.stop)
        runner.restart()
        state = runner.snapshot(-1, timeout=5)
        self.assertEqual(state["scene"], SCENES[0])
        self.assertEqual(state["scenes"], list(SCENES))
        before = runner.pixels
        runner.set_scene("offline")
        state = runner.snapshot(state["frame"], timeout=10)
        while state["frame"] < 2 and state["error"] is None:
            state = runner.snapshot(state["frame"], timeout=5)
        self.assertEqual(state["scene"], "offline")
        self.assertIsNone(state["error"])
        self.assertNotEqual(runner.pixels, before, "the scene did not change the screen")

    def test_a_scene_name_the_demo_does_not_have_is_refused(self):
        from demo_client import scene_offset
        import demo_client

        with self.assertRaises(KeyError):
            scene_offset("no-such-scene")
        self.assertNotIn("no-such-scene", demo_client.SCENES)


class FakeRunner:
    """Enough of the live runner for the handler: it records what it is told."""

    def __init__(self):
        import threading
        self.cond = threading.Condition()
        self.pixels = bytes(240 * 240 * 2)
        self.led = (0, 0, 0)
        self.error = None
        self.network_fail = False
        self.demo = True
        self.restarts = 0
        self.fed = []
        runner = self

        class Touch:
            def feed(self, events):
                runner.fed.extend(events)

        class Presto:
            touch = Touch()

        self.presto = Presto()

    def publish(self, *args):
        pass

    def restart(self):
        self.restarts += 1

    def snapshot(self, after, timeout=0):
        return {"frame": 1, "led": self.led, "error": None, "demo": self.demo}


class PageTemplateTests(unittest.TestCase):
    """The preview page is one well-formed document with one of each control."""

    def test_the_page_parses_and_has_one_of_each_control(self):
        from html.parser import HTMLParser

        class Collector(HTMLParser):
            def __init__(self):
                HTMLParser.__init__(self)
                self.tags = []
                self.ids = []
                self.closed = []

            def handle_starttag(self, tag, attrs):
                attrs = dict(attrs)
                self.tags.append(tag)
                if attrs.get("id"):
                    self.ids.append(attrs["id"])

            def handle_endtag(self, tag):
                self.closed.append(tag)

        page = Collector()
        page.feed(live.PAGE)
        page.close()
        for tag in ("html", "head", "body", "style", "script"):
            self.assertEqual(page.tags.count(tag), 1, "<%s> count" % tag)
            self.assertEqual(page.closed.count(tag), 1, "</%s> count" % tag)
        for control in ("demo", "scene", "netfail", "restart"):
            self.assertEqual(page.ids.count(control), 1, 'id="%s" count' % control)
        self.assertNotIn("STATIC_DEMO", live.PAGE)
        # The checkbox says Demo, and the scene menu is a real control.
        self.assertIn("> Demo</label>", live.PAGE)
        self.assertIn('id="scene-label"', live.PAGE)


class LiveServerGuardTests(unittest.TestCase):
    """A web page the developer visits must not be able to drive the preview."""

    def setUp(self):
        import http.client
        import threading
        from http.server import ThreadingHTTPServer
        self.runner = FakeRunner()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), live.make_handler(self.runner))
        self.port = self.server.server_address[1]
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.http = http.client

    def call(self, method, path, body=None, headers=None, host=None):
        connection = self.http.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            sent = {"Host": host or "127.0.0.1:%d" % self.port}
            sent.update(headers or {})
            connection.request(method, path, body=body, headers=sent)
            reply = connection.getresponse()
            return reply.status, reply.read()
        finally:
            connection.close()

    TOUCH = b'[["down", 226, 14], ["up", 226, 14]]'
    JSON = {"Content-Type": "application/json"}

    def test_the_page_and_its_own_requests_still_work(self):
        self.assertEqual(self.call("GET", "/")[0], 200)
        self.assertEqual(self.call("GET", "/state?after=-1")[0], 200)
        self.assertEqual(self.call("GET", "/frame.png")[0], 200)
        origin = {"Origin": "http://127.0.0.1:%d" % self.port}
        status, _ = self.call("POST", "/touch", self.TOUCH, dict(self.JSON, **origin))
        self.assertEqual(status, 200)
        self.assertEqual(len(self.runner.fed), 2)
        localhost = self.call("GET", "/", host="localhost:%d" % self.port)
        self.assertEqual(localhost[0], 200)

    def test_another_host_name_is_refused_so_rebinding_reads_nothing(self):
        for path in ("/", "/state?after=-1", "/frame.png"):
            with self.subTest(path=path):
                status, _ = self.call("GET", path, host="rebind.evil.example")
                self.assertEqual(status, 403)
        self.assertEqual(self.call("GET", "/", host="127.0.0.1:1")[0], 403)
        self.assertEqual(self.call("GET", "/", host="127.0.0.1")[0], 403)

    def test_a_text_plain_post_is_refused_and_does_nothing(self):
        for headers in ({"Content-Type": "text/plain"}, {},
                        {"Content-Type": "application/x-www-form-urlencoded"}):
            with self.subTest(headers=headers):
                self.assertEqual(self.call("POST", "/touch", self.TOUCH, headers)[0], 403)
        self.assertEqual(self.runner.fed, [])

    def test_a_foreign_origin_is_refused_even_with_json(self):
        headers = dict(self.JSON, Origin="http://evil.example")
        self.assertEqual(self.call("POST", "/touch", self.TOUCH, headers)[0], 403)
        self.assertEqual(self.call("POST", "/control", b'{"restart": true}', headers)[0], 403)
        self.assertEqual(self.call("GET", "/state?after=-1", None,
                                   {"Origin": "http://evil.example"})[0], 403)
        self.assertEqual(self.runner.fed, [])
        self.assertEqual(self.runner.restarts, 0)

    def test_json_with_a_charset_is_accepted(self):
        headers = {"Content-Type": "application/json; charset=utf-8"}
        self.assertEqual(self.call("POST", "/control", b'{"restart": true}', headers)[0], 200)
        self.assertEqual(self.runner.restarts, 1)

    def test_the_decision_function(self):
        port = 8790
        refusal = live.request_refusal
        self.assertIsNone(refusal("127.0.0.1:8790", None, None, port, "GET"))
        self.assertIsNone(refusal("LOCALHOST:8790", "http://localhost:8790", "application/json",
                                  port, "POST"))
        self.assertIsNotNone(refusal(None, None, None, port, "GET"))
        self.assertIsNotNone(refusal("127.0.0.1:8790", "null", None, port, "GET"))
        self.assertIsNotNone(refusal("127.0.0.1:8790", "https://127.0.0.1:8790", None, port, "GET"))
        self.assertIsNotNone(refusal("127.0.0.1:8790", None, "text/plain", port, "POST"))

    def test_a_container_names_the_hosts_it_answers(self):
        names = ("localhost:18790", "127.0.0.1:18790")
        refusal = live.request_refusal
        self.assertIsNone(refusal("127.0.0.1:18790", "http://localhost:18790", None, 8790, "GET", names))
        # The listening port is not the published one: only the named hosts pass.
        self.assertIsNotNone(refusal("127.0.0.1:8790", None, None, 8790, "GET", names))
        self.assertIsNotNone(refusal("evil.example:18790", None, None, 8790, "GET", names))
        self.assertIsNotNone(refusal("127.0.0.1:18790", "http://evil.example", None, 8790, "GET", names))


if __name__ == "__main__":
    unittest.main()
