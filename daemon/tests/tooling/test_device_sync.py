"""One app inventory, real file hashes, safe USB writes and drift reporting."""

import builtins
from contextlib import redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import presto_path

TOOLS = Path(presto_path.REPO_ROOT) / "tools"
if str(TOOLS) not in sys.path:
    sys.path.append(str(TOOLS))
import presto_bundle as bundle
import presto_device as device


class BundleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        app = self.root / "presto"
        (app / "launcher").mkdir(parents=True)
        for name in ("main.py", "ui.py", "home.py", "config.py", "config.example.py", "secrets.py"):
            (app / name).write_text("value = 1\n", encoding="utf-8")
        (app / "launcher" / "ai_dash.py").write_text("value = 2\n", encoding="utf-8")

    def test_inventory_excludes_private_files_and_automatically_adds_modules(self):
        first = bundle.snapshot(self.root)
        self.assertEqual(set(first["files"]), {"/ai_dash.py", "/ai_dash_files/ai_dash_app.py",
                                             "/ai_dash_files/ui.py", "/ai_dash_files/home.py",
                                             bundle.REVISION_TARGET})
        # A new helper must not require updating a second copy or deployment list.
        (self.root / "presto" / "new_helper.py").write_text("value = 3\n", encoding="utf-8")
        changed = bundle.snapshot(self.root)
        self.assertIn("/ai_dash_files/new_helper.py", changed["files"])
        self.assertNotEqual(changed["revision"], first["revision"])
        self.assertNotIn("/main.py", changed["files"])

    def test_copies_and_backups_of_private_files_are_never_uploaded(self):
        before = bundle.snapshot(self.root)
        for name in ("secrets_old.py", "my_secrets.py", "config-backup.py",
                     "config_old.py", "Secrets.PY", "SECRETS.py", "ConfigCopy.py"):
            (self.root / "presto" / name).write_text("token = 'private'", encoding="utf-8")
        after = bundle.snapshot(self.root)
        self.assertEqual(set(after["files"]), set(before["files"]))
        self.assertEqual(after["revision"], before["revision"])
        for name, data in after["files"].items():
            self.assertNotIn(b"private", data, name)

    def test_only_plain_module_names_are_uploaded(self):
        before = bundle.snapshot(self.root)
        for name in ("helper.py.bak", "notes.txt", "my helper.py", "edit~.py", ".hidden.py",
                     "new-helper.py"):
            (self.root / "presto" / name).write_text("value = 5", encoding="utf-8")
        self.assertEqual(set(bundle.snapshot(self.root)["files"]), set(before["files"]))

    def test_the_real_app_inventory_is_what_the_device_runs(self):
        files = set(bundle.snapshot()["files"])
        for name in ("main", "ui", "home", "cards", "net", "gestures", "popup", "alerts", "ticks"):
            target = "/ai_dash_files/ai_dash_app.py" if name == "main" else "/ai_dash_files/%s.py" % name
            self.assertIn(target, files)
        self.assertFalse([target for target in files
                          if "secret" in target.lower() or "config" in target.lower()])

    def test_the_revision_file_carries_the_revision_and_is_not_part_of_it(self):
        current = bundle.snapshot(self.root)
        written = current["files"][bundle.REVISION_TARGET].decode("ascii").strip()
        self.assertEqual(written, current["revision"][:bundle.REVISION_CHARS])
        without = {target: digest for target, digest in current["hashes"].items()
                   if target != bundle.REVISION_TARGET}
        self.assertEqual(bundle.revision(without), current["revision"])

    def test_private_settings_do_not_affect_code_revision(self):
        original = bundle.snapshot(self.root)["revision"]
        for name in ("config.py", "secrets.py", "config.example.py"):
            (self.root / "presto" / name).write_text("this is deliberately invalid Python", encoding="utf-8")
        self.assertEqual(bundle.snapshot(self.root)["revision"], original)

    def test_source_change_invalidates_previous_usb_receipt(self):
        current = bundle.snapshot(self.root)
        receipt = self.root / "receipt.json"
        device.record("matched", current, receipt)
        self.assertEqual(bundle.sync_status(current["revision"], receipt)["status"], "matched")
        (self.root / "presto" / "home.py").write_text("value = 4\n", encoding="utf-8")
        self.assertEqual(bundle.sync_status(bundle.snapshot(self.root)["revision"], receipt)["status"],
                         "source_changed")
        receipt.write_text("broken", encoding="utf-8")
        self.assertEqual(bundle.sync_status(current["revision"], receipt)["status"], "not_checked")

    def test_invalid_source_cannot_be_deployed(self):
        (self.root / "presto" / "home.py").write_text("this is invalid Python", encoding="utf-8")
        with self.assertRaises(SyntaxError):
            bundle.snapshot(self.root)


class RemoteFilesystem:
    """Execute the actual MicroPython scripts against an in-memory filesystem."""
    def __init__(self, files, fail_target=None):
        self.files = dict(files)
        self.fail_target = fail_target
        self.opened = []

    def stat(self, path):
        if path not in self.files:
            raise OSError("missing")
        return (0,) * 10

    def open(self, path, mode):
        self.opened.append(path)
        if path not in self.files:
            raise OSError("missing")
        return io.BytesIO(self.files[path])

    def rename(self, before, after):
        if after == self.fail_target:
            self.fail_target = None
            raise OSError("injected rename failure")
        self.files[after] = self.files.pop(before)

    def execute(self, script):
        real_import = builtins.__import__
        fake_os = SimpleNamespace(stat=self.stat, rename=self.rename, remove=self.files.pop,
                                  listdir=lambda folder: [p.rsplit("/", 1)[1] for p in self.files
                                                          if p.rsplit("/", 1)[0] == folder],
                                  uname=lambda: SimpleNamespace(machine="Pimoroni Presto with RP2350"))
        fake_sys = SimpleNamespace(implementation=SimpleNamespace(name="micropython"))
        def importing(name, *args, **kwargs):
            return {"os": fake_os, "sys": fake_sys}.get(name) or real_import(name, *args, **kwargs)
        namespace = {"__builtins__": {**vars(builtins), "open": self.open, "__import__": importing}}
        output = io.StringIO()
        with redirect_stdout(output):
            exec(script, namespace)
        return output.getvalue()


class DeviceSyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.receipt = Path(self.temp.name) / "receipt.json"
        files = {"/ai_dash_files/home.py": b"new home", "/ai_dash.py": b"new launcher"}
        hashes = {p: hashlib.sha256(data).hexdigest() for p, data in files.items()}
        self.bundle = {"files": files, "hashes": hashes, "revision": bundle.revision(hashes)}
        self.original = {p: b"old code" for p in files}

    def staged_files(self):
        return {**self.original, **{p + ".presto-new": data for p, data in self.bundle["files"].items()},
                "/ai_dash_files/config.py": b"private", "/secrets.py": b"private", "/main.py": b"firmware"}

    def test_stages_and_verifies_whole_bundle_then_preserves_private_and_firmware_files(self):
        fs = RemoteFilesystem(self.staged_files())
        output = fs.execute(device.commit_script(self.bundle["hashes"]))
        self.assertEqual(device.parse_hashes(output, self.bundle["hashes"]), self.bundle["hashes"])
        for path, data in self.bundle["files"].items():
            self.assertEqual(fs.files[path], data)
        for protected in ("/ai_dash_files/config.py", "/secrets.py", "/main.py"):
            self.assertNotIn(protected, fs.opened)
            self.assertEqual(fs.files[protected], self.staged_files()[protected])
        self.assertFalse(any(p.endswith((".presto-prev", ".presto-new")) for p in fs.files))

    def test_failed_stage_hash_does_not_replace_any_installed_file(self):
        fs = RemoteFilesystem(self.staged_files())
        fs.files["/ai_dash.py.presto-new"] = b"truncated upload"
        with self.assertRaisesRegex(RuntimeError, "Staged app verification"):
            fs.execute(device.commit_script(self.bundle["hashes"]))
        for path, old in self.original.items():
            self.assertEqual(fs.files[path], old)

    def test_deleted_helpers_are_detected_and_retired_without_touching_private_files(self):
        fs = RemoteFilesystem({**self.staged_files(), "/ai_dash_files/old_helper.py": b"old helper"})
        actual = device.parse_hashes(fs.execute(device.inspection_script(self.bundle["hashes"])),
                                     self.bundle["hashes"])
        self.assertIn("/ai_dash_files/old_helper.py", actual)
        self.assertNotIn("/ai_dash_files/old_helper.py", fs.opened)
        fs.execute(device.commit_script(self.bundle["hashes"]))
        self.assertNotIn("/ai_dash_files/old_helper.py", fs.files)
        self.assertEqual(fs.files["/ai_dash_files/old_helper.py.presto-prev"], b"old helper")
        self.assertIn("/ai_dash_files/config.py", fs.files)

    def test_verification_refuses_private_or_out_of_app_paths(self):
        for extra in ("/main.py", "/secrets.py", "/ai_dash_files/config.py", "/ai_dash_files/../boot.py"):
            with self.subTest(extra=extra), self.assertRaises(RuntimeError):
                device.parse_hashes(device.MARKER + json.dumps({**self.bundle["hashes"], extra: None}),
                                    self.bundle["hashes"])

    def test_failed_rename_rolls_back_all_changed_files(self):
        fs = RemoteFilesystem(self.staged_files(), fail_target="/ai_dash.py")
        with self.assertRaisesRegex(OSError, "injected"):
            fs.execute(device.commit_script(self.bundle["hashes"]))
        for path, old in self.original.items():
            self.assertEqual(fs.files[path], old)

    def test_check_hashes_actual_files_instead_of_trusting_a_version_label(self):
        fs = RemoteFilesystem({**self.bundle["files"], "/ai_dash.py": b"manually changed"})
        actual = device.parse_hashes(fs.execute(device.inspection_script(self.bundle["hashes"])),
                                     self.bundle["hashes"])
        self.assertNotEqual(actual, self.bundle["hashes"])
        self.assertEqual(fs.opened, list(self.bundle["hashes"]))

    def test_sync_uploads_frozen_bytes_and_records_only_verified_result(self):
        calls = []
        def remote(port, arguments):
            self.assertEqual(port, "TESTPORT")
            calls.append(arguments)
            if "run" in arguments:
                uploads = [i for i, value in enumerate(arguments) if value == "cp"]
                self.assertEqual(len(uploads), len(self.bundle["files"]))
                for index in uploads:
                    target = arguments[index + 2][1:].removesuffix(".presto-new")
                    self.assertEqual(Path(arguments[index + 1]).read_bytes(), self.bundle["files"][target])
                return device.MARKER + json.dumps(self.bundle["hashes"])
            return device.MARKER + json.dumps({"config": True})
        self.assertEqual(device.operate("sync", "TESTPORT", self.bundle, remote, self.receipt), [])
        self.assertEqual(bundle.sync_status(self.bundle["revision"], self.receipt)["status"], "matched")
        self.assertIn("--no-follow", calls[-1])

    def test_sync_refuses_a_device_that_has_no_settings_and_writes_nothing(self):
        calls = []
        def remote(port, arguments):
            calls.append(arguments)
            return device.MARKER + json.dumps({"config": False})
        with self.assertRaisesRegex(RuntimeError, "Set up a Presto"):
            device.operate("sync", "TESTPORT", self.bundle, remote, self.receipt)
        self.assertEqual([arguments for arguments in calls if "run" in arguments], [])
        self.assertFalse(any("cp" in arguments for arguments in calls))
        self.assertEqual(bundle.sync_status(self.bundle["revision"], self.receipt)["status"], "unverified")

    def test_a_failed_settings_preflight_still_relaunches_the_app(self):
        calls = []
        def remote(port, arguments):
            calls.append(arguments)
            return ""  # no marker: the device's answer cannot be read
        with self.assertRaisesRegex(RuntimeError, "unambiguous"):
            device.operate("sync", "TESTPORT", self.bundle, remote, self.receipt)
        self.assertFalse(any("cp" in arguments for arguments in calls))
        self.assertIn("--no-follow", calls[-1])

    def test_failed_sync_does_not_record_a_success_and_attempts_app_relaunch(self):
        calls = []
        def remote(port, arguments):
            calls.append(arguments)
            if "run" in arguments:
                raise RuntimeError("connection interrupted")
            return device.MARKER + json.dumps({"config": True})
        with self.assertRaisesRegex(RuntimeError, "interrupted"):
            device.operate("sync", "TESTPORT", self.bundle, remote, self.receipt)
        self.assertEqual(bundle.sync_status(self.bundle["revision"], self.receipt)["status"], "unverified")
        self.assertIn("--no-follow", calls[-1])

    def test_check_reports_drift_and_never_uploads_files(self):
        def remote(port, arguments):
            self.assertNotIn("cp", arguments)
            return device.MARKER + json.dumps({p: None for p in self.bundle["hashes"]}) if "run" in arguments else ""
        result = device.operate("check", "TESTPORT", self.bundle, remote, self.receipt)
        self.assertEqual(result, list(self.bundle["files"]))
        self.assertEqual(bundle.sync_status(self.bundle["revision"], self.receipt)["status"], "different")

    def test_relaunch_failure_is_reported_instead_of_claiming_running_app(self):
        def remote(port, arguments):
            if "--no-follow" in arguments:
                raise RuntimeError("disconnected")
            return device.MARKER + json.dumps(self.bundle["hashes"])
        with self.assertRaisesRegex(RuntimeError, "Could not relaunch"):
            device.operate("check", "TESTPORT", self.bundle, remote, self.receipt)

    def test_concurrent_usb_commands_are_rejected_and_lock_is_released(self):
        path = self.receipt.with_suffix(".lock")
        with device.device_lock(path):
            with self.assertRaisesRegex(RuntimeError, "Another Presto"):
                with device.device_lock(path):
                    self.fail("concurrent USB operation was allowed")
        with device.device_lock(path):
            pass


if __name__ == "__main__":
    unittest.main()
