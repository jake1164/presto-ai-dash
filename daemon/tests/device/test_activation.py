"""The durable activation journal: every read, write and recovery boundary.

The journal is the only thing standing between a power cut during an update
and a device that boots nothing, so these tests drive the writer's three
intermediate states (temporary file, backup, primary) directly, plus the
candidate-entry counter that decides when a slot is rolled back.
"""

import json
import unittest

import presto_path  # noqa: F401  (puts the device modules on the path)
import activation

SLOT_A = "a" * 64
SLOT_B = "b" * 64
GEN = "1" * 32
ATTEMPT = "2" * 32
COMMAND = "3" * 32


class MemoryStore(object):
    """The journal's four calls, with an injectable failure point."""

    def __init__(self, files=None, fail_after=None):
        self.files = dict(files or {})
        self.ops = []
        self.fail_after = fail_after

    def _step(self, name):
        if self.fail_after is not None and len(self.ops) >= self.fail_after:
            raise OSError("simulated power loss")
        self.ops.append(name)

    def exists(self, path):
        return path in self.files

    def read(self, path):
        return self.files.get(path)

    def write(self, path, text):
        self._step("write:" + path)
        self.files[path] = text

    def replace(self, source, target):
        self._step("replace:%s>%s" % (source, target))
        if source not in self.files:
            raise OSError("missing")
        self.files[target] = self.files.pop(source)

    def remove(self, path):
        self.files.pop(path, None)

    def sync(self):
        self._step("sync")


def journal(**overrides):
    value = activation.blank_journal()
    value.update(overrides)
    return value


class ParseTests(unittest.TestCase):

    def test_a_blank_journal_round_trips(self):
        text = activation.encode_journal(activation.blank_journal())
        self.assertEqual(activation.parse_journal(text), activation.blank_journal())

    def test_unknown_keys_are_refused_whole(self):
        text = json.dumps(dict(json.loads(activation.encode_journal(journal())),
                               extra=1))
        self.assertIsNone(activation.parse_journal(text))

    def test_a_newer_bootstrap_journal_is_not_interpreted(self):
        value = journal(bootstrap=activation.BOOTSTRAP_VERSION + 1)
        self.assertIsNone(activation.parse_journal(activation.encode_journal(value)))

    def test_corrupt_or_truncated_json_is_absent_not_half_trusted(self):
        for text in ("", "{", '{"v":1}', None, 5):
            self.assertIsNone(activation.parse_journal(text))

    def test_an_invalid_pending_activation_is_refused(self):
        for pending in ({"slot": "c", "target": SLOT_B, "generation": None,
                         "attempt": None, "command": None, "tries": 0},
                        {"slot": "b", "target": "short", "generation": None,
                         "attempt": None, "command": None, "tries": 0},
                        {"slot": "b", "target": SLOT_B, "generation": None,
                         "attempt": None, "command": None, "tries": 99}):
            value = journal(pending=pending)
            self.assertIsNone(activation.parse_journal(json.dumps(value)), pending)

    def test_a_pending_activation_with_a_permit_round_trips(self):
        value = journal(pending={"slot": "b", "target": SLOT_B, "generation": GEN,
                                 "attempt": ATTEMPT, "command": COMMAND, "tries": 1})
        self.assertEqual(activation.parse_journal(activation.encode_journal(value)), value)


class RecoveryTests(unittest.TestCase):
    """Every rename boundary the writer can be interrupted at."""

    def test_a_clean_write_is_read_back(self):
        store = MemoryStore()
        saved = journal(boots=4)
        activation.save(store, saved)
        self.assertEqual(activation.load(store), (saved, "primary"))

    def test_a_cut_before_any_rename_leaves_the_previous_journal(self):
        store = MemoryStore()
        activation.save(store, journal(boots=3))
        before = dict(store.files)
        cutting = MemoryStore(before, fail_after=0)  # fails on the first write
        with self.assertRaises(OSError):
            activation.save(cutting, journal(boots=4))
        self.assertEqual(activation.load(MemoryStore(before))[0]["boots"], 3)

    def test_a_cut_between_the_two_renames_recovers_the_backup(self):
        store = MemoryStore()
        activation.save(store, journal(boots=3))
        # First rename moves the current journal to .bak, then the power goes.
        cutting = MemoryStore(dict(store.files), fail_after=2)
        with self.assertRaises(OSError):
            activation.save(cutting, journal(boots=4))
        self.assertNotIn(activation.JOURNAL_PATH, cutting.files)
        recovered, source = activation.load(cutting)
        self.assertEqual(source, "backup")
        self.assertEqual(recovered["boots"], 3)

    def test_a_cut_after_the_primary_rename_keeps_the_new_journal(self):
        store = MemoryStore()
        activation.save(store, journal(boots=3))
        cutting = MemoryStore(dict(store.files), fail_after=3)
        with self.assertRaises(OSError):
            activation.save(cutting, journal(boots=4))
        recovered, source = activation.load(cutting)
        self.assertEqual((recovered["boots"], source), (4, "primary"))

    def test_a_truncated_primary_falls_back_to_the_backup(self):
        store = MemoryStore()
        activation.save(store, journal(boots=2))
        activation.save(store, journal(boots=3))
        store.files[activation.JOURNAL_PATH] = '{"v":1,"boo'
        recovered, source = activation.load(store)
        self.assertEqual((recovered["boots"], source), (2, "backup"))

    def test_no_journal_at_all_requires_explicit_provisioning(self):
        recovered, source = activation.load(MemoryStore())
        self.assertEqual(source, "fresh")
        self.assertIsNone(recovered)
        self.assertEqual(activation.on_boot(recovered, source)[1]["action"], "recovery")

    def test_a_journal_that_cannot_be_read_is_reported_as_corrupt(self):
        store = MemoryStore({activation.JOURNAL_PATH: "not json"})
        recovered, source = activation.load(store)
        self.assertEqual(source, "corrupt")
        self.assertIsNone(recovered)
        self.assertEqual(activation.on_boot(recovered, source)[1]["action"], "recovery")


class BootTests(unittest.TestCase):

    def test_a_confirmed_slot_increments_the_boot_count_every_start(self):
        first, decision = activation.on_boot(journal(boots=7))
        self.assertEqual(first["boots"], 8)
        self.assertEqual((decision["slot"], decision["candidate"]), ("a", False))
        self.assertTrue(decision["boot_confirmed"])

    def test_a_candidate_gets_one_entry_then_rolls_back(self):
        pending = {"slot": "b", "target": SLOT_B, "generation": None,
                   "attempt": None, "command": None, "tries": 0}
        first, decision = activation.on_boot(journal(pending=pending))
        self.assertEqual((decision["slot"], decision["candidate"]), ("b", True))
        self.assertFalse(decision["boot_confirmed"])
        self.assertEqual(first["pending"]["tries"], 1)
        second, decision = activation.on_boot(first)
        self.assertEqual(second["boots"], first["boots"] + 1)
        self.assertEqual(decision["slot"], first["confirmed"])
        self.assertTrue(decision["rolled_back"])
        self.assertIsNone(second["pending"])
        self.assertEqual(second["failure"]["phase"], "rolled_back")
        self.assertEqual(second["failure"]["error"], "boot_crash")

    def test_a_confirmed_candidate_is_not_rolled_back_on_the_next_boot(self):
        pending = {"slot": "b", "target": SLOT_B, "generation": None,
                   "attempt": None, "command": None, "tries": 0}
        entered, decision = activation.on_boot(journal(pending=pending))
        self.assertTrue(decision["candidate"])
        confirmed = activation.confirm(entered, SLOT_B)
        again, decision = activation.on_boot(confirmed)
        self.assertEqual((decision["slot"], decision["candidate"]), ("b", False))
        self.assertTrue(decision["boot_confirmed"])

    def test_the_context_reports_an_unconfirmed_candidate(self):
        pending = {"slot": "b", "target": SLOT_B, "generation": None,
                   "attempt": None, "command": None, "tries": 0}
        entered, _decision = activation.on_boot(journal(pending=pending))
        context = activation.context(entered)
        self.assertEqual((context["slot"], context["boot_confirmed"]), ("b", False))
        self.assertEqual(context["target"], SLOT_B)

    def test_confirming_a_different_target_is_refused(self):
        pending = {"slot": "b", "target": SLOT_B, "generation": GEN,
                   "attempt": ATTEMPT, "command": COMMAND, "tries": 1}
        with self.assertRaises(ValueError):
            activation.confirm(journal(pending=pending), SLOT_A)


class UpdateBlockTests(unittest.TestCase):
    def test_reset_during_transfer_reports_the_same_failed_attempt(self):
        accepted = activation.begin_transfer(journal(), "b", SLOT_B, GEN, ATTEMPT, COMMAND)
        restored = activation.parse_journal(activation.encode_journal(accepted))
        started, decision = activation.on_boot(restored)
        self.assertEqual(decision["slot"], "a")
        block = activation.update_block(started)
        self.assertEqual((block["phase"], block["error"], block["attempt"]),
                         ("failed", "connection_lost", ATTEMPT))

    def test_a_deviceless_journal_reports_idle(self):
        self.assertEqual(activation.update_block(journal())["phase"], "idle")

    def test_a_local_activation_reports_idle_because_it_has_no_permit(self):
        """A USB activation has no generation, so a phase would be unparseable."""
        pending = {"slot": "b", "target": SLOT_B, "generation": None,
                   "attempt": None, "command": None, "tries": 1}
        self.assertEqual(activation.update_block(journal(pending=pending))["phase"],
                         "idle")

    def test_a_daemon_attempt_reports_confirming(self):
        pending = {"slot": "b", "target": SLOT_B, "generation": GEN,
                   "attempt": ATTEMPT, "command": COMMAND, "tries": 1}
        block = activation.update_block(journal(pending=pending))
        self.assertEqual(block["phase"], "confirming")
        self.assertEqual((block["generation"], block["attempt"]), (GEN, ATTEMPT))

    def test_a_retained_failure_keeps_reporting_until_cleared(self):
        value = activation.record_failure(journal(), "failed", "transfer_timeout",
                                          SLOT_B, GEN, ATTEMPT)
        self.assertEqual(activation.update_block(value)["phase"], "failed")
        cleaned = activation.clear_failure(value)
        self.assertEqual(activation.update_block(cleaned)["phase"], "idle")

    def test_a_confirmed_daemon_attempt_reports_success_after_a_reboot(self):
        pending = {"slot": "b", "target": SLOT_B, "generation": GEN,
                   "attempt": ATTEMPT, "command": COMMAND, "tries": 1}
        confirmed = activation.confirm(journal(pending=pending), SLOT_B)
        self.assertEqual(activation.update_block(confirmed)["phase"], "succeeded")
        self.assertEqual(activation.parse_journal(
            activation.encode_journal(confirmed)), confirmed)


if __name__ == "__main__":
    unittest.main()
