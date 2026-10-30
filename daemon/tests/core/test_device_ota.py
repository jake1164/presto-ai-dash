"""Operational update coordination, exercised with OTA enabled in an isolated store.

These tests are the section 8.4 contract: a permit is persisted before it is
returned, one attempt exists per device, only transient failures retry, a
candidate confirms in two polls, a lost report is `unconfirmed` rather than a
failure, and a manual retry grants a fresh budget without clearing the warning.

Normal operation never enables any of this: the shipped default keeps
``ota_enabled`` false, which the last test class covers.
"""

from email.message import Message
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from presto_ai_dash import device_protocol as protocol
from presto_ai_dash.device_lifecycle import AdminRequestError, DeviceLifecycle
from presto_ai_dash.storage import Storage

LEGACY = "1234567890abcdef" * 4
TARGET = "a" * 64
OTHER = "b" * 64
NEWER = "c" * 64
DEVICE = "d" * 32


class Clock(object):
    def __init__(self, start=1793400000.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def bundle(slot_revision=TARGET):
    """A daemon bundle that carries both a legacy and a slot identity."""
    return {"scheme": "legacy-v1", "revision": LEGACY, "min_bootstrap": None,
            "slot": {"scheme": "slot-v1", "revision": slot_revision, "min_bootstrap": 1},
            "bootstrap": {"version": 1}}


def headers(*pairs):
    message = Message()
    for name, value in pairs:
        message[name] = value
    return message


def report(boot=1, seq=1, revision=OTHER, caps=("ota-v1",), update=None,
           boot_confirmed=True, slot="a", bootstrap=1):
    value = {"v": 1, "boot": boot, "seq": seq, "scheme": "slot-v1",
             "revision": revision, "bootstrap": bootstrap, "slot": slot,
             "boot_confirmed": boot_confirmed, "caps": list(caps),
             "update": update or protocol.idle_update()}
    return protocol.lifecycle_text(value)


def phase(name, error=None, permit=None, target=TARGET):
    """A device phase report that names the permit the daemon actually issued."""
    return {"target": target,
            "generation": permit["generation"] if permit else None,
            "attempt": permit["attempt"] if permit else None,
            "command_ack": None, "phase": name, "error": error}


class Base(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.database = Path(self.temp.name) / "dash.db"
        self.storage = Storage(self.database)
        self.addCleanup(self.storage.close)
        self.clock = Clock()
        self.lifecycle = self.make()

    def make(self, slot_revision=TARGET, ota=True):
        return DeviceLifecycle(self.storage,
                               expected_provider=lambda: bundle(slot_revision),
                               clock=self.clock, running_version="0.50.0",
                               channel="pypi", ota_enabled=ota)

    def poll(self, text, label=OTHER[:12]):
        return self.lifecycle.observe(headers(
            ("X-Presto-Client", "presto"), ("X-Presto-Revision", label),
            ("X-Presto-Device-ID", DEVICE), ("X-Presto-Lifecycle", text)))

    def stored(self):
        return self.storage.device(DEVICE)

    def attempt(self):
        row = self.stored()
        return (self.storage.device_attempt(row["active_attempt_id"])
                if row["active_attempt_id"] else None)

    def budget(self):
        return self.storage.active_budget(DEVICE)

    def latest_budget(self):
        rows = self.storage.device_budgets(DEVICE)
        return rows[0] if rows else None

    def latest_attempt(self):
        rows = self.storage.device_attempts(DEVICE, 1)
        return rows[0] if rows else None

    def start(self, boot=1):
        """Take a permit and report that its transfer began."""
        permit = self.poll(report(boot=boot))["command"]
        self.assertIsNotNone(permit)
        self.poll(report(boot=boot, seq=2, update=phase("downloading", permit=permit)))
        return permit


class PermitTests(Base):
    def test_terminal_failure_after_lost_progress_spends_each_attempt_once(self):
        permit = self.poll(report())["command"]
        first_id = permit["attempt"]
        sequence = 1
        for number in range(1, 4):
            sequence += 1
            self.poll(report(seq=sequence, update=phase("failed", "transfer_timeout", permit)))
            self.assertEqual(self.latest_budget()["attempts_started"], number)
            if number < 3:
                self.clock.advance((60, 300)[number - 1])
                sequence += 1
                permit = self.poll(report(seq=sequence))["command"]
                self.assertNotEqual(permit["attempt"], first_id)
        self.assertEqual(self.stored()["update_state"], "failed")
        self.assertEqual(self.latest_budget()["status"], "exhausted")

    def test_a_first_report_gets_one_permit_and_the_rows_are_persisted(self):
        seen = self.poll(report())
        self.assertEqual(seen["update"]["state"], "updating")
        permit = seen["command"]
        self.assertIsNotNone(permit)
        self.assertEqual(permit["target_revision"], TARGET)
        self.assertEqual(permit["manifest_path"], "/api/v1/device-app/manifest")
        stored = self.stored()
        self.assertEqual(stored["active_generation_id"], permit["generation"])
        self.assertEqual(stored["active_attempt_id"], permit["attempt"])
        self.assertEqual(self.attempt()["status"], "queued")
        self.assertEqual(self.budget()["attempts_started"], 0)

    def test_a_repeated_poll_returns_the_same_permit_without_a_second_attempt(self):
        first = self.poll(report())
        second = self.poll(report(seq=2))
        self.assertEqual(first["command"], second["command"])
        self.assertEqual(len(self.storage.device_attempts(DEVICE)), 1)
        self.assertEqual(len(self.storage.device_budgets(DEVICE)), 1)

    def test_a_matching_device_is_quiet_and_gets_nothing(self):
        seen = self.poll(report(revision=TARGET), label=TARGET[:12])
        self.assertEqual(seen["update"]["state"], "current")
        self.assertIsNone(seen["command"])
        self.assertFalse(seen["update"]["needs_attention"])

    def test_a_device_without_the_capability_is_never_offered_an_impossible_update(self):
        seen = self.poll(report(caps=()))
        self.assertIsNone(seen["command"])
        self.assertEqual(seen["update"]["state"], "manual_action")
        self.assertEqual(seen["update"]["reason"], "usb_update_required")
        self.assertTrue(seen["update"]["needs_attention"])
        self.assertIsNone(self.budget())
        self.assertIsNone(self.stored()["active_attempt_id"])

    def test_a_legacy_device_is_never_commanded(self):
        text = protocol.lifecycle_text({"v": 1, "boot": 1, "seq": 1,
                                        "scheme": "legacy-v1", "revision": LEGACY[:12],
                                        "bootstrap": None, "slot": None,
                                        "boot_confirmed": True, "caps": ["ota-v1"],
                                        "update": protocol.idle_update()})
        seen = self.poll(text, label=LEGACY[:12])
        self.assertIsNone(seen["command"])
        # The legacy aggregate matches this daemon's legacy bundle, so the
        # device is current on its own layout; it is never handed a slot target.
        self.assertEqual(seen["comparison"], "equal")
        self.assertEqual(seen["update"]["state"], "current")

    def test_an_unchanged_target_does_not_grant_a_second_budget_after_a_restart(self):
        permit = self.poll(report())["command"]
        self.lifecycle = self.make()
        seen = self.poll(report(seq=2))
        self.assertEqual(seen["command"], permit)
        self.assertEqual(len(self.storage.device_budgets(DEVICE)), 1)

    def test_a_target_change_cancels_an_unacknowledged_permit(self):
        first = self.poll(report())["command"]
        self.assertEqual(first["target_revision"], TARGET)
        self.lifecycle = self.make(slot_revision=NEWER)
        second = self.poll(report(seq=2))
        self.assertEqual(second["command"]["target_revision"], NEWER)
        self.assertNotEqual(second["command"]["attempt"], first["attempt"])
        old = [row for row in self.storage.device_budgets(DEVICE)
               if row["target_revision"] == TARGET][0]
        self.assertEqual(old["status"], "superseded")
        self.assertEqual(self.storage.device_attempt(first["attempt"])["status"],
                         "cancelled")


class AttemptTests(Base):

    def test_the_attempt_is_spent_once_when_it_proves_it_started(self):
        self.start()
        self.assertEqual(self.budget()["attempts_started"], 1)
        attempt = self.attempt()
        self.assertEqual(attempt["status"], "started")
        deadline = attempt["attempt_deadline"]
        self.poll(report(seq=3, update=phase("verifying", permit={
            "generation": attempt["generation_id"], "attempt": attempt["attempt_id"]})))
        self.assertEqual(self.attempt()["attempt_deadline"], deadline)

    def test_a_transient_failure_schedules_the_one_minute_retry(self):
        permit = self.start()
        seen = self.poll(report(seq=3, update=phase("failed", "connection_lost", permit)))
        self.assertEqual(seen["update"]["state"], "pending")
        self.assertTrue(seen["update"]["needs_attention"])
        budget = self.budget()
        self.assertEqual(budget["attempts_started"], 1)
        self.assertIsNotNone(budget["next_attempt_at"])
        self.assertIsNone(self.attempt())

    def test_the_retry_schedule_is_one_minute_then_five_and_then_exhausted(self):
        permit = self.start(boot=1)
        self.poll(report(boot=1, seq=3, update=phase("failed", "connection_lost", permit)))
        self.clock.advance(61)
        second = self.poll(report(boot=2, seq=1))["command"]
        self.assertEqual(second["attempt_no"], 2)
        self.poll(report(boot=2, seq=2, update=phase("downloading", permit=second)))
        self.poll(report(boot=2, seq=3, update=phase("failed", "transfer_timeout", second)))
        self.clock.advance(301)
        third = self.poll(report(boot=3, seq=1))["command"]
        self.assertEqual(third["attempt_no"], 3)
        self.poll(report(boot=3, seq=2, update=phase("downloading", permit=third)))
        exhausted = self.poll(report(boot=3, seq=3,
                                     update=phase("failed", "transfer_timeout", third)))
        self.assertEqual(exhausted["update"]["state"], "failed")
        self.assertEqual(exhausted["update"]["reason"], "retry_exhausted")
        self.assertEqual(self.latest_budget()["status"], "exhausted")

    def test_a_retry_is_not_granted_before_its_due_time(self):
        permit = self.start()
        self.poll(report(seq=3, update=phase("failed", "connection_lost", permit)))
        seen = self.poll(report(seq=4))
        self.assertIsNone(seen["command"])
        self.assertEqual(seen["update"]["state"], "pending")

    def test_a_block_is_manual_action_and_never_retries(self):
        permit = self.start()
        seen = self.poll(report(seq=3, update=phase("blocked", "storage_insufficient", permit)))
        self.assertEqual(seen["update"]["state"], "manual_action")
        self.assertEqual(seen["update"]["reason"], "storage_insufficient")
        self.assertIsNone(self.attempt())
        self.assertEqual(self.storage.device_budgets(DEVICE)[0]["status"], "exhausted")
        self.clock.advance(1000)
        again = self.poll(report(seq=4))
        self.assertIsNone(again["command"])
        self.assertTrue(again["update"]["needs_attention"])

    def test_a_first_boot_crash_stops_immediately_without_another_attempt(self):
        permit = self.start()
        seen = self.poll(report(seq=3, update=phase("rolled_back", "boot_crash", permit)))
        self.assertEqual(seen["update"]["state"], "failed")
        self.clock.advance(1000)
        self.assertIsNone(self.poll(report(seq=4))["command"])

    def test_an_attempt_that_stops_reporting_becomes_unconfirmed_not_failed(self):
        self.start()
        self.clock.advance(901)
        seen = self.poll(report(seq=3))
        self.assertEqual(seen["update"]["state"], "unconfirmed")
        self.assertEqual(seen["update"]["reason"], "attempt_timeout")
        self.assertEqual(self.attempt()["status"], "unconfirmed")

    def test_the_state_survives_a_daemon_restart_without_a_fabricated_success(self):
        self.start()
        self.clock.advance(901)
        self.poll(report(seq=3))
        self.lifecycle = self.make()
        self.lifecycle.clock = self.clock
        view = self.lifecycle.device_view(DEVICE, 15)
        self.assertEqual(view["update"]["state"], "unconfirmed")
        self.assertTrue(view["update"]["needs_attention"])
        self.assertTrue(view["last_seen_is_checkpoint"])


class ConfirmationTests(Base):

    def candidate(self, permit, seq, boot_confirmed, phase_name, revision=TARGET):
        return report(boot=2, seq=seq, revision=revision, slot="b",
                      boot_confirmed=boot_confirmed,
                      update=phase(phase_name, permit=permit))

    def test_the_two_poll_handshake_acknowledges_then_confirms(self):
        permit = self.start()
        confirming = self.poll(self.candidate(permit, 1, False, "confirming"),
                               label=TARGET[:12])
        self.assertEqual(confirming["confirm_attempt_id"], permit["attempt"])
        self.assertEqual(confirming["update"]["state"], "updating")
        self.assertEqual(self.attempt()["status"], "confirming")
        done = self.poll(self.candidate(permit, 2, True, "succeeded"), label=TARGET[:12])
        self.assertEqual(done["update"]["state"], "current")
        self.assertFalse(done["update"]["needs_attention"])
        self.assertIsNone(self.attempt())
        self.assertEqual(self.latest_budget()["status"], "succeeded")

    def test_a_lost_acknowledgment_is_repeated_rather_than_recreated(self):
        permit = self.start()
        first = self.poll(self.candidate(permit, 1, False, "confirming"), label=TARGET[:12])
        second = self.poll(self.candidate(permit, 2, False, "confirming"), label=TARGET[:12])
        self.assertEqual(first["confirm_attempt_id"], permit["attempt"])
        self.assertEqual(second["confirm_attempt_id"], permit["attempt"])
        self.assertEqual(len(self.storage.device_attempts(DEVICE)), 1)

    def test_confirmation_without_the_expected_revision_is_not_acknowledged(self):
        permit = self.start()
        seen = self.poll(self.candidate(permit, 1, False, "confirming", revision=OTHER))
        self.assertIsNone(seen["confirm_attempt_id"])
        self.assertNotEqual(self.attempt()["status"], "confirming")

    def test_a_lost_confirmation_report_times_out_as_unconfirmed(self):
        permit = self.start()
        self.poll(self.candidate(permit, 1, False, "confirming"), label=TARGET[:12])
        self.clock.advance(181)
        seen = self.poll(self.candidate(permit, 2, False, "confirming"), label=TARGET[:12])
        self.assertEqual(seen["update"]["state"], "unconfirmed")
        self.assertEqual(seen["update"]["reason"], "confirmation_timeout")


class MutationTests(Base):
    def test_concurrent_duplicate_retries_share_one_atomic_result(self):
        from concurrent.futures import ThreadPoolExecutor
        self.exhaust()
        body = self.body()
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(self.lifecycle.authorize_retry, DEVICE, body, 'f' * 32)
                       for _ in range(2)]
            first, second = [future.result(timeout=5) for future in futures]
        self.assertEqual(first, second)
        self.assertEqual(len(self.storage.device_budgets(DEVICE)), 2)
    def test_request_result_failure_rolls_back_the_entire_retry(self):
        self.exhaust()
        before = self.stored()
        budgets = self.storage.device_budgets(DEVICE)
        # Fail the actual INSERT inside the transaction, after row/budget writes.
        self.storage._conn.execute("CREATE TRIGGER reject_admin_request BEFORE INSERT ON device_admin_requests "
                                   "BEGIN SELECT RAISE(ABORT, 'synthetic failure'); END")
        with self.assertRaises(AdminRequestError) as caught:
            self.lifecycle.authorize_retry(DEVICE, self.body(), "f" * 32)
        self.assertEqual(caught.exception.code, "state_unavailable")
        self.assertEqual(self.stored(), before)
        self.assertEqual(self.storage.device_budgets(DEVICE), budgets)
        self.storage._conn.execute("DROP TRIGGER reject_admin_request")
        body = self.body()
        result = self.lifecycle.authorize_retry(DEVICE, body, "f" * 32)
        restarted = self.make()
        self.assertEqual(restarted.authorize_retry(DEVICE, body, "f" * 32), result)

    def body(self, revision=TARGET, state_revision=None):
        if state_revision is None:
            state_revision = self.stored()["state_revision"]
        return {"state_revision": state_revision, "target_scheme": "slot-v1",
                "target_revision": revision}

    def exhaust(self):
        """Leave the device failed with its automatic budget exhausted."""
        permit = self.start(boot=1)
        self.poll(report(boot=1, seq=3, update=phase("failed", "connection_lost", permit)))
        for boot in (2, 3):
            self.clock.advance(301)
            permit = self.poll(report(boot=boot, seq=1))["command"]
            self.poll(report(boot=boot, seq=2, update=phase("downloading", permit=permit)))
            self.poll(report(boot=boot, seq=3,
                             update=phase("failed", "connection_lost", permit)))
        self.assertEqual(self.stored()["update_state"], "failed")
        return self.stored()

    def test_retry_grants_a_fresh_budget_and_keeps_the_warning(self):
        self.exhaust()
        previous = self.latest_budget()["generation_id"]
        result = self.lifecycle.authorize_retry(DEVICE, self.body(), "f" * 32)
        self.assertTrue(result["queued"])
        self.assertEqual(result["state_revision"], self.stored()["state_revision"])
        self.assertTrue(self.stored()["attention_latched"])
        self.assertEqual(self.budget()["attempts_started"], 0)
        self.assertNotEqual(self.budget()["generation_id"], previous)

    def test_the_same_idempotency_key_returns_the_saved_answer(self):
        self.exhaust()
        body = self.body()
        first = self.lifecycle.authorize_retry(DEVICE, body, "f" * 32)
        second = self.lifecycle.authorize_retry(DEVICE, body, "f" * 32)
        self.assertEqual(first, second)
        # One automatic budget spent three attempts; retry adds exactly one.
        self.assertEqual(len(self.storage.device_budgets(DEVICE)), 2)

    def test_a_reused_key_with_a_different_body_is_a_conflict(self):
        self.exhaust()
        body = self.body()
        self.lifecycle.authorize_retry(DEVICE, body, "f" * 32)
        with self.assertRaises(AdminRequestError) as caught:
            self.lifecycle.authorize_retry(DEVICE, dict(body, state_revision=99), "f" * 32)
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(caught.exception.code, "idempotency_conflict")

    def test_a_stale_state_revision_is_refused(self):
        self.exhaust()
        with self.assertRaises(AdminRequestError) as caught:
            self.lifecycle.authorize_retry(DEVICE, self.body(state_revision=1), "f" * 32)
        self.assertEqual(caught.exception.code, "state_changed")

    def test_a_changed_target_is_refused(self):
        self.exhaust()
        with self.assertRaises(AdminRequestError) as caught:
            self.lifecycle.authorize_retry(DEVICE, self.body(revision=OTHER), "f" * 32)
        self.assertEqual(caught.exception.code, "target_changed")

    def test_an_unresolved_attempt_cannot_be_retried(self):
        permit = self.start()
        with self.assertRaises(AdminRequestError) as caught:
            self.lifecycle.authorize_retry(DEVICE, self.body(), "f" * 32)
        self.assertEqual(caught.exception.code, "attempt_unresolved")
        self.assertEqual(permit["attempt"], self.attempt()["attempt_id"])

    def test_a_compatibility_block_is_not_bypassed_by_retry(self):
        permit = self.start()
        self.poll(report(seq=3, update=phase("blocked", "bootstrap_incompatible", permit)))
        with self.assertRaises(AdminRequestError) as caught:
            self.lifecycle.authorize_retry(DEVICE, self.body(), "f" * 32)
        self.assertEqual(caught.exception.code, "manual_action_required")

    def test_an_invalid_body_or_key_is_a_client_error(self):
        self.exhaust()
        for body, key in (({"state_revision": 2}, "f" * 32),
                          (self.body(), "short"),
                          (self.body(revision=LEGACY[:12]), "f" * 32)):
            with self.assertRaises(AdminRequestError) as caught:
                self.lifecycle.authorize_retry(DEVICE, body, key)
            self.assertEqual(caught.exception.status, 400)

    def test_recovery_queues_nothing_until_a_fresh_report_arrives(self):
        self.start()
        self.clock.advance(901)
        self.poll(report(seq=3))
        self.assertEqual(self.stored()["update_state"], "unconfirmed")
        queued = self.lifecycle.queue_recovery(DEVICE, self.body(), "e" * 32)
        self.assertTrue(queued["queued"])
        self.assertTrue(self.stored()["attention_latched"])
        self.assertTrue(self.lifecycle.device_view(DEVICE)["update"]["recovery_pending"])
        # The report that queued it cannot satisfy it: the tuple must rise.
        self.poll(report(seq=3))
        self.assertEqual(self.stored()["update_state"], "unconfirmed")
        done = self.poll(report(boot=2, seq=1, revision=TARGET), label=TARGET[:12])
        self.assertEqual(done["update"]["state"], "current")
        self.assertFalse(done["update"]["needs_attention"])
        stored = self.stored()
        self.assertEqual(stored["last_recovery_kind"], "admin_app_reconciled")
        self.assertIsNone(stored["recovery_request_id"])
        self.assertEqual(self.latest_attempt()["status"], "cancelled")

    def test_an_expired_recovery_clears_the_check_but_not_the_warning(self):
        self.start()
        self.clock.advance(901)
        self.poll(report(seq=3))
        self.lifecycle.queue_recovery(DEVICE, self.body(), "e" * 32)
        self.clock.advance(601)
        self.poll(report(seq=4))
        stored = self.stored()
        self.assertIsNone(stored["recovery_request_id"])
        self.assertTrue(stored["attention_latched"])
        self.assertEqual(stored["update_state"], "unconfirmed")


class DisabledTests(unittest.TestCase):

    def test_the_default_daemon_never_issues_a_command(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        storage = Storage(Path(temp.name) / "dash.db")
        self.addCleanup(storage.close)
        lifecycle = DeviceLifecycle(storage, expected_provider=lambda: bundle(),
                                    ota_enabled=False)
        seen = lifecycle.observe(headers(
            ("X-Presto-Client", "presto"), ("X-Presto-Revision", OTHER[:12]),
            ("X-Presto-Device-ID", DEVICE), ("X-Presto-Lifecycle", report())))
        self.assertIsNone(seen["command"])
        self.assertFalse(seen["update"]["ota_enabled"])
        self.assertEqual(storage.device_count(), 1)
        with self.assertRaises(AdminRequestError) as caught:
            lifecycle.authorize_retry(DEVICE, {"state_revision": 1,
                                               "target_scheme": "slot-v1",
                                               "target_revision": TARGET}, "f" * 32)
        self.assertEqual(caught.exception.code, "ota_not_enabled")


if __name__ == "__main__":
    unittest.main()
