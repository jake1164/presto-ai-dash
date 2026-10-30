"""The device's updater: what may execute, how it streams, and what it refuses.

Every rule here is a refusal. A manifest that disagrees with its own hashes, a
transport that is not verified TLS, a board clock still in 2021, a slot with no
room, or an unresolved activation all stop before a byte of code is stored.
"""

import json
import io
import tempfile
from pathlib import Path
from unittest.mock import patch
import unittest

import presto_path  # noqa: F401  (puts the device modules on the path)
import activation
import updater

NOW = 1800000000  # 2027-01-15, a usable calendar time


def digest(data):
    return updater._digest(data)


FILES = {"ai_dash_app.py": b"print('hello')\n", "ui.py": b"value = 1\n"}


def manifest(files=None, bootstrap=1, revision=None):
    files = FILES if files is None else files
    hashes = {name: digest(data) for name, data in files.items()}
    return {"revision": revision or updater.revision_of(hashes), "bootstrap": bootstrap,
            "files": [(name, len(files[name]), hashes[name]) for name in files],
            "total": sum(len(data) for data in files.values())}


def manifest_text(files=None, **kwargs):
    return updater.manifest_text(manifest(files, **kwargs))


class Transport(object):
    def __init__(self, payload=None, trusted=True, hostname="dash.lan", redirect=False):
        self.payload = FILES if payload is None else payload
        self.trusted = trusted
        self.hostname = hostname
        self.redirect = redirect
        self.fetched = []

    def stream(self, name, chunk):
        self.fetched.append(name)
        data = self.payload[name]
        for start in range(0, len(data), chunk):
            yield data[start:start + chunk]


class FakeFs(object):
    def __init__(self, files=None, free=8 * 1024 * 1024, fail_write=False):
        self.files = dict(files or {})
        self.free = free
        self.fail_write = fail_write
        self.opened = []

    def exists(self, path):
        return path in self.files

    def read(self, path):
        return self.files.get(path)

    def write(self, path, data):
        if self.fail_write:
            raise OSError("disk full")
        self.files[path] = data if isinstance(data, str) else bytes(data)

    def open(self, path, mode):
        fs = self
        if "r" in mode:
            return io.BytesIO(fs.files[path])
        if self.fail_write:
            raise OSError("disk full")
        fs.files[path] = b""

        class Handle(object):
            def write(self, chunk):
                fs.files[path] = fs.files.get(path, b"") + chunk

            def close(self):
                pass

        self.opened.append(path)
        return Handle()

    def replace(self, source, target):
        self.files[target] = self.files.pop(source)

    def remove(self, path):
        self.files.pop(path, None)

    def free_bytes(self):
        return self.free

    def listdir(self, folder):
        return [path.rsplit('/', 1)[1] for path in self.files if path.rsplit('/', 1)[0] == folder]

    def sync(self):
        pass


def journal(**overrides):
    value = activation.blank_journal()
    value.update(overrides)
    return value


class ManifestTests(unittest.TestCase):
    def test_canonical_hash_uses_only_the_micropython_json_api(self):
        hashes = {"ui.py": "a" * 64, "ai_dash_app.py": "b" * 64}
        wanted = digest(json.dumps(hashes, sort_keys=True, separators=(",", ":")).encode())
        with patch.object(updater.json, "dumps", side_effect=AssertionError("unsupported serializer")):
            self.assertEqual(updater.revision_of(hashes), wanted)

    def test_a_consistent_manifest_is_accepted(self):
        parsed = updater.parse_manifest(manifest_text())
        self.assertEqual(parsed["revision"], manifest()["revision"])
        self.assertEqual(parsed["total"], len(FILES["ai_dash_app.py"]) + len(FILES["ui.py"]))

    def test_a_revision_that_disagrees_with_the_files_is_refused(self):
        text = updater.manifest_text(manifest(revision="0" * 64))
        with self.assertRaisesRegex(updater.ManifestError, "hash_mismatch"):
            updater.parse_manifest(text)

    def test_a_non_canonical_or_generated_name_is_refused(self):
        for name in ("../boot.py", "sub/ui.py", "ui.pyc", "revision.txt"):
            files = {"ai_dash_app.py": b"x", name: b"y"}
            text = updater.manifest_text(manifest(files))
            with self.assertRaises(updater.ManifestError, msg=name):
                updater.parse_manifest(text)

    def test_a_duplicate_file_is_refused(self):
        entry = {"path": "ui.py", "size": 1, "sha256": "0" * 64}
        text = json.dumps({"v": 1, "scheme": "slot-v1", "revision": "0" * 64,
                           "bootstrap": 1, "files": [entry, entry]})
        with self.assertRaises(updater.ManifestError):
            updater.parse_manifest(text)

    def test_oversized_and_malformed_input_is_refused(self):
        for text in ("", "{", "[]", "x" * 9000):
            with self.assertRaises(updater.ManifestError):
                updater.parse_manifest(text)

    def test_a_manifest_over_the_file_limit_is_refused(self):
        text = json.dumps({"v": 1, "scheme": "slot-v1", "revision": "0" * 64,
                           "bootstrap": 1,
                           "files": [{"path": "f%d.py" % i, "size": 1,
                                      "sha256": "0" * 64} for i in range(41)]})
        with self.assertRaises(updater.ManifestError):
            updater.parse_manifest(text)


class TrustTests(unittest.TestCase):

    def test_an_untrusted_transport_or_missing_hostname_is_refused(self):
        self.assertEqual(updater.trust_error(Transport(trusted=False), lambda: NOW),
                         "transport_untrusted")
        self.assertEqual(updater.trust_error(Transport(hostname=""), lambda: NOW),
                         "transport_untrusted")
        self.assertIsNone(updater.trust_error(Transport(), lambda: NOW))

    def test_a_board_clock_still_in_the_past_fails_closed(self):
        self.assertEqual(updater.trust_error(Transport(), lambda: 1600000000),
                         "transport_untrusted")
        self.assertEqual(updater.trust_error(Transport(), lambda: "now"),
                         "transport_untrusted")


class PreflightTests(unittest.TestCase):

    def check(self, **overrides):
        values = {"manifest": manifest(), "journal": journal(),
                  "firmware": {"sha256": True, "rename": True, "statvfs": True, "watchdog": True},
                  "free_bytes": 8 * 1024 * 1024, "ota_enabled": True}
        values.update(overrides)
        return updater.preflight(**values)

    def test_a_ready_device_may_start(self):
        self.assertIsNone(self.check())

    def test_disabled_or_unmeasured_firmware_blocks(self):
        self.assertEqual(self.check(ota_enabled=False), "firmware_unsupported")
        self.assertEqual(self.check(firmware={}), "firmware_unsupported")
        self.assertEqual(self.check(firmware={"sha256": True, "rename": True}),
                         "firmware_unsupported")

    def test_nearly_full_flash_blocks_and_changes_nothing(self):
        self.assertEqual(self.check(free_bytes=updater.SAFETY_MARGIN_BYTES),
                         "storage_insufficient")

    def test_a_bootstrap_that_cannot_run_the_target_blocks(self):
        self.assertEqual(self.check(journal=journal(bootstrap=0)),
                         "bootstrap_incompatible")
        self.assertEqual(self.check(manifest=manifest(bootstrap=2)),
                         "bootstrap_incompatible")

    def test_an_unresolved_activation_must_be_reconciled_first(self):
        pending = {"slot": "b", "target": "b" * 64, "generation": None,
                   "attempt": None, "command": None, "tries": 0}
        self.assertEqual(self.check(journal=journal(pending=pending)),
                         "local_state_invalid")


class TransferTests(unittest.TestCase):
    def test_real_filesystem_adapter_and_generated_metadata(self):
        with tempfile.TemporaryDirectory() as folder:
            fs = activation.FileStore()
            value = self.transfer(fs)
            value.folder = folder
            Path(folder, "obsolete.mpy").write_bytes(b"old executable")
            self.assertIsNone(self.run_to_end(value))
            self.assertFalse(Path(folder, "obsolete.mpy").exists())
            self.assertEqual(Path(folder, "revision.sha256").read_text().strip(), value.manifest["revision"])
            self.assertEqual(Path(folder, "ai_dash_app.py").read_bytes(), FILES["ai_dash_app.py"])

    def test_metadata_failure_never_completes_a_candidate(self):
        fs = FakeFs()
        value = self.transfer(fs)
        write = fs.write
        def fail_metadata(path, data):
            if "revision" in path:
                raise OSError("full")
            write(path, data)
        with patch.object(fs, "write", side_effect=fail_metadata):
            self.assertEqual(self.run_to_end(value), "storage_insufficient")

    def transfer(self, fs=None, transport=None, payload=None):
        files = FILES if payload is None else payload
        parsed = updater.parse_manifest(updater.manifest_text(manifest(files)))
        return updater.Transfer(fs or FakeFs(), transport or Transport(files), parsed, "b")

    def run_to_end(self, transfer):
        steps = 0
        while not transfer.done and steps < 1000:
            if transfer.flash_due:
                transfer.flash()
            else:
                transfer.step()
            steps += 1
        return transfer.error

    def test_a_whole_transfer_verifies_and_moves_each_file_into_place(self):
        fs = FakeFs()
        transfer = self.transfer(fs)
        self.assertIsNone(self.run_to_end(transfer))
        for name, data in FILES.items():
            self.assertEqual(fs.files["/ai_dash_slots/b/" + name], data)
            self.assertNotIn("/ai_dash_slots/b/" + name + ".presto-new", fs.files)

    def test_the_transfer_is_chunked_so_the_ui_can_be_served_between_steps(self):
        fs = FakeFs()
        payload = {"ai_dash_app.py": b"x" * 4096}
        transfer = self.transfer(fs, Transport(payload=payload), payload=payload)
        steps = 0
        sizes = []
        while not transfer.flash_due:
            before = transfer.metrics["bytes"]
            transfer.step()
            if transfer.metrics["bytes"] > before:
                sizes.append(transfer.metrics["bytes"] - before)
            steps += 1
            self.assertLess(steps, 100)
        self.assertIsNone(transfer.error)
        self.assertGreater(steps, 1)
        self.assertTrue(all(size <= updater.CHUNK_BYTES for size in sizes))

    def test_downloading_never_writes_flash_until_flash_is_called(self):
        # On the measured firmware a flash write hangs while the display
        # runs, so the app pauses the display only around flash().
        fs = FakeFs()
        transfer = self.transfer(fs)
        while not transfer.flash_due:
            transfer.step()
        self.assertTrue(transfer.downloaded)
        self.assertEqual(fs.files, {})
        self.assertEqual(fs.opened, [])
        fed = []
        self.assertIsNone(transfer.flash(lambda: fed.append(1)))
        self.assertTrue(transfer.done)
        self.assertTrue(fed)
        self.assertEqual(transfer.metrics["flash_windows"], 1)
        for name, data in FILES.items():
            self.assertEqual(fs.files["/ai_dash_slots/b/" + name], data)

    def test_verified_files_are_committed_in_bounded_batches(self):
        fs = FakeFs()
        payload = {"ai_dash_app.py": b"a" * 3000, "ui.py": b"b" * 3000, "net.py": b"c" * 3000}
        transfer = self.transfer(fs, Transport(payload=payload), payload=payload)
        with patch.object(updater, "FLASH_BATCH_BYTES", 5000):
            while not transfer.flash_due:
                transfer.step()
            # Two files are waiting, the third is not downloaded yet.
            self.assertFalse(transfer.downloaded)
            self.assertIsNone(transfer.flash())
            self.assertFalse(transfer.done)
            self.assertEqual(len([path for path in fs.files if path.endswith(".py")]), 2)
            self.assertIsNone(self.run_to_end(transfer))
        self.assertEqual(transfer.metrics["flash_windows"], 2)
        for name, data in payload.items():
            self.assertEqual(fs.files["/ai_dash_slots/b/" + name], data)

    def test_a_corrupt_file_is_refused_before_anything_reaches_flash(self):
        fs = FakeFs()
        transport = Transport(payload={"ai_dash_app.py": b"print('HELLO')\n"})
        transfer = self.transfer(fs, transport,
                                 payload={"ai_dash_app.py": FILES["ai_dash_app.py"]})
        while not transfer.done and not transfer.flash_due:
            transfer.step()
        self.assertEqual(transfer.error, "hash_mismatch")
        self.assertEqual(fs.opened, [])

    def test_corrupt_bytes_are_refused_and_never_become_a_live_file(self):
        fs = FakeFs()
        # The same length, different bytes: only the hash can tell.
        transport = Transport(payload={"ai_dash_app.py": b"print('HELLO')\n"})
        transfer = self.transfer(fs, transport,
                                 payload={"ai_dash_app.py": FILES["ai_dash_app.py"]})
        self.assertEqual(self.run_to_end(transfer), "hash_mismatch")
        self.assertNotIn("/ai_dash_slots/b/ai_dash_app.py", fs.files)

    def test_a_truncated_body_is_refused_before_completion(self):
        class Short(Transport):
            def stream(self, name, chunk):
                yield self.payload[name][:-2]
        fs = FakeFs()
        transfer = self.transfer(fs, Short())
        self.assertEqual(self.run_to_end(transfer), "transfer_timeout")
        self.assertNotIn("/ai_dash_slots/b/ui.py", fs.files)

    def test_a_body_longer_than_declared_is_refused(self):
        class Long(Transport):
            def stream(self, name, chunk):
                yield self.payload[name] + b"extra"
        fs = FakeFs()
        transfer = self.transfer(fs, Long())
        self.assertEqual(self.run_to_end(transfer), "manifest_invalid")

    def test_a_storage_failure_is_reported_not_raised(self):
        transfer = self.transfer(FakeFs(fail_write=True))
        self.assertEqual(self.run_to_end(transfer), "storage_insufficient")


class FactoryTests(unittest.TestCase):
    def test_no_updater_without_a_trusted_transport_or_a_usable_clock(self):
        firmware = {"sha256": True, "rename": True, "statvfs": True, "watchdog": True}
        self.assertIsNone(updater.make_updater(FakeFs(), Transport(trusted=False),
                                              lambda: NOW, firmware, journal()))
        self.assertIsNone(updater.make_updater(FakeFs(), Transport(),
                                              lambda: 1600000000, firmware, journal()))

    def test_no_updater_without_the_measured_firmware_capabilities(self):
        self.assertIsNone(updater.make_updater(FakeFs(), Transport(), lambda: NOW,
                                               {}, journal()))
        self.assertIsNone(updater.make_updater(FakeFs(), Transport(), lambda: NOW,
                                               {"sha256": True}, journal()))

    def test_every_prerequisite_present_builds_the_updater(self):
        built = updater.make_updater(
            FakeFs(), Transport(), lambda: NOW,
            {"sha256": True, "rename": True, "statvfs": True, "watchdog": True}, journal())
        self.assertIsInstance(built, updater.Updater)
        self.assertTrue(built.enabled)


class AcceptTests(unittest.TestCase):

    def command(self, revision=None, **overrides):
        value = {"id": "3" * 32, "kind": "start_update", "generation": "1" * 32,
                 "attempt": "2" * 32, "attempt_no": 1, "target_scheme": "slot-v1",
                 "target_revision": revision or manifest()["revision"],
                 "manifest_path": "/api/v1/device-app/manifest"}
        value.update(overrides)
        return value

    def build(self, enabled=True, journal_value=None):
        self.resets = []
        return updater.Updater(FakeFs(), Transport(), lambda: NOW,
                               firmware={"sha256": True, "rename": True, "statvfs": True, "watchdog": True},
                               reset=lambda: self.resets.append(1), enabled=enabled,
                               journal=journal_value or journal())

    def test_a_disabled_updater_refuses_every_permit(self):
        instance = self.build(enabled=False)
        journal_value, error = instance.accept(self.command(), manifest_text(), "a")
        self.assertEqual(error, "firmware_unsupported")
        self.assertIsNone(journal_value["pending"])

    def test_a_permit_for_another_target_is_refused(self):
        instance = self.build()
        _journal, error = instance.accept(self.command(revision="9" * 64),
                                          manifest_text(), "a")
        self.assertEqual(error, "manifest_invalid")

    def test_a_valid_permit_stages_the_opposite_slot_and_activates_it(self):
        instance = self.build()
        journal_value, error = instance.accept(self.command(), manifest_text(), "a")
        self.assertIsNone(error)
        pending = journal_value["pending"]
        self.assertEqual(pending["slot"], "b")
        self.assertEqual(pending["attempt"], "2" * 32)
        self.assertEqual(self.resets, [1])

    def test_an_untrusted_transport_stops_before_anything_is_written(self):
        instance = self.build()
        instance.transport = Transport(trusted=False)
        _journal, error = instance.accept(self.command(), manifest_text(), "a")
        self.assertEqual(error, "transport_untrusted")
        self.assertEqual(self.resets, [])


if __name__ == "__main__":
    unittest.main()
