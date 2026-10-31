"""The app's side of the lifecycle indicator.

The payload's two extension objects reach the real ``presto/main.py`` App, the
shared dock reserves its space only while a notice exists, a tap on the dock
opens the read-only view before any other control, and a full-screen connection
error takes the view with it.
"""

import importlib.util
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import presto_path
from gestures import TAP
from payloads import sample_payload

DEVICE = "0123456789abcdef" * 2
REVISION = "abcdef123456"

PREVIEW_DIR = os.path.join(presto_path.REPO_ROOT, "tools", "presto_preview")
if PREVIEW_DIR not in sys.path:
    sys.path.append(PREVIEW_DIR)
import lifecycle_fixtures  # noqa: E402


class Display(object):
    """Enough of a PicoGraphics for App to construct; drawing is not asserted."""

    def __init__(self, width=240, height=240):
        self.width, self.height = width, height
        self.font = "bitmap8"
        self.pen = None
        self.clip = None

    def get_bounds(self):
        return self.width, self.height

    def create_pen(self, *rgb):
        return rgb

    def set_pen(self, pen):
        self.pen = pen

    def set_font(self, name):
        self.font = name

    def set_clip(self, *args):
        self.clip = args

    def remove_clip(self):
        self.clip = None

    def measure_text(self, text, scale=1):
        return len(str(text)) * 6 * scale

    def text(self, *args, **kwargs):
        pass

    def rectangle(self, *args):
        pass

    def circle(self, *args):
        pass

    def clear(self):
        pass


def load_app(values):
    """main.py, with a fake ``config`` module that stays in ``sys.modules``.

    ``main.py`` keeps its own reference to the module and ``net.device_setting``
    looks it up in ``sys.modules``, so both must see the same object for the
    whole test, not only while ``main.py`` is executed.
    """
    patcher = patch.dict(sys.modules, {"config": SimpleNamespace(**values)})
    patcher.start()
    spec = importlib.util.spec_from_file_location(
        "lifecycle_app_for_test", os.path.join(presto_path.PRESTO_DIR, "main.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, patcher
class AppBase(unittest.TestCase):
    config = None

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        values = {"DEVICE_ID": DEVICE, "DEVICE_REVISION": REVISION, "DEVICE_NAME": "presto",
                  # The boot counter is a file; keep the test out of /.
                  "BOOT_COUNTER_FILE": os.path.join(self.temp.name, "boot.txt")}
        values.update(self.config or {})
        self.module, patcher = load_app(values)
        self.addCleanup(patcher.stop)
        self.device = SimpleNamespace(
            display=Display(), update=lambda: None, NUM_LEDS=7,
            set_led_rgb=lambda *args: None,
            wifi=SimpleNamespace(isconnected=lambda: True),
            touch=SimpleNamespace(state=False, poll=lambda: None, x=0, y=0))
        self.clock = patch.object(self.module, "_now_ms", return_value=0)
        self.clock.start()
        self.addCleanup(self.clock.stop)
        diff = patch.object(self.module, "_ticks_diff", side_effect=lambda a, b: a - b)
        diff.start()
        self.addCleanup(diff.stop)
        self.app = self.module.App(self.device)
        # The drawing itself is covered elsewhere; a tick here is about state.
        self.app._render = lambda: None

    def payload(self, state=None):
        payload = sample_payload()
        return lifecycle_fixtures.apply(payload, state) if state else payload

    def tap(self, x, y):
        self.app._touch_x, self.app._touch_y = x, y
        self.app._handle_gestures([TAP])


class NoticeTests(AppBase):
    def test_footer_reflows_when_status_chips_appear_and_clear(self):
        self.app._set_payload(self.payload("usb-required"))
        self.app._maybe_load_detail = lambda: None
        for stale, demo, muted, expected_height in (
            (False, False, False, 220),
            (True, False, False, 204),
            (False, True, False, 204),
            (False, False, True, 204),
            (False, False, False, 220),
        ):
            self.app.connection_error = ("Daemon offline", "Retrying", False) if stale else None
            self.app.demo = demo
            self.app.alerts.muted = muted
            self.module.App._render(self.app)
            self.assertEqual(self.app.home_layout.height, expected_height)
            self.assertEqual(self.app.home_layout.dock_top, expected_height - 20)

    def test_a_payload_without_the_extension_shows_nothing(self):
        self.app._set_payload(self.payload())
        self.assertIsNone(self.app.lifecycle_notice)

    def test_a_current_device_stays_quiet(self):
        self.app._set_payload(self.payload("current"))
        self.assertIsNone(self.app.lifecycle_notice)

    def test_a_release_that_is_current_stale_or_failed_draws_no_arrow(self):
        # A stale or failed check still names a newer version, kept from the
        # last good result. It must not be shown as a fresh announcement.
        for state in ("release-current", "release-stale", "release-error"):
            with self.subTest(state=state):
                self.app._set_payload(self.payload(state))
                self.assertIsNone(self.app.lifecycle_notice)
                self.assertIsNone(self.app.home_layout.dock_top)

    def test_a_fresh_release_draws_the_blue_arrow_and_a_warning_outranks_it(self):
        self.app._set_payload(self.payload("release"))
        self.assertEqual(self.module.lifecycle.kind(self.app.lifecycle_notice), "release")
        self.app._set_payload(self.payload("both"))
        self.assertEqual(self.module.lifecycle.kind(self.app.lifecycle_notice), "warning")
        text = " ".join(self.app.lifecycle_notice["lines"])
        self.assertIn("0.50.0 -> 0.51.0", text)

    def test_a_mismatch_reserves_the_dock(self):
        self.app._set_payload(self.payload("usb-required"))
        self.assertIsNotNone(self.app.lifecycle_notice)
        self.assertEqual(self.app.home_layout.dock_top, 200)
        # The overflow column stops at the dock's left edge less the 4px gap.
        self.assertEqual(self.app.home_layout.strip_limit(), 204)

    def test_no_notice_releases_the_dock(self):
        self.app._set_payload(self.payload("usb-required"))
        self.app._set_payload(self.payload())
        self.assertIsNone(self.app.home_layout.dock_top)

    def test_a_notice_that_clears_closes_the_view(self):
        self.app._set_payload(self.payload("usb-required"))
        self.app.lifecycle_open = True
        self.app._set_payload(self.payload())
        self.assertFalse(self.app.lifecycle_open)


class DockTapTests(AppBase):
    def test_a_tap_on_the_dock_opens_the_view(self):
        self.app._set_payload(self.payload("usb-required"))
        self.tap(220, 212)
        self.assertTrue(self.app.lifecycle_open)

    def test_a_tap_outside_the_dock_does_not(self):
        self.app._set_payload(self.payload("usb-required"))
        self.tap(120, 130)
        self.assertFalse(self.app.lifecycle_open)

    def test_the_dock_is_not_interactive_without_a_notice(self):
        self.app._set_payload(self.payload())
        self.tap(220, 212)
        self.assertFalse(self.app.lifecycle_open)

    def test_a_second_tap_closes_it(self):
        self.app._set_payload(self.payload("both"))
        self.tap(220, 212)
        self.assertTrue(self.app.lifecycle_open)
        self.tap(120, 120)
        self.assertFalse(self.app.lifecycle_open)

    def test_opening_the_view_changes_no_card_or_scroll_state(self):
        self.app._set_payload(self.payload("usb-required"))
        index, offset, detail = self.app.index, self.app.home_scroll.offset, self.app.detail_open
        self.tap(220, 212)
        self.assertEqual((self.app.index, self.app.home_scroll.offset, self.app.detail_open),
                         (index, offset, detail))

    def test_the_connection_screen_takes_the_view_with_it(self):
        self.app._set_payload(self.payload("usb-required"))
        self.tap(220, 212)
        self.app._poll_failed("Daemon offline", "TimeoutError", immediate=True)
        self.app._tick()
        self.assertFalse(self.app.lifecycle_open)

    def test_a_dock_tap_never_opens_a_card_detail(self):
        self.app._set_payload(self.payload("retry-exhausted"))
        self.tap(220, 212)
        self.assertFalse(self.app.detail_open)


class HeaderTests(AppBase):
    def test_the_header_carries_the_device_id_and_an_idle_report(self):
        from presto_ai_dash import device_protocol as protocol
        self.app._set_payload(self.payload())
        device_id, text = self.app._lifecycle_headers()
        self.assertEqual(device_id, DEVICE)
        parsed = protocol.parse_lifecycle(text)
        self.assertEqual(parsed["scheme"], "legacy-v1")
        self.assertEqual(parsed["revision"], REVISION)
        self.assertEqual(parsed["update"]["phase"], "idle")
        self.assertEqual(parsed["boot"], self.app.boot)

    def test_the_sequence_increases_with_each_report(self):
        from presto_ai_dash import device_protocol as protocol
        first = protocol.parse_lifecycle(self.app._lifecycle_headers()[1])
        second = protocol.parse_lifecycle(self.app._lifecycle_headers()[1])
        self.assertEqual((first["seq"], second["seq"]), (1, 2))

    def test_the_boot_counter_is_durable(self):
        self.assertGreaterEqual(self.app.boot, 1)
        self.assertTrue(os.path.exists(os.path.join(self.temp.name, "boot.txt")))


class NoDeviceIdTests(AppBase):
    config = {"DEVICE_ID": ""}

    def test_a_device_without_an_id_sends_no_lifecycle_header(self):
        self.assertIsNone(self.app.device_id)
        self.assertIsNone(self.app._lifecycle_headers())


if __name__ == "__main__":
    unittest.main()
