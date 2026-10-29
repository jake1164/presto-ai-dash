"""The slot layout, the bootstrap, and the download manifest agree end to end.

One inventory feeds the release wheel, the USB tool, the daemon's served
bundle and the device's manifest parser. These tests check that the four agree
byte for byte, that a legacy device is never compared with a slot hash, and
that a migration stages everything before it retires anything.
"""

import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest

import presto_path

TOOLS = Path(presto_path.REPO_ROOT) / "tools"
if str(TOOLS) not in sys.path:
    sys.path.append(str(TOOLS))
SRC = Path(presto_path.REPO_ROOT) / "daemon" / "src"
if str(SRC) not in sys.path:
    sys.path.append(str(SRC))

import presto_bundle as bundle  # noqa: E402
import build_release  # noqa: E402
import updater as device_updater  # noqa: E402
from presto_ai_dash import device_app, device_install  # noqa: E402


class SlotBundleTests(unittest.TestCase):

    def setUp(self):
        self.built = bundle.snapshot()

    def test_the_slot_bundle_is_a_full_slot_hash_of_slot_relative_names(self):
        slot = self.built["slot"]
        self.assertEqual(slot["scheme"], "slot-v1")
        self.assertEqual(slot["min_bootstrap"], 1)
        self.assertEqual(len(slot["revision"]), 64)
        self.assertEqual(bundle.revision(slot["hashes"]), slot["revision"])
        self.assertNotIn("ai_dash.py", slot["hashes"])
        self.assertNotIn(bundle.SLOT_REVISION_LABEL, slot["hashes"])
        self.assertNotIn(bundle.SLOT_REVISION_FULL, slot["hashes"])
        self.assertIn("ai_dash_app.py", slot["hashes"])
        for name in slot["hashes"]:
            self.assertNotIn("/", name)

    def test_the_slot_revision_is_not_the_legacy_revision(self):
        self.assertNotEqual(self.built["revision"], self.built["slot"]["revision"])

    def test_the_two_generated_files_carry_the_label_and_the_full_hash(self):
        slot = self.built["slot"]
        self.assertEqual(slot["files"][bundle.SLOT_REVISION_LABEL],
                         (slot["revision"][:bundle.REVISION_CHARS] + "\n").encode())
        self.assertEqual(slot["files"][bundle.SLOT_REVISION_FULL],
                         (slot["revision"] + "\n").encode())

    def test_the_bootstrap_is_installed_outside_both_slots(self):
        boot = self.built["bootstrap"]
        self.assertEqual(boot["version"], 1)
        self.assertEqual(set(boot["files"]),
                         {"/ai_dash_boot.py", "/ai_dash_boot_files/boot.py",
                          "/ai_dash_boot_files/activation.py"})
        for target in boot["files"]:
            self.assertFalse(target.startswith("/ai_dash_slots/"))

    def test_the_activation_module_is_one_source_installed_twice(self):
        activation = (Path(presto_path.PRESTO_DIR) / "activation.py").read_bytes()
        self.assertEqual(self.built["bootstrap"]["files"][
            "/ai_dash_boot_files/activation.py"], activation)
        self.assertEqual(self.built["slot"]["files"]["activation.py"], activation)


class ReleaseCheckTests(unittest.TestCase):

    def setUp(self):
        self.built = bundle.snapshot()

    def test_the_real_bundle_passes(self):
        self.assertEqual(build_release.check_slot_identity(
            self.built["slot"], self.built["bootstrap"]), self.built["slot"])

    def test_a_slot_without_its_entry_module_is_refused(self):
        slot = dict(self.built["slot"], files={"ui.py": b"x"})
        with self.assertRaises(ValueError):
            build_release.check_slot_identity(slot, self.built["bootstrap"])

    def test_a_slot_without_a_bootstrap_requirement_is_refused(self):
        slot = dict(self.built["slot"], min_bootstrap=None)
        with self.assertRaises(ValueError):
            build_release.check_slot_identity(slot, self.built["bootstrap"])

    def test_a_bootstrap_without_a_slot_is_refused(self):
        with self.assertRaises(ValueError):
            build_release.check_slot_identity(None, self.built["bootstrap"])


class StagedPackageTests(unittest.TestCase):

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name) / "presto_app"
        self.built = build_release.stage(Path(presto_path.REPO_ROOT), self.folder)

    def test_the_staged_package_reads_back_the_same_identities(self):
        snapshot = device_install.bundled_snapshot(self.folder)
        self.assertEqual(snapshot["revision"], self.built["revision"])
        self.assertEqual(snapshot["slot"]["revision"], self.built["slot"]["revision"])
        self.assertEqual(snapshot["bootstrap"]["hashes"],
                         self.built["bootstrap"]["hashes"])

    def test_a_tampered_slot_revision_file_is_refused_not_served(self):
        slot = self.built["slot"]
        (self.folder.parent / "presto_app_slot" / "revision.sha256").write_text(
            "0" * 64 + "\n", encoding="ascii")
        self.assertIsNone(device_install.bundled_slot(
            self.folder.parent / "presto_app_slot"))
        self.assertIsNotNone(slot)

    def test_private_files_are_never_staged_into_any_layout(self):
        names = {path.name for path in Path(self.temp.name).rglob("*") if path.is_file()}
        self.assertFalse({"config.py", "secrets.py", "config.example.py"} & names)


class ManifestParityTests(unittest.TestCase):

    def setUp(self):
        self.inventory = device_app.slot_inventory(bundle.snapshot())
        self.assertIsNotNone(self.inventory)

    def test_the_served_manifest_is_accepted_by_the_device_parser(self):
        text = device_app.manifest_bytes(self.inventory).decode("ascii")
        parsed = device_updater.parse_manifest(text)
        self.assertEqual(parsed["revision"], self.inventory["revision"])
        self.assertEqual({name for name, _, _ in parsed["files"]},
                         set(self.inventory["files"]))

    def test_the_served_file_bytes_match_their_manifest_hashes(self):
        for name, data in self.inventory["files"].items():
            self.assertEqual(hashlib.sha256(data).hexdigest(),
                             self.inventory["hashes"][name])
            self.assertGreater(len(data), 0)

    def test_a_bundle_with_a_disagreeing_revision_is_not_served(self):
        broken = bundle.snapshot()
        broken["slot"] = dict(broken["slot"], revision="0" * 64)
        self.assertIsNone(device_app.slot_inventory(broken))

    def test_generated_revision_files_are_never_offered_for_download(self):
        self.assertNotIn("revision.txt", self.inventory["files"])
        self.assertNotIn("revision.sha256", self.inventory["files"])


class MemoryDevice(object):
    """An in-memory MicroPython filesystem for the generated device scripts."""

    def execute(self, script):
        import builtins

        files = self.files
        opened = self.opened = []

        def stat(path):
            if path not in files:
                raise OSError("missing")
            return (0,) * 10

        def rename(before, after):
            if before not in files:
                raise OSError("missing")
            files[after] = files.pop(before)

        class Namespace(object):
            def __init__(self, **values):
                self.__dict__.update(values)

        fake_os = Namespace(stat=stat, rename=rename, remove=lambda p: files.pop(p, None),
                            listdir=lambda folder: sorted(
                                p.rsplit("/", 1)[1] for p in files
                                if p.rsplit("/", 1)[0] == folder),
                            mkdir=lambda folder: None,
                            sync=lambda: None,
                            uname=lambda: Namespace(machine="Pimoroni Presto with RP2350"))
        fake_sys = Namespace(implementation=Namespace(name="micropython"))
        real_import = builtins.__import__

        def importing(name, *args, **kwargs):
            return {"os": fake_os, "sys": fake_sys}.get(name) or real_import(name, *args, **kwargs)

        class Handle(object):
            def __init__(self, path):
                self.path = path
                self.parts = []

            def write(self, value):
                self.parts.append(value)
                return len(value)

            def __enter__(self):
                return self

            def __exit__(self, *args):
                if all(isinstance(part, bytes) for part in self.parts):
                    files[self.path] = b"".join(self.parts)
                else:
                    files[self.path] = "".join(str(part) for part in self.parts)

        def opener(path, mode="rb"):
            opened.append(path)
            if "w" in mode:
                return Handle(path)
            if path not in files:
                raise OSError("missing")
            value = files[path]
            return io.StringIO(value if isinstance(value, str) else value.decode()) if "b" not in mode else io.BytesIO(value)

        namespace = {"__builtins__": {**vars(builtins), "open": opener,
                                      "__import__": importing}}
        return exec(script, namespace)

    def __init__(self, files):
        self.files = dict(files)


class LayoutScriptTests(unittest.TestCase):
    def test_private_backups_and_unrelated_modules_are_preserved(self):
        files = dict(self.files)
        names = ("config_backup.py", "secrets_old.py", "custom_helper.py", "my_secrets.mpy")
        for name in names:
            files["/ai_dash_files/" + name] = b"synthetic user file"
        device = MemoryDevice(files)
        device.execute(self.script)
        for name in names:
            self.assertEqual(device.files["/ai_dash_files/" + name], b"synthetic user file")

    def test_usb_activation_never_regresses_a_boot_since_the_last_check(self):
        files = dict(self.files)
        files["/ai_dash_state/journal.json"] = json.dumps({"boots": 900})
        device = MemoryDevice(files)
        device.execute(self.script)
        self.assertEqual(json.loads(device.files["/ai_dash_state/journal.json"])["boots"], 900)

    def setUp(self):
        self.built = bundle.snapshot()
        boot, slots = device_install._slot_targets(self.built)
        self.files = {}
        staged = {}
        for group in list(slots.values()) + [boot]:
            for path, data in group.items():
                self.files[path] = b"old"
                self.files[path + ".presto-new"] = data
                staged[path] = data
        self.files["/ai_dash_files/config.py"] = b"private config"
        self.files["/secrets.py"] = b"private secrets"
        self.files["/ai_dash.py"] = b"legacy launcher"
        self.files["/ai_dash_files/ai_dash_app.py"] = b"legacy app"
        self.files["/ai_dash_files/revision.txt"] = b"abc123"
        self.files["/main.py"] = b"firmware"
        self.files["/ai_dash_files/notes.txt"] = b"user file"
        boot_hashes, slot_hashes = device_install.slot_hashes(self.built)
        self.script = device_install.layout_script(
            boot_hashes, slot_hashes,
            '{"v":1,"bootstrap":1,"boots":1,"active":"a","confirmed":"a",'
            '"pending":null,"failure":null,"success":null}', retire_legacy=True,
            legacy_paths=self.built["files"])
        self.device = MemoryDevice(self.files)
        self.device.execute(self.script)

    def test_both_slots_and_the_bootstrap_are_installed_and_verified(self):
        for name in ("/ai_dash_slots/a/ai_dash_app.py", "/ai_dash_slots/b/ui.py",
                     "/ai_dash_boot.py", "/ai_dash_boot_files/boot.py"):
            self.assertTrue(self.device.files[name].startswith(b""), name)
            self.assertNotEqual(self.device.files[name], b"old")

    def test_the_journal_is_written_outside_both_slots(self):
        journal = json.loads(self.device.files["/ai_dash_state/journal.json"])
        self.assertEqual((journal["active"], journal["confirmed"]), ("a", "a"))

    def test_the_legacy_app_is_retired_but_private_and_unrelated_files_are_not(self):
        self.assertNotIn("/ai_dash.py", self.device.files)
        self.assertNotIn("/ai_dash_files/ai_dash_app.py", self.device.files)
        self.assertNotIn("/ai_dash_files/revision.txt", self.device.files)
        self.assertEqual(self.device.files["/ai_dash_files/config.py"], b"private config")
        self.assertEqual(self.device.files["/secrets.py"], b"private secrets")
        self.assertEqual(self.device.files["/main.py"], b"firmware")
        self.assertEqual(self.device.files["/ai_dash_files/notes.txt"], b"user file")

    def test_no_private_file_is_ever_opened_by_the_script(self):
        opened = set(self.device.opened)
        self.assertNotIn("/ai_dash_files/config.py", opened)
        self.assertNotIn("/secrets.py", opened)
        self.assertNotIn("/main.py", opened)

    def test_a_sync_script_never_retires_the_legacy_app(self):
        boot, slots = device_install._slot_targets(self.built)
        del boot
        files = {path: b"old" for path in slots["b"]}
        files.update({path + ".presto-new": data for path, data in slots["b"].items()})
        files["/ai_dash.py"] = b"legacy launcher"
        hashes = {path: hashlib.sha256(data).hexdigest()
                  for path, data in slots["b"].items()}
        script = device_install.layout_script(
            {}, {"b": hashes},
            '{"v":1,"bootstrap":1,"boots":4,"active":"b","confirmed":"b",'
            '"pending":null,"failure":null,"success":null}')
        device = MemoryDevice(files)
        device.execute(script)
        self.assertIn("/ai_dash.py", device.files)
        journal = json.loads(device.files["/ai_dash_state/journal.json"])
        self.assertEqual(journal["boots"], 4)


class MigrationOperationTests(unittest.TestCase):
    def test_directories_and_boot_epoch_are_prepared_before_uploads(self):
        prepared = []
        script_text = []
        def remote(port, args):
            if "exec" in args and "mkdirs()" in args[-1]:
                prepared.append(True)
            if "fs" in args:
                self.assertTrue(prepared, "upload before directory preparation")
                script_text.append(Path(args[-1]).read_text())
            return self.remote(port, args)
        device_install.migrate_operation("TESTPORT", self.built,
            json.dumps({"boots": 1}), remote=remote, receipt=self.receipt)
        import ast
        line = next(line for line in script_text[0].splitlines() if line.startswith('journal = '))
        preserved = json.loads(ast.literal_eval(line.split(' = ', 1)[1]))
        self.assertEqual(preserved["boots"], 100)


    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.receipt = Path(self.temp.name) / "receipt.json"
        self.built = bundle.snapshot()
        self.calls = []

    def remote(self, port, arguments):
        self.calls.append(arguments)
        if "exec" in arguments:
            return device_install.MARKER + json.dumps({"config": True, "legacy": True, "boots": 100})
        if "run" in arguments:
            return device_install.MARKER + json.dumps({"installed": []})
        return ""

    def test_every_slot_and_bootstrap_file_is_uploaded_beside_its_target(self):
        device_install.migrate_operation("TESTPORT", self.built,
                                         '{"v":1,"bootstrap":1,"boots":1,'
                                         '"active":"a","confirmed":"a",'
                                         '"pending":null,"failure":null,'
                                         '"success":null}',
                                         remote=self.remote, receipt=self.receipt)
        uploads = [arguments[i + 2] for arguments in self.calls for i in
                   range(len(arguments)) if arguments[i] == "cp"]
        expected = set(self.built["bootstrap"]["files"])
        for slot in ("a", "b"):
            expected |= {"/ai_dash_slots/" + slot + "/" + name.lstrip("/")
                         for name in self.built["slot"]["files"]}
        self.assertEqual({path[1:] for path in uploads},
                         {path + ".presto-new" for path in expected})
        self.assertTrue(all("private" not in path for path in uploads))

    def test_a_device_without_settings_is_refused_before_any_upload(self):
        def remote(port, arguments):
            self.calls.append(arguments)
            return device_install.MARKER + json.dumps({"config": False, "legacy": True})
        with self.assertRaisesRegex(RuntimeError, "Set up a Presto"):
            device_install.migrate_operation("TESTPORT", self.built, "{}",
                                             remote=remote, receipt=self.receipt)
        self.assertFalse(any("cp" in arguments for arguments in self.calls))

    def test_a_device_already_on_slots_is_not_migrated_again(self):
        def remote(port, arguments):
            self.calls.append(arguments)
            return device_install.MARKER + json.dumps({"config": True, "legacy": False})
        with self.assertRaisesRegex(RuntimeError, "not on the legacy install layout"):
            device_install.migrate_operation("TESTPORT", self.built, "{}",
                                             remote=remote, receipt=self.receipt)
        self.assertFalse(any("cp" in arguments for arguments in self.calls))


if __name__ == "__main__":
    unittest.main()
