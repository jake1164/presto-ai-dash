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


if __name__ == "__main__":
    unittest.main()
