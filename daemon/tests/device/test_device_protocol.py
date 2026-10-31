"""The device lifecycle wire contract: what a device may say, and how it compares.

The module under test holds no state, so every rule here is exercised directly:
the size and depth bounds, duplicate keys, booleans standing in for integers,
cross-field combinations, the two revision schemes, and the comparison that
decides whether a device is ``equal``, ``different``, ``incomparable`` or
``unknown``.
"""

import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from presto_ai_dash import device_protocol as protocol

DEVICE = "1" * 32
LEGACY_FULL = "a" * 64
SLOT_FULL = "b" * 64


def report(**overrides):
    """A valid v1 report, with the given fields replaced."""
    base = {
        "v": 1, "boot": 8, "seq": 42,
        "scheme": "legacy-v1", "revision": LEGACY_FULL[:12],
        "bootstrap": None, "slot": None, "boot_confirmed": True,
        "caps": [],
        "update": {"target": None, "generation": None, "attempt": None,
                   "command_ack": None, "phase": "idle", "error": None},
    }
    base.update(overrides)
    return base


def encoded(**overrides):
    return json.dumps(report(**overrides), separators=(",", ":"))


class VariableTests(unittest.TestCase):
    def test_a_healthy_legacy_report_is_accepted(self):
        parsed = protocol.parse_lifecycle(encoded())
        self.assertEqual(parsed["v"], 1)
        self.assertEqual(parsed["boot"], 8)
        self.assertEqual(parsed["seq"], 42)
        self.assertEqual(parsed["scheme"], "legacy-v1")
        self.assertEqual(parsed["caps"], [])
        self.assertEqual(parsed["update"]["phase"], "idle")
        self.assertIsNone(parsed["bootstrap"])
        self.assertIsNone(parsed["slot"])

    def test_the_serializer_round_trips_through_the_parser(self):
        parsed = protocol.parse_lifecycle(encoded())
        text = protocol.lifecycle_text(parsed)
        self.assertEqual(protocol.parse_lifecycle(text), parsed)
        self.assertNotIn(" ", text)

    def test_a_slot_report_with_a_bootstrap_version_is_accepted(self):
        parsed = protocol.parse_lifecycle(encoded(
            scheme="slot-v1", revision=SLOT_FULL, bootstrap=1, slot="a",
            caps=["ota-v1"]))
        self.assertEqual(parsed["bootstrap"], 1)
        self.assertEqual(parsed["slot"], "a")
        self.assertEqual(parsed["caps"], ["ota-v1"])

    def test_a_missing_revision_is_reported_as_both_null(self):
        parsed = protocol.parse_lifecycle(encoded(scheme=None, revision=None))
        self.assertIsNone(parsed["scheme"])
        self.assertIsNone(parsed["revision"])

    def test_only_the_scheme_or_only_the_revision_is_refused(self):
        for overrides in ({"scheme": None}, {"revision": None}):
            with self.subTest(overrides=overrides):
                with self.assertRaises(protocol.LifecycleError) as caught:
                    protocol.parse_lifecycle(encoded(**overrides))
                self.assertEqual(caught.exception.code, "invalid")

    def test_a_booleans_and_strings_are_not_counters(self):
        for overrides in ({"boot": True}, {"boot": "8"}, {"boot": 8.0},
                          {"seq": False}, {"boot": 0}, {"seq": 2147483648}):
            with self.subTest(overrides=overrides):
                with self.assertRaises(protocol.LifecycleError):
                    protocol.parse_lifecycle(encoded(**overrides))

    def test_boot_confirmed_must_be_a_real_boolean(self):
        for value in (1, 0, "true", None):
            with self.subTest(value=value):
                with self.assertRaises(protocol.LifecycleError):
                    protocol.parse_lifecycle(encoded(boot_confirmed=value))

    def test_the_slot_is_a_or_b_or_null(self):
        for value in ("c", "", 1):
            with self.subTest(value=value):
                with self.assertRaises(protocol.LifecycleError):
                    protocol.parse_lifecycle(encoded(slot=value))

    def test_an_unknown_version_is_unsupported_not_invalid(self):
        with self.assertRaises(protocol.LifecycleError) as caught:
            protocol.parse_lifecycle(encoded(v=2))
        self.assertEqual(caught.exception.code, "unsupported")

    def test_a_missing_version_is_invalid(self):
        payload = report()
        del payload["v"]
        with self.assertRaises(protocol.LifecycleError) as caught:
            protocol.parse_lifecycle(json.dumps(payload))
        self.assertEqual(caught.exception.code, "invalid")

    def test_an_unknown_key_invalidates_the_report(self):
        payload = report()
        payload["extra"] = 1
        with self.assertRaises(protocol.LifecycleError):
            protocol.parse_lifecycle(json.dumps(payload))

    def test_a_duplicate_key_invalidates_the_report(self):
        text = ('{"v":1,"v":1,"boot":8,"seq":42,"scheme":null,"revision":null,'
                '"bootstrap":null,"slot":null,"boot_confirmed":true,"caps":[],'
                '"update":{"target":null,"generation":null,"attempt":null,'
                '"command_ack":null,"phase":"idle","error":null}}')
        with self.assertRaises(protocol.LifecycleError):
            protocol.parse_lifecycle(text)

    def test_an_oversized_header_is_refused_before_parsing(self):
        padding = "1" * (protocol.LIFECYCLE_MAX_BYTES + 1)
        with self.assertRaises(protocol.LifecycleError):
            protocol.parse_lifecycle('{"v":1,"pad":"' + padding + '"}')

    def test_too_deep_nesting_is_refused(self):
        deep = '{"v":1,"boot":1,"seq":1,"scheme":null,"revision":null,' \
               '"bootstrap":null,"slot":null,"boot_confirmed":true,"caps":[],' \
               '"update":{"target":[[[[1]]]],"generation":null,"attempt":null,' \
               '"command_ack":null,"phase":"idle","error":null}}'
        with self.assertRaises(protocol.LifecycleError):
            protocol.parse_lifecycle(deep)

    def test_non_ascii_and_empty_headers_are_refused(self):
        for value in ("", None, '{"v":1,"boot":1}', "\u00e9"):
            with self.subTest(value=value):
                with self.assertRaises(protocol.LifecycleError):
                    protocol.parse_lifecycle(value)

    def test_the_rejected_value_is_never_echoed(self):
        try:
            protocol.parse_lifecycle('{"v":1,"secret":"hunter2"')
        except protocol.LifecycleError as exc:
            self.assertNotIn("hunter2", str(exc))

    def test_capabilities_are_unique_short_ascii_and_bounded(self):
        for caps in (["ota-v1", "ota-v1"], ["x" * 25], ["a\ttab"], ["a"] * 5, "ota-v1"):
            with self.subTest(caps=caps):
                with self.assertRaises(protocol.LifecycleError):
                    protocol.parse_lifecycle(encoded(caps=caps))

    def test_an_unknown_capability_is_kept_but_grants_nothing(self):
        parsed = protocol.parse_lifecycle(encoded(caps=["future-v9"]))
        self.assertEqual(parsed["caps"], ["future-v9"])
        self.assertNotIn("future-v9", protocol.KNOWN_CAPABILITIES)

    def test_idle_excludes_every_attempt_field(self):
        for field in ("target", "generation", "attempt", "command_ack", "error"):
            with self.subTest(field=field):
                update = protocol.idle_update()
                update[field] = "1" * 32
                with self.assertRaises(protocol.LifecycleError):
                    protocol.parse_lifecycle(encoded(update=update))

    def test_a_failed_phase_needs_an_error_and_the_other_phases_forbid_one(self):
        attempt = {"target": SLOT_FULL, "generation": "c" * 32, "attempt": "d" * 32,
                   "command_ack": "e" * 32}
        with self.assertRaises(protocol.LifecycleError):
            protocol.parse_lifecycle(encoded(update=dict(attempt, phase="failed", error=None)))
        with self.assertRaises(protocol.LifecycleError):
            protocol.parse_lifecycle(encoded(update=dict(attempt, phase="downloading",
                                                         error="http_error")))
        ok = protocol.parse_lifecycle(encoded(update=dict(attempt, phase="downloading",
                                                          error=None)))
        self.assertEqual(ok["update"]["phase"], "downloading")
        failed = protocol.parse_lifecycle(encoded(update=dict(attempt, phase="blocked",
                                                              error="storage_insufficient")))
        self.assertEqual(failed["update"]["error"], "storage_insufficient")

    def test_an_attempt_without_a_permit_is_refused(self):
        update = {"target": SLOT_FULL, "generation": None, "attempt": "d" * 32,
                  "command_ack": None, "phase": "verifying", "error": None}
        with self.assertRaises(protocol.LifecycleError):
            protocol.parse_lifecycle(encoded(update=update))

    def test_an_unknown_error_code_is_refused(self):
        update = {"target": None, "generation": None, "attempt": None,
                  "command_ack": None, "phase": "idle", "error": "made_up"}
        with self.assertRaises(protocol.LifecycleError):
            protocol.parse_lifecycle(encoded(update=update))


class IdentityTests(unittest.TestCase):
    def test_a_device_id_is_32_lowercase_hexadecimal_characters(self):
        self.assertTrue(protocol.valid_id(DEVICE))
        for value in ("1" * 31, "1" * 33, "A" * 32, "g" * 32, "", None, 123):
            with self.subTest(value=value):
                self.assertFalse(protocol.valid_id(value))

    def test_a_legacy_revision_may_be_short_or_full(self):
        self.assertTrue(protocol.valid_revision("legacy-v1", "a" * 12))
        self.assertTrue(protocol.valid_revision("legacy-v1", "a" * 64))
        self.assertFalse(protocol.valid_revision("legacy-v1", "a" * 40))

    def test_a_slot_revision_is_always_full(self):
        self.assertTrue(protocol.valid_revision("slot-v1", "a" * 64))
        self.assertFalse(protocol.valid_revision("slot-v1", "a" * 12))

    def test_an_unknown_scheme_has_no_valid_revision(self):
        self.assertFalse(protocol.valid_revision("slot-v2", "a" * 64))


class ComparisonTests(unittest.TestCase):
    def test_identical_hashes_are_equal_and_different_ones_are_not_ordered(self):
        expected = {"scheme": "slot-v1", "revision": SLOT_FULL}
        self.assertEqual(protocol.compare({"scheme": "slot-v1", "revision": SLOT_FULL},
                                          expected), "equal")
        self.assertEqual(protocol.compare({"scheme": "slot-v1", "revision": "c" * 64},
                                          expected), "different")

    def test_a_legacy_prefix_is_compared_with_a_legacy_expectation_only(self):
        expected = {"scheme": "legacy-v1", "revision": LEGACY_FULL}
        self.assertEqual(protocol.compare(
            {"scheme": "legacy-v1", "revision": LEGACY_FULL[:12]}, expected), "equal")
        self.assertEqual(protocol.compare(
            {"scheme": "legacy-v1", "revision": LEGACY_FULL}, expected), "equal")
        self.assertEqual(protocol.compare(
            {"scheme": "legacy-v1", "revision": "f" * 12}, expected), "different")

    def test_a_legacy_value_is_never_compared_with_a_slot_hash(self):
        self.assertEqual(protocol.compare(
            {"scheme": "legacy-v1", "revision": LEGACY_FULL[:12]},
            {"scheme": "slot-v1", "revision": LEGACY_FULL}), "incomparable")

    def test_a_missing_value_is_unknown_not_different(self):
        expected = {"scheme": "legacy-v1", "revision": LEGACY_FULL}
        self.assertEqual(protocol.compare({"scheme": None, "revision": None}, expected),
                         "unknown")
        self.assertEqual(protocol.compare({"scheme": "legacy-v1", "revision": None},
                                          expected), "unknown")
        self.assertEqual(protocol.compare(None, expected), "unknown")
        self.assertEqual(protocol.compare({"scheme": "legacy-v1", "revision": LEGACY_FULL},
                                          None), "unknown")


class BundleIdentityTests(unittest.TestCase):
    def test_a_wheel_bundle_reports_its_scheme_and_no_bootstrap(self):
        identity = protocol.bundle_identity(
            {"revision": LEGACY_FULL, "scheme": "legacy-v1", "min_bootstrap": None})
        self.assertEqual(identity, {"scheme": "legacy-v1", "revision": LEGACY_FULL,
                                    "min_bootstrap": None})

    def test_a_legacy_bundle_never_requires_a_bootstrap(self):
        identity = protocol.bundle_identity(
            {"revision": LEGACY_FULL, "scheme": "legacy-v1", "min_bootstrap": 3})
        self.assertIsNone(identity["min_bootstrap"])

    def test_a_bundle_without_a_scheme_is_read_as_legacy(self):
        identity = protocol.bundle_identity({"revision": LEGACY_FULL})
        self.assertEqual(identity["scheme"], "legacy-v1")

    def test_an_unverifiable_bundle_has_no_identity(self):
        for bundle in (None, {}, {"revision": "a" * 12}, {"revision": "A" * 64},
                       {"revision": LEGACY_FULL, "scheme": "v9"},
                       {"revision": LEGACY_FULL, "scheme": "slot-v1", "min_bootstrap": 0}):
            with self.subTest(bundle=bundle):
                self.assertIsNone(protocol.bundle_identity(bundle))

    def test_the_expected_shape_carries_the_bootstrap_requirement(self):
        shape = protocol.expected_shape(
            {"scheme": "slot-v1", "revision": SLOT_FULL, "min_bootstrap": 1})
        self.assertEqual(shape, {"scheme": "slot-v1", "revision": SLOT_FULL,
                                 "min_bootstrap": 1})
        self.assertIsNone(protocol.expected_shape(None))


if __name__ == "__main__":
    unittest.main()
