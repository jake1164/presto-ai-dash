"""Durable device identity, revision comparison, and update-state persistence.

These exercise the coordinator against a real SQLite file: what it accepts,
what it refuses, what survives a restart, and what it must never claim while
there is no updater.
"""

import json
from concurrent.futures import ThreadPoolExecutor
from email.message import Message
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from presto_ai_dash import device_protocol as protocol
from presto_ai_dash.device_lifecycle import (AdminRequestError, DeviceLifecycle,
                                             release_channel_name, request_hash)
from presto_ai_dash.storage import Storage

LEGACY = "1234567890abcdef" * 4
SLOT = "fedcba0987654321" * 4
DEVICE_A = "a" * 32
DEVICE_B = "b" * 32
DEVICE_C = "c" * 32


def identity(revision=LEGACY, scheme="legacy-v1", min_bootstrap=None):
    return {"scheme": scheme, "revision": revision, "min_bootstrap": min_bootstrap}


def headers(*pairs):
    """A header collection that keeps repeats, as a real request does."""
    message = Message()
    for name, value in pairs:
        message[name] = value
    return message


def lifecycle_header(boot=1, seq=1, revision=LEGACY[:12], scheme="legacy-v1",
                     bootstrap=None, slot=None, caps=(), update=None):
    report = {
        "v": 1, "boot": boot, "seq": seq,
        "scheme": scheme, "revision": revision,
        "bootstrap": bootstrap, "slot": slot, "boot_confirmed": True,
        "caps": list(caps), "update": update or protocol.idle_update(),
    }
    return protocol.lifecycle_text(report)


class Clock(object):
    def __init__(self, start=1793400000.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class Base(unittest.TestCase):
    expected = LEGACY

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.database = Path(self.temp.name) / "dash.db"
        self.storage = Storage(self.database)
        self.addCleanup(self.storage.close)
        self.clock = Clock()
        self.lifecycle = self.make()
        self.fixed = identity(self.expected)

    def make(self):
        return DeviceLifecycle(self.storage, expected_provider=lambda: self.fixed_identity(),
                               clock=self.clock, running_version="0.50.0", channel="pypi")

    def fixed_identity(self):
        return dict(self.fixed)

    def poll(self, lifecycle=None, device_id=DEVICE_A, revision=LEGACY[:12],
             name="presto", lifecycle_client=None):
        pairs = [("X-Presto-Client", name)]
        if revision is not None:
            pairs.append(("X-Presto-Revision", revision))
        if device_id is not None:
            pairs.append(("X-Presto-Device-ID", device_id))
        if lifecycle is not None:
            pairs.append(("X-Presto-Lifecycle", lifecycle))
        return lifecycle_client(pairs) if lifecycle_client else self.lifecycle.observe(headers(*pairs))


class LegacyClientTests(Base):
    """A device that predates the lifecycle protocol."""

    def test_a_matching_revision_is_current_but_only_self_reported(self):
        shown = self.poll(device_id=None)
        self.assertEqual(shown["report_status"], "legacy")
        self.assertIsNone(shown["device_id"])
        self.assertEqual(shown["comparison"], "equal")
        self.assertEqual(shown["update"]["state"], "current")
        self.assertFalse(shown["update"]["needs_attention"])
        self.assertEqual(self.storage.device_count(), 0)

    def test_a_different_revision_asks_for_a_usb_update(self):
        shown = self.poll(device_id=None, revision="0123456789ab")
        self.assertEqual(shown["comparison"], "different")
        self.assertEqual(shown["update"]["state"], "manual_action")
        self.assertEqual(shown["update"]["reason"], "usb_update_required")
        self.assertTrue(shown["update"]["needs_attention"])

    def test_no_revision_is_unknown_not_outdated(self):
        shown = self.poll(device_id=None, revision=None)
        self.assertEqual(shown["comparison"], "unknown")
        self.assertEqual(shown["update"]["state"], "unknown")
        self.assertEqual(shown["update"]["reason"], "revision_uncomparable")
        self.assertFalse(shown["update"]["needs_attention"])

    def test_a_full_legacy_hash_is_compared_in_full(self):
        self.assertEqual(self.poll(device_id=None, revision=LEGACY)["comparison"], "equal")
        self.assertEqual(self.poll(device_id=None, revision="0" * 64)["comparison"],
                         "different")

    def test_a_bare_full_hash_header_is_read_as_legacy_not_slot(self):
        # A 64-character header with no scheme is legacy telemetry compared in
        # full; it is never silently reinterpreted as a slot hash.
        shown = self.poll(device_id=None, revision=SLOT)
        self.assertEqual(shown["reported"]["scheme"], "legacy-v1")
        self.assertEqual(shown["comparison"], "different")

    def test_a_declared_slot_value_is_never_compared_with_a_legacy_aggregate(self):
        shown = self.poll(lifecycle_header(revision=SLOT, scheme="slot-v1",
                                          bootstrap=1, slot="a"),
                          revision=SLOT)
        self.assertEqual(shown["reported"]["scheme"], "slot-v1")
        self.assertEqual(shown["comparison"], "incomparable")
        self.assertEqual(shown["update"]["state"], "unknown")

    def test_a_legacy_client_is_never_persisted(self):
        for revision in (LEGACY[:12], "0123456789ab", SLOT, None):
            self.poll(device_id=None, revision=revision)
        self.assertEqual(self.storage.device_count(), 0)


class ReportingTests(Base):
    def test_a_full_revision_must_agree_with_its_compatibility_header(self):
        for scheme, revision in (("legacy-v1", LEGACY), ("slot-v1", SLOT)):
            with self.subTest(scheme=scheme):
                shown = self.poll(lifecycle_header(revision=revision, scheme=scheme),
                                  revision="0" * 12)
                self.assertEqual(shown["report_status"], "invalid")
                self.assertEqual(self.storage.device_count(), 0)

    def test_a_matching_or_absent_compatibility_header_accepts_a_full_revision(self):
        for seq, label in enumerate((LEGACY[:12], LEGACY, None), 1):
            with self.subTest(label=label):
                shown = self.poll(lifecycle_header(seq=seq, revision=LEGACY), revision=label)
                self.assertEqual(shown["report_status"], "accepted")

    def test_repeated_compatibility_headers_cannot_disambiguate_a_full_revision(self):
        shown = self.lifecycle.observe(headers(
            ("X-Presto-Device-ID", DEVICE_A),
            ("X-Presto-Lifecycle", lifecycle_header(revision=LEGACY)),
            ("X-Presto-Revision", LEGACY[:12]),
            ("X-Presto-Revision", "0" * 12)))
        self.assertEqual(shown["report_status"], "invalid")
        self.assertEqual(self.storage.device_count(), 0)

    def test_a_matching_report_is_accepted_and_stored(self):
        shown = self.poll(lifecycle_header())
        self.assertEqual(shown["device_id"], DEVICE_A)
        self.assertEqual(shown["report_status"], "accepted")
        self.assertEqual(shown["update"]["state"], "current")
        self.assertEqual(shown["update"]["attempts_started"], 0)
        self.assertEqual(shown["update"]["attempt_limit"], 3)
        self.assertFalse(shown["update"]["ota_enabled"])
        self.assertIsNone(shown["command"])
        self.assertIsNone(shown["confirm_attempt_id"])
        row = self.storage.device(DEVICE_A)
        self.assertEqual(row["name"], "presto")
        self.assertEqual(row["revision"], LEGACY[:12])
        self.assertEqual(row["update_state"], "current")

    def test_ota_is_disabled_and_no_command_is_ever_issued(self):
        shown = self.poll(lifecycle_header(revision="0123456789ab", caps=("ota-v1",)),
                          revision="0123456789ab")
        self.assertFalse(shown["update"]["ota_enabled"])
        self.assertIsNone(shown["command"])
        self.assertIsNone(shown["confirm_attempt_id"])
        self.assertEqual(shown["update"]["state"], "manual_action")

    def test_a_missing_device_id_falls_back_to_the_legacy_view(self):
        shown = self.poll(lifecycle_header(), device_id=None)
        self.assertEqual(shown["report_status"], "legacy")

    def test_a_malformed_device_id_is_refused_without_echo(self):
        shown = self.poll(lifecycle_header(), device_id="Z" * 32)
        self.assertEqual(shown["report_status"], "invalid")
        self.assertIsNone(shown["device_id"])
        self.assertEqual(self.storage.device_count(), 0)

    def test_a_valid_id_without_a_lifecycle_report_does_not_create_a_device(self):
        shown = self.poll(None)
        self.assertEqual(shown["report_status"], "invalid")
        self.assertEqual(shown["device_id"], DEVICE_A)
        self.assertEqual(self.storage.device_count(), 0)

    def test_a_malformed_lifecycle_report_is_refused_whole(self):
        shown = self.poll("not json")
        self.assertEqual(shown["report_status"], "invalid")
        self.assertEqual(self.storage.device_count(), 0)

    def test_an_unsupported_version_says_so(self):
        shown = self.poll('{"v":9,"boot":1}')
        self.assertEqual(shown["report_status"], "unsupported")
        self.assertEqual(self.storage.device_count(), 0)

    def test_a_repeated_header_invalidates_the_report(self):
        def doubled(pairs):
            return self.lifecycle.observe(headers(*pairs, ("X-Presto-Lifecycle",
                                                           lifecycle_header())))
        shown = self.poll(lifecycle_header(), lifecycle_client=doubled)
        self.assertEqual(shown["report_status"], "invalid")
        self.assertEqual(self.storage.device_count(), 0)

    def test_an_unverifiable_bundle_is_unknown_and_issues_nothing(self):
        self.fixed = None
        lifecycle = DeviceLifecycle(self.storage, expected_provider=lambda: None,
                                    clock=self.clock, running_version="0.50.0",
                                    channel="pypi")
        shown = lifecycle.observe(headers(("X-Presto-Device-ID", DEVICE_A),
                                          ("X-Presto-Lifecycle", lifecycle_header())))
        self.assertEqual(shown["update"]["state"], "unknown")
        self.assertEqual(shown["update"]["reason"], "bundle_unavailable")
        self.assertIsNone(shown["expected"])
        self.assertIsNone(shown["command"])

    def test_a_write_failure_degrades_to_unavailable(self):
        with patch.object(self.storage, "save_device", side_effect=RuntimeError("disk")):
            shown = self.poll(lifecycle_header())
        self.assertEqual(shown["report_status"], "unavailable")
        self.assertEqual(shown["update"]["reason"], "state_unavailable")
        self.assertIsNone(shown["command"])

    def test_a_name_of_unknown_never_blanks_an_established_name(self):
        self.poll(lifecycle_header(seq=1), name="Kitchen Presto")
        self.poll(lifecycle_header(seq=2), name="bad\u0000name")
        self.assertEqual(self.storage.device(DEVICE_A)["name"], "Kitchen Presto")


class OrderingTests(Base):
    def test_overlapping_polls_cannot_overwrite_a_newer_fault(self):
        self.poll(lifecycle_header(seq=1, revision="0" * 12), revision="0" * 12)
        writing = threading.Event()
        release = threading.Event()
        newer_started = threading.Event()
        newer_finished = threading.Event()
        save = self.storage.save_device

        def paused_save(row):
            if row["seq"] == 2:
                writing.set()
                if not release.wait(5):
                    raise RuntimeError("timed out waiting to release the older report")
            return save(row)

        def newer_poll():
            newer_started.set()
            try:
                return self.poll(lifecycle_header(seq=3, revision="0" * 12),
                                 revision="0" * 12)
            finally:
                newer_finished.set()

        with patch.object(self.storage, "save_device", side_effect=paused_save):
            with ThreadPoolExecutor(max_workers=2) as pool:
                older = pool.submit(self.poll, lifecycle_header(seq=2))
                try:
                    self.assertTrue(writing.wait(5))
                    newer = pool.submit(newer_poll)
                    self.assertTrue(newer_started.wait(5))
                    self.assertFalse(newer_finished.wait(0.2))
                finally:
                    release.set()
                self.assertEqual(older.result(timeout=5)["report_status"], "accepted")
                self.assertEqual(newer.result(timeout=5)["report_status"], "accepted")

        row = self.storage.device(DEVICE_A)
        self.assertEqual(row["seq"], 3)
        self.assertTrue(row["attention_latched"])
        restarted = self.make()
        replay = self.poll(lifecycle_header(seq=2), lifecycle_client=lambda pairs:
                           restarted.observe(headers(*pairs)))
        self.assertEqual(replay["report_status"], "stale")
        self.assertTrue(replay["update"]["needs_attention"])

    def test_a_repeated_report_is_idempotent(self):
        first = self.poll(lifecycle_header(seq=5))
        revision = self.storage.device(DEVICE_A)["state_revision"]
        again = self.poll(lifecycle_header(seq=5), revision=LEGACY[:12])
        self.assertEqual(again["report_status"], "accepted")
        self.assertEqual(self.storage.device(DEVICE_A)["state_revision"], revision)
        self.assertEqual(first["update"]["state"], again["update"]["state"])

    def test_an_equal_counter_with_a_different_report_is_invalid(self):
        self.poll(lifecycle_header(seq=5, revision=LEGACY[:12]))
        shown = self.poll(lifecycle_header(seq=5, revision="0123456789ab"),
                          revision="0123456789ab")
        self.assertEqual(shown["report_status"], "invalid")
        self.assertEqual(self.storage.device(DEVICE_A)["revision"], LEGACY[:12])

    def test_a_lower_counter_is_stale_and_changes_nothing(self):
        self.poll(lifecycle_header(boot=2, seq=9))
        before = self.storage.device(DEVICE_A)
        shown = self.poll(lifecycle_header(boot=2, seq=4, revision="0123456789ab"),
                          revision="0123456789ab")
        self.assertEqual(shown["report_status"], "stale")
        self.assertEqual(self.storage.device(DEVICE_A)["state_revision"],
                         before["state_revision"])
        self.assertEqual(self.storage.device(DEVICE_A)["update_state"], "current")

    def test_a_newer_boot_resets_the_sequence(self):
        self.poll(lifecycle_header(boot=1, seq=90))
        shown = self.poll(lifecycle_header(boot=2, seq=1))
        self.assertEqual(shown["report_status"], "accepted")

    def test_the_checkpointed_tuple_survives_a_restart(self):
        self.poll(lifecycle_header(boot=3, seq=7))
        restarted = DeviceLifecycle(self.storage, expected_provider=self.fixed_identity,
                                    clock=self.clock, running_version="0.50.0",
                                    channel="pypi")
        shown = restarted.observe(headers(("X-Presto-Device-ID", DEVICE_A),
                                          ("X-Presto-Lifecycle",
                                           lifecycle_header(boot=3, seq=4))))
        self.assertEqual(shown["report_status"], "stale")

    def test_a_transition_is_written_immediately(self):
        self.poll(lifecycle_header(seq=1, revision=LEGACY[:12]))
        self.poll(lifecycle_header(seq=2, revision="0123456789ab"),
                  revision="0123456789ab")
        self.assertEqual(self.storage.device(DEVICE_A)["update_state"], "manual_action")

    def test_an_unchanged_device_is_checkpointed_at_most_every_five_minutes(self):
        self.poll(lifecycle_header(seq=1))
        first = self.storage.device(DEVICE_A)["last_seen_at"]
        self.clock.advance(60)
        self.poll(lifecycle_header(seq=2))
        self.assertEqual(self.storage.device(DEVICE_A)["last_seen_at"], first)
        self.clock.advance(protocol.CHECKPOINT_SECONDS)
        self.poll(lifecycle_header(seq=3))
        self.assertNotEqual(self.storage.device(DEVICE_A)["last_seen_at"], first)


class AttentionTests(Base):
    def test_rejected_reports_preserve_the_warning_in_the_device_response(self):
        self.poll(lifecycle_header(seq=5, revision="0" * 12), revision="0" * 12)
        before = self.storage.device(DEVICE_A)
        for report, status in ((lifecycle_header(seq=4), "stale"),
                               (lifecycle_header(seq=5), "invalid"),
                               ("not json", "invalid"), ('{"v":9}', "unsupported")):
            with self.subTest(status=status, report=report):
                shown = self.poll(report)
                self.assertEqual(shown["report_status"], status)
                self.assertTrue(shown["update"]["needs_attention"])
                self.assertEqual(shown["update"]["reason"], "usb_update_required")
                self.assertEqual(self.storage.device(DEVICE_A), before)

    def test_a_mismatch_latches_and_a_matching_report_clears_it(self):
        self.poll(lifecycle_header(seq=1, revision="0123456789ab"),
                  revision="0123456789ab")
        self.assertEqual(self.storage.device(DEVICE_A)["attention_latched"], 1)
        self.assertEqual(self.lifecycle.attention_count(), 1)
        self.clock.advance(60)
        shown = self.poll(lifecycle_header(seq=2, revision=LEGACY[:12]))
        self.assertEqual(shown["update"]["state"], "current")
        self.assertFalse(shown["update"]["needs_attention"])
        self.assertEqual(self.storage.device(DEVICE_A)["attention_latched"], 0)
        self.assertEqual(self.lifecycle.attention_count(), 0)

    def test_attention_survives_a_restart_and_an_offline_device(self):
        self.poll(lifecycle_header(seq=1, revision="0123456789ab"),
                  revision="0123456789ab")
        restarted = DeviceLifecycle(self.storage, expected_provider=self.fixed_identity,
                                    clock=self.clock, running_version="0.50.0",
                                    channel="pypi")
        self.assertEqual(restarted.attention_count(), 1)
        self.clock.advance(3600)
        view = restarted.device_view(DEVICE_A)
        self.assertFalse(view["connected"])
        self.assertTrue(view["update"]["needs_attention"])
        self.assertEqual(view["update"]["state"], "manual_action")

    def test_an_unknown_state_does_not_clear_a_latch(self):
        self.poll(lifecycle_header(seq=1, revision="0123456789ab"),
                  revision="0123456789ab")
        self.poll(lifecycle_header(seq=2, scheme=None, revision=None), revision=None)
        self.assertEqual(self.storage.device(DEVICE_A)["attention_latched"], 1)


class DeviceFailurePhaseTests(Base):
    def attempt(self, phase, error=None, **kwargs):
        return lifecycle_header(update={"target": SLOT, "generation": "c" * 32,
                                        "attempt": "d" * 32, "command_ack": "e" * 32,
                                        "phase": phase, "error": error}, **kwargs)

    def test_a_device_reported_failure_is_shown_as_failed(self):
        shown = self.poll(self.attempt("failed", "boot_crash"))
        self.assertEqual(shown["update"]["state"], "failed")
        self.assertEqual(shown["update"]["reason"], "boot_crash")
        self.assertTrue(shown["update"]["needs_attention"])

    def test_a_blocked_report_is_manual_action(self):
        shown = self.poll(self.attempt("blocked", "storage_insufficient"))
        self.assertEqual(shown["update"]["state"], "manual_action")

    def test_progress_is_quiet(self):
        shown = self.poll(self.attempt("downloading"))
        self.assertEqual(shown["update"]["state"], "updating")
        self.assertFalse(shown["update"]["needs_attention"])


class RegistryTests(Base):
    def test_devices_with_the_same_name_are_kept_apart(self):
        self.poll(lifecycle_header(seq=1), device_id=DEVICE_A, name="presto")
        self.poll(lifecycle_header(seq=1), device_id=DEVICE_B, name="presto")
        view = self.lifecycle.devices_view(poll_interval=15)
        self.assertEqual([device["device_id"] for device in view["devices"]],
                         [DEVICE_A, DEVICE_B])

    def test_the_registry_stops_at_sixty_four_devices(self):
        for index in range(protocol.MAX_DEVICES):
            device_id = ("%032x" % index)
            self.lifecycle.observe(headers(("X-Presto-Device-ID", device_id),
                                           ("X-Presto-Lifecycle", lifecycle_header())))
        self.assertEqual(self.storage.device_count(), protocol.MAX_DEVICES)
        shown = self.poll(lifecycle_header(), device_id=DEVICE_C)
        self.assertEqual(shown["report_status"], "registry_full")
        self.assertEqual(shown["update"]["reason"], "registry_full")
        self.assertEqual(self.storage.device_count(), protocol.MAX_DEVICES)
        self.assertIsNone(self.storage.device(DEVICE_C))

    def test_an_existing_device_still_reports_when_the_registry_is_full(self):
        for index in range(protocol.MAX_DEVICES):
            device_id = ("%032x" % index)
            self.lifecycle.observe(headers(("X-Presto-Device-ID", device_id),
                                           ("X-Presto-Lifecycle", lifecycle_header())))
        shown = self.poll(lifecycle_header(seq=2), device_id="%032x" % 0)
        self.assertEqual(shown["report_status"], "accepted")


class AdminProjectionTests(Base):
    def test_live_polls_keep_a_device_connected_between_database_checkpoints(self):
        self.poll(lifecycle_header(seq=1))
        checkpoint = self.storage.device(DEVICE_A)["last_seen_at"]
        self.clock.advance(60)
        self.poll(lifecycle_header(seq=2))
        view = self.lifecycle.device_view(DEVICE_A, poll_interval=15)
        self.assertTrue(view["connected"])
        self.assertFalse(view["last_seen_is_checkpoint"])
        self.assertNotEqual(view["last_seen_at"], checkpoint)
        self.assertEqual(view["last_seen_checkpoint_at"], checkpoint)
        self.assertEqual(self.storage.device(DEVICE_A)["last_seen_at"], checkpoint)
        self.clock.advance(46)
        self.assertFalse(self.lifecycle.device_view(DEVICE_A, poll_interval=15)["connected"])

    def test_a_fresh_process_marks_last_seen_as_a_checkpoint(self):
        self.poll(lifecycle_header())
        restarted = DeviceLifecycle(self.storage, expected_provider=self.fixed_identity,
                                    clock=self.clock, running_version="0.50.0",
                                    channel="pypi")
        view = restarted.device_view(DEVICE_A)
        self.assertTrue(view["last_seen_is_checkpoint"])
        self.assertTrue(view["connected"])
        restarted.observe(headers(("X-Presto-Device-ID", DEVICE_A),
                                  ("X-Presto-Lifecycle", lifecycle_header(seq=2))))
        self.assertFalse(restarted.device_view(DEVICE_A)["last_seen_is_checkpoint"])

    def test_the_projection_carries_no_usb_evidence_and_no_device_verdict(self):
        self.poll(lifecycle_header())
        view = self.lifecycle.device_view(DEVICE_A)
        self.assertIsNone(view["usb_observation"])
        self.assertFalse(view["update"]["can_retry"])
        self.assertFalse(view["update"]["ota_enabled"])
        self.assertEqual(view["update"]["command_status"], "none")
        self.assertNotIn("report_json", json.dumps(view))

    def test_an_unknown_device_is_none(self):
        self.assertIsNone(self.lifecycle.device_view(DEVICE_B))
        self.assertIsNone(self.lifecycle.device_view("not-an-id"))

    def test_the_disabled_mutation_routes_refuse_known_devices(self):
        self.poll(lifecycle_header())
        with self.assertRaises(AdminRequestError) as retry:
            self.lifecycle.refuse_mutation(DEVICE_A, "retry")
        self.assertEqual((retry.exception.status, retry.exception.code),
                         (409, "ota_not_enabled"))
        with self.assertRaises(AdminRequestError) as recovery:
            self.lifecycle.refuse_mutation(DEVICE_A, "confirm-recovery")
        self.assertEqual(recovery.exception.status, 409)

    def test_an_unknown_or_malformed_device_is_not_found(self):
        for device_id in (DEVICE_B, "not-an-id"):
            with self.subTest(device_id=device_id):
                with self.assertRaises(AdminRequestError) as caught:
                    self.lifecycle.refuse_mutation(device_id, "retry")
                self.assertEqual(caught.exception.status, 404)

    def test_the_request_hash_covers_method_route_and_body(self):
        body = {"state_revision": 1}
        base = request_hash("POST", "/a", body)
        self.assertNotEqual(base, request_hash("PUT", "/a", body))
        self.assertNotEqual(base, request_hash("POST", "/b", body))
        self.assertNotEqual(base, request_hash("POST", "/a", {"state_revision": 2}))
        self.assertEqual(base, request_hash("POST", "/a", {"state_revision": 1}))


class ReleaseTests(Base):
    def setUp(self):
        super().setUp()
        # A background checker exists in these tests; its result is what is read.
        self.lifecycle.release_checks = True

    def test_without_a_checker_the_state_is_disabled_not_current(self):
        self.lifecycle.release_checks = False
        shown = self.lifecycle.release()
        self.assertEqual(shown["state"], "disabled")
        self.assertEqual(shown["running"], "0.50.0")
        self.assertEqual(shown["channel"], "pypi")
        self.assertIsNone(shown["latest"])

    def test_a_row_from_another_running_version_is_history(self):
        self.lifecycle.storage.save_release_cache({
            "channel": "pypi", "running": "0.49.0", "latest": "0.50.0",
            "checked_at": "2026-10-29T09:00:00Z",
            "last_attempt_at": "2026-10-29T09:00:00Z", "state": "available",
            "error_code": None, "source_url": None})
        self.assertEqual(self.lifecycle.release()["state"], "unknown")

    def test_a_fresh_available_result_is_returned(self):
        self.lifecycle.storage.save_release_cache({
            "channel": "pypi", "running": "0.50.0", "latest": "0.51.0",
            "checked_at": "2026-10-29T09:00:00Z",
            "last_attempt_at": "2026-10-29T09:00:00Z", "state": "available",
            "error_code": None, "source_url": "https://pypi.org/project/presto-ai-dash/"})
        shown = self.lifecycle.release()
        self.assertEqual(shown["state"], "available")
        self.assertEqual(shown["latest"], "0.51.0")
        self.assertEqual(shown["max_age_s"], 172800)

    def test_an_old_success_is_stale_not_current(self):
        self.lifecycle.storage.save_release_cache({
            "channel": "pypi", "running": "0.50.0", "latest": "0.51.0",
            "checked_at": "2026-10-20T09:00:00Z",
            "last_attempt_at": "2026-10-20T09:00:00Z", "state": "available",
            "error_code": None, "source_url": None})
        self.assertEqual(self.lifecycle.release()["state"], "stale")

    def test_a_restart_clears_a_check_that_was_in_flight(self):
        self.lifecycle.storage.save_release_cache({
            "channel": "pypi", "running": "0.50.0", "latest": None,
            "checked_at": None, "last_attempt_at": "2026-10-29T09:00:00Z",
            "state": "checking", "error_code": None, "source_url": None})
        self.storage.clear_release_checking()
        self.assertEqual(self.lifecycle.release()["state"], "unknown")

    def test_the_channel_is_chosen_from_the_installation_kind(self):
        self.assertIn(release_channel_name({}), ("pypi", "development"))


if __name__ == "__main__":
    unittest.main()
