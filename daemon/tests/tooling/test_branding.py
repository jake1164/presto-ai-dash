"""Artwork must survive web packaging and the device's shared app inventory."""

from pathlib import Path
import sys
import unittest

import presto_path

ROOT = Path(presto_path.REPO_ROOT)
sys.path.append(str(ROOT / "tools"))
from build_brand_assets import outputs
from presto_bundle import snapshot
from device import test_ui as ui_tests
from ui import (BACKGROUND, Palette, draw_boot, draw_card, draw_message,
                draw_sources, draw_lifecycle_view)
from brand import COLOURS, NAME, SPRITES, TAGLINE, TAGLINE_LINES
from cards import Card


class BrandAssetTests(unittest.TestCase):
    def test_derived_artwork_matches_the_approved_bundle(self):
        for path, expected in outputs().items():
            with self.subTest(path=path.relative_to(ROOT)):
                self.assertEqual(path.read_bytes(), expected)

    def test_device_art_is_in_both_installation_layouts(self):
        bundle = snapshot()
        artwork = (ROOT / "presto/brand.py").read_bytes()
        self.assertEqual(bundle["files"]["/ai_dash_files/brand.py"], artwork)
        self.assertEqual(bundle["slot"]["files"]["brand.py"], artwork)
        # Metadata cannot advertise a custom icon the stock launcher cannot load.
        for path in ("presto/launcher/ai_dash.py", "presto/bootstrap/ai_dash_boot.py"):
            lines = (ROOT / path).read_text(encoding="utf-8").splitlines()
            self.assertEqual(lines[:3], ["# ICON monitoring", "# NAME " + NAME,
                                         "# DESC " + TAGLINE])

    def test_sprites_stay_inside_their_declared_size(self):
        for size, colours in SPRITES.items():
            self.assertEqual(len(colours), len(COLOURS))
            for spans in colours:
                self.assertTrue(spans)
                self.assertEqual(len(spans) % 3, 0)
                for offset in range(0, len(spans), 3):
                    x, y, width = spans[offset:offset + 3]
                    self.assertLess(y, size)
                    self.assertGreater(width, 0)
                    self.assertLessEqual(x + width, size)

    def test_startup_and_error_text_fit_without_hiding_recovery(self):
        for draw in (
            lambda d, p: draw_boot(d, p),
            lambda d, p: draw_message(d, p, "No network", "Check Wi-Fi settings",
                detail=["Retrying...", "FAILS 0 UP 0H00M", "FREE 7477K WIFI DOWN ST 1",
                        "RESET returns to carousel"], demo=True),
        ):
            display = ui_tests.Display()
            draw(display, Palette(display))
            ui_tests.TextLayoutTests().assert_rows_fit(display)
            labels = [row[0] for row in display.rows]
            self.assertIn(NAME, labels)
            for text in labels:
                self.assertTrue(text.isascii())
            if "Starting..." in labels:
                for line in TAGLINE_LINES:
                    self.assertIn(line, labels)
            else:
                self.assertIn("RESET returns to carousel", labels)
                self.assertIn(TAGLINE, labels)

    def test_home_and_service_card_footer_have_no_product_logo(self):
        display = ui_tests.Framebuffer()
        palette = Palette(display)
        background = display.create_pen(*BACKGROUND)
        for draw in (
            lambda: draw_sources(display, palette, [], 0, 7),
            lambda: draw_card(display, palette, Card("service", "Service", "ok", "12%"),
                              1, 7, muted=True, stale=True, demo=True),
        ):
            draw()
            for y in range(220, 236):
                for x in range(8, 24):
                    offset = (y * 240 + x) * 3
                    self.assertEqual(bytes(display.pixels[offset:offset + 3]), background)

    def test_status_screens_share_the_large_centered_header(self):
        reference = None
        for draw in (
            lambda d, p: draw_boot(d, p),
            lambda d, p: draw_message(d, p, "Connecting...", "Joining Wi-Fi"),
            lambda d, p: draw_message(d, p, "Daemon offline", "Retrying..."),
            lambda d, p: draw_lifecycle_view(d, p, {
                "warning": True, "lines": ["Line %d" % n for n in range(8)]}, demo=True),
        ):
            display = ui_tests.Framebuffer()
            draw(display, Palette(display))
            # Compare the actual centered 64px header pixels, outside the
            # status-colour side rail and the screen-specific text.
            pixels = b"".join(bytes(display.pixels[(y * 240 + 88) * 3:
                                                     (y * 240 + 152) * 3])
                               for y in range(4, 68))
            if reference is None:
                reference = pixels
            self.assertEqual(pixels, reference)


if __name__ == "__main__":
    unittest.main()
