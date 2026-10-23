"""The Presto layout preview must keep rendering the real device screens."""

import os
import struct
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import presto_path

PREVIEW_DIR = os.path.join(presto_path.REPO_ROOT, "tools", "presto_preview")
if PREVIEW_DIR not in sys.path:
    sys.path.append(PREVIEW_DIR)

from framebuffer import Framebuffer  # noqa: E402
import live  # noqa: E402
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

    def test_sample_payload_renders_every_card_at_panel_size(self):
        with tempfile.TemporaryDirectory() as out:
            screens = render.render("sample", out)
            self.assertEqual(len(screens), 3)
            for screen in screens:
                with open(os.path.join(out, screen["file"]), "rb") as handle:
                    head = handle.read(24)
                self.assertEqual(head[:8], b"\x89PNG\r\n\x1a\n")
                self.assertEqual(struct.unpack(">II", head[16:24]), (480, 480))
            self.assertTrue(os.path.exists(os.path.join(out, "index.html")))

    def test_offline_renders_one_connection_screen(self):
        with tempfile.TemporaryDirectory() as out:
            self.assertEqual(len(render.render("sample", out, offline=True)), 1)


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
        state = runner.snapshot(state["frame"], timeout=5)
        self.assertIsNone(state["error"])
        self.assertNotEqual(runner.pixels, home)


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


if __name__ == "__main__":
    unittest.main()
