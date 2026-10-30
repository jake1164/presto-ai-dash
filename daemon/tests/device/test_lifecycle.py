"""The device half of the lifecycle protocol.

``presto/lifecycle.py`` builds the header this device sends and turns the
daemon's two extension objects into the short ASCII lines the shared indicator
shows. Both are pure Python, so they run here exactly as they do on the device.
"""

import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

import presto_path  # noqa: F401
import lifecycle

DEVICE = "11111111111111111111111111111111"
REVISION = "a" * 12

DAEMON_DIR = Path(__file__).resolve().parents[2] / "src"
if str(DAEMON_DIR) not in sys.path:
    sys.path.append(str(DAEMON_DIR))
from presto_ai_dash import device_protocol as protocol  # noqa: E402


def lifecycle_object(state="current", reason=None, attention=False):
    return {
        "v": 1, "device_id": DEVICE, "report_status": "accepted",
        "reported": {"scheme": "legacy-v1", "revision": REVISION, "bootstrap": None},
        "expected": {"scheme": "legacy-v1", "revision": "a" * 64, "min_bootstrap": None},
        "comparison": "equal",
        "update": {"state": state, "reason": reason, "needs_attention": attention,
                   "ota_enabled": False, "generation": None, "attempt": None,
                   "attempts_started": 0, "attempt_limit": 3, "next_attempt_at": None,
                   "attempt_deadline": None, "confirmation_deadline": None},
        "command": None, "confirm_attempt_id": None,
    }


def release_object(state="available", running="0.50.0", latest="0.51.0"):
    return {"v": 1, "running": running, "latest": latest if state == "available" else None,
            "channel": "pypi", "state": state, "last_attempt_at": None,
            "checked_at": "2026-10-29T09:00:00Z", "max_age_s": 172800,
            "error": None, "source_url": None}


class BuildTests(unittest.TestCase):
    def test_a_legacy_report_is_the_contract_the_daemon_accepts(self):
        text = lifecycle.build(DEVICE, 3, 7, REVISION)
        parsed = protocol.parse_lifecycle(text)
        self.assertEqual(parsed["boot"], 3)
        self.assertEqual(parsed["seq"], 7)
        self.assertEqual(parsed["scheme"], "legacy-v1")
        self.assertEqual(parsed["revision"], REVISION)
        self.assertIsNone(parsed["bootstrap"])
        self.assertIsNone(parsed["slot"])
        self.assertIs(parsed["boot_confirmed"], True)
        self.assertEqual(parsed["caps"], [])
        self.assertEqual(parsed["update"]["phase"], "idle")

    def test_a_full_legacy_hash_is_accepted(self):
        parsed = protocol.parse_lifecycle(lifecycle.build(DEVICE, 1, 1, "a" * 64))
        self.assertEqual(len(parsed["revision"]), 64)

    def test_an_unknown_revision_reports_both_fields_null(self):
        parsed = protocol.parse_lifecycle(lifecycle.build(DEVICE, 1, 1, None))
        self.assertIsNone(parsed["scheme"])
        self.assertIsNone(parsed["revision"])

    def test_the_header_is_compact(self):
        text = lifecycle.build(DEVICE, 1, 1, REVISION)
        self.assertNotIn(" ", text)
        self.assertLessEqual(len(text.encode("ascii")), protocol.LIFECYCLE_MAX_BYTES)

    def test_an_unusable_identity_or_counter_builds_nothing(self):
        for arguments in ((None, 1, 1, REVISION), ("", 1, 1, REVISION),
                          ("Z" * 32, 1, 1, REVISION), (DEVICE, 0, 1, REVISION),
                          (DEVICE, 1, 0, REVISION), (DEVICE, True, 1, REVISION),
                          (DEVICE, 1, 1, "nothex"), (DEVICE, 1, 1, "a" * 40)):
            with self.subTest(arguments=arguments):
                self.assertIsNone(lifecycle.build(*arguments))

    def test_the_header_never_carries_a_non_idle_phase(self):
        text = lifecycle.build(DEVICE, 1, 1, REVISION)
        parsed = protocol.parse_lifecycle(text)
        self.assertEqual(parsed["update"]["phase"], "idle")
        self.assertIsNone(parsed["update"]["error"])


class NoticeTests(unittest.TestCase):
    def test_nothing_to_say_is_none(self):
        self.assertIsNone(lifecycle.notice({}))
        self.assertIsNone(lifecycle.notice({"device_lifecycle": lifecycle_object()}))
        self.assertIsNone(lifecycle.notice(None))
        self.assertIsNone(lifecycle.notice("payload"))

    def test_a_current_device_and_a_disabled_release_stay_quiet(self):
        payload = {"device_lifecycle": lifecycle_object(),
                   "daemon_release": release_object("disabled")}
        self.assertIsNone(lifecycle.notice(payload))

    def test_a_usb_update_is_a_warning_with_guidance(self):
        payload = {"device_lifecycle": lifecycle_object("manual_action",
                                                        "usb_update_required", True)}
        shown = lifecycle.notice(payload)
        self.assertTrue(shown["warning"])
        self.assertFalse(shown["release"])
        self.assertIn("presto_setup.py", " ".join(shown["lines"]))
        self.assertEqual(lifecycle.kind(shown), "warning")

    def test_a_release_alone_is_the_blue_arrow(self):
        payload = {"daemon_release": release_object()}
        shown = lifecycle.notice(payload)
        self.assertFalse(shown["warning"])
        self.assertTrue(shown["release"])
        self.assertEqual(lifecycle.kind(shown), "release")
        self.assertIn("0.50.0 -> 0.51.0", shown["lines"])

    def test_both_at_once_list_the_device_problem_first(self):
        payload = {"device_lifecycle": lifecycle_object("manual_action",
                                                        "usb_update_required", True),
                   "daemon_release": release_object()}
        shown = lifecycle.notice(payload)
        self.assertEqual(lifecycle.kind(shown), "warning")
        text = " ".join(shown["lines"])
        self.assertLess(text.index("Presto app"), text.index("Daemon update"))
        self.assertIn("Open daemon admin", text)

    def test_an_unknown_reason_shows_nothing_rather_than_echoing_it(self):
        payload = {"device_lifecycle": lifecycle_object("failed", "<script>", True)}
        self.assertIsNone(lifecycle.notice(payload))

    def test_a_release_that_is_not_fresh_is_not_announced(self):
        for state in ("stale", "error", "checking", "unknown", "current", "disabled"):
            with self.subTest(state=state):
                self.assertIsNone(lifecycle.notice({"daemon_release": release_object(state)}))

    def test_an_unusable_version_label_is_not_drawn(self):
        for latest in ("", "0.5 " , "x" * 40, 12, None):
            with self.subTest(latest=latest):
                payload = {"daemon_release": release_object("available", latest=latest)}
                self.assertIsNone(lifecycle.notice(payload))

    def test_a_release_equal_to_the_running_version_is_not_announced(self):
        payload = {"daemon_release": release_object("available", latest="0.50.0")}
        self.assertIsNone(lifecycle.notice(payload))

    def test_every_line_is_ascii_and_inside_the_screen_budget(self):
        payload = {"device_lifecycle": lifecycle_object("manual_action",
                                                        "usb_update_required", True),
                   "daemon_release": release_object()}
        shown = lifecycle.notice(payload)
        self.assertLessEqual(len(shown["lines"]), lifecycle.MAX_NOTICE_LINES)
        for line in shown["lines"]:
            self.assertLessEqual(len(line), lifecycle.MAX_LINE_CHARS)
            self.assertTrue(all(" " <= char <= "~" for char in line))

    def test_every_reason_text_is_drawable(self):
        for reason, lines in lifecycle.REASON_TEXT.items():
            with self.subTest(reason=reason):
                self.assertLessEqual(len(lines), lifecycle.MAX_NOTICE_LINES)
                for line in lines:
                    self.assertLessEqual(len(line), lifecycle.MAX_LINE_CHARS)
                    self.assertTrue(all(" " <= char <= "~" for char in line))


class BootCounterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = os.path.join(self.temp.name, "boot.txt")

    def test_the_counter_starts_at_one_and_increments_each_start(self):
        self.assertEqual(lifecycle.bump_boot(self.path), 1)
        self.assertEqual(lifecycle.bump_boot(self.path), 2)
        self.assertEqual(lifecycle.bump_boot(self.path), 3)

    def test_the_counter_is_readable_after_the_write(self):
        self.assertEqual(lifecycle.bump_boot(self.path), 1)
        with open(self.path, "r") as handle:
            self.assertEqual(handle.read(), "1")

    def test_a_corrupt_counter_recovers_from_the_temporary_file(self):
        with open(self.path, "w") as handle:
            handle.write("7")
        with open(self.path + ".tmp", "w") as handle:
            handle.write("9")
        os.remove(self.path)
        self.assertEqual(lifecycle.bump_boot(self.path), 10)

    def test_a_corrupt_counter_without_a_copy_restarts_at_one(self):
        with open(self.path, "w") as handle:
            handle.write("not a number")
        self.assertEqual(lifecycle.bump_boot(self.path), 1)

    def test_an_unwritable_location_does_not_raise(self):
        self.assertEqual(lifecycle.bump_boot(os.path.join(self.temp.name, "taken",
                                                          "boot.txt")), 1)


if __name__ == "__main__":
    unittest.main()
