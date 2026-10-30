"""The standalone setup script, exercised against an in-memory Presto.

These tests never touch a board. They run the exact rendered installer and
give it a fake device that answers the same probe codes, so the flow that
matters can be checked: nothing is written before the handoff opens, a board
that is not a Presto is refused, existing settings are kept unless they are
explicitly replaced, a stable device identifier survives a repair, an
interrupted transfer rolls back, a retry finishes the job, and the encrypted
file is removed only after the device reached the daemon.
"""

import ast
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import types
import unittest
import unittest.mock

import presto_path

TOOLS = Path(presto_path.REPO_ROOT) / "tools"
SRC = Path(presto_path.REPO_ROOT) / "daemon" / "src"
for folder in (TOOLS, SRC):
    if str(folder) not in sys.path:
        sys.path.insert(0, str(folder))

import presto_bundle  # noqa: E402
from presto_ai_dash import device_setup  # noqa: E402

BUNDLE = presto_bundle.snapshot()
TOKEN = "device-token-0123456789-abcdefghij"
MARK = "PRESTO_SETUP:"
CONFIG = "/ai_dash_files/config.py"
SECRETS = "/secrets.py"


def module_source():
    return device_setup.render_setup_script(BUNDLE, "0.50.0").decode("utf-8")


def rendered_module():
    module = types.ModuleType("rendered_presto_setup")
    module.__dict__["__file__"] = "presto_setup.py"
    exec(compile(module_source(), "presto_setup.py", "exec"), module.__dict__)
    return module


class FakeDevice:
    """The device primitives the installer uses, over an in-memory filesystem."""

    def __init__(self, files=None, board="Pimoroni Presto with RP2350", reach="ok",
                 device_id=None, name=None, fail_write=None, corrupt=None, fail_rename=None):
        self.files = dict(files if files is not None else {})
        self.board = board
        self.reach = reach
        self.device_id = device_id
        self.config_name = name
        self.fail_write = fail_write
        self.corrupt = set(corrupt or ())
        self.fail_rename = fail_rename
        self.writes = []
        self.launched = False
        self.closed = False

    def _marker(self, value):
        return (MARK + json.dumps(value)).encode("ascii")

    def run(self, code, timeout=20):
        text = bytes(code).decode("utf-8")
        if "os.uname().machine" in text:
            return self._marker({"micropython": True, "machine": self.board})
        if "'reach': result" in text:
            return self._marker({"reach": self.reach})
        if "DEVICE_ID" in text:
            return self._marker({"device_id": self.device_id, "name": self.config_name})
        rename = re.search(r"os\.rename\((.+), (.+)\)", text)
        if rename:
            before, after = (ast.literal_eval(rename.group(1)),
                             ast.literal_eval(rename.group(2)))
            if before not in self.files:
                raise RuntimeError("missing")
            self.files[after] = self.files.pop(before)
            return b""
        raise AssertionError("unexpected device script: " + text[:60])

    def exists(self, path):
        return path in self.files

    def digest(self, path):
        if path not in self.files:
            return None
        return hashlib.sha256(self.files[path]).hexdigest()

    def write(self, path, data):
        if path == self.fail_write:
            raise RuntimeError("injected USB failure")
        self.writes.append(path)
        self.files[path] = bytes(data).replace(b"x", b"y") if path in self.corrupt else bytes(data)

    def rename(self, before, after):
        if self.fail_rename is not None and after == self.fail_rename:
            self.fail_rename = None
            raise RuntimeError("injected rename failure")
        self.run(("import os\nos.rename(%r, %r)" % (before, after)).encode("ascii"))

    def remove(self, path):
        self.files.pop(path, None)

    def listdir(self, folder):
        prefix = folder.rstrip("/") + "/"
        return [path[len(prefix):] for path in self.files
                if path.startswith(prefix) and "/" not in path[len(prefix):]]

    def launch(self):
        self.launched = True

    def close(self):
        self.closed = True


def existing_install():
    """A device that already runs an older build, with private settings."""
    files = {path: b"old " + path.encode() for path in BUNDLE["files"]}
    files[CONFIG] = b"DAEMON_HOST = '10.0.0.2'\nDEVICE_ID = '%s'\n" % (b"a" * 32)
    files[SECRETS] = b"WIFI_SSID = 'old'\nWIFI_PASSWORD = 'old-pass'\n"
    files["/ai_dash_files/old_helper.py"] = b"old helper"
    files["/main.py"] = b"stock launcher"
    return files


class ScriptCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)

    def handoff(self, code, bundle=BUNDLE, host="192.168.1.50", wifi="set"):
        payload = device_setup.build_payload(bundle, host, 8787, TOKEN, "presto", "0.50.0",
                                             wifi=wifi)
        path = self.folder / device_setup.HANDOFF_NAME
        path.write_bytes(device_setup.build_handoff(payload, code))
        return path

    def run_script(self, path, answers, device, arguments=()):
        module = rendered_module()
        said, asked = [], list(answers)

        def ask(text, secret=False):
            said.append(text)
            if not asked:
                raise AssertionError("unexpected prompt: " + text)
            return asked.pop(0)

        def say(text):
            said.append(str(text))

        code = module.main(["--handoff", str(path), *arguments],
                           device_factory=lambda port: device, ask=ask, say=say)
        return code, said

    def assert_no_secret(self, said, *secrets):
        blob = "\n".join(said)
        for secret in secrets:
            self.assertNotIn(secret, blob)


class FirstTimeTests(ScriptCase):
    def test_a_fresh_presto_is_installed_configured_and_confirmed(self):
        code = device_setup.new_unlock_code()
        path = self.handoff(code)
        device = FakeDevice()
        status, said = self.run_script(path, [code, "1", "Home Wi-Fi", "s3cret-pass", "yes"],
                                       device)
        self.assertEqual(status, 0)
        # The app, its revision and both private files are on the device now.
        for target, data in BUNDLE["files"].items():
            self.assertEqual(device.files[target], data)
        config = device.files[CONFIG].decode("ascii")
        self.assertIn("DAEMON_HOST = '192.168.1.50'", config)
        self.assertIn("DAEMON_PORT = 8787", config)
        self.assertIn(TOKEN, config)
        self.assertIn("DEVICE_ID = ", config)
        secrets = device.files[SECRETS].decode("ascii")
        self.assertIn("WIFI_SSID = 'Home Wi-Fi'", secrets)
        self.assertIn("WIFI_PASSWORD = 's3cret-pass'", secrets)
        self.assertTrue(device.launched)
        self.assertTrue(device.closed)
        self.assertFalse(path.exists(), "a successful setup removes the handoff")
        # The device token and the Wi-Fi password are never printed.
        self.assert_no_secret(said, TOKEN, "s3cret-pass", code)
        self.assertTrue(any("reached the daemon" in text for text in said))

    def test_the_device_identifier_is_stable_random_and_outside_the_app_files(self):
        code = device_setup.new_unlock_code()
        device = FakeDevice()
        status, said = self.run_script(self.handoff(code), [code, "1", "net", "pw", "yes"],
                                       device)
        self.assertEqual(status, 0, said)
        written = device.files[CONFIG].decode("ascii")
        match = re.search(r"DEVICE_ID = '([0-9a-f]{32})'", written)
        self.assertIsNotNone(match)
        self.assertNotIn(match.group(1), "".join(BUNDLE["hashes"]))

    def test_a_bad_handoff_is_refused_before_any_device_is_opened(self):
        code = device_setup.new_unlock_code()
        path = self.handoff(code)
        opened = []

        def factory(port):
            opened.append(port)
            return FakeDevice()

        module = rendered_module()
        said = []
        status = module.main(["--handoff", str(path)], device_factory=factory,
                             ask=lambda text, secret=False: "A" * 32, say=said.append)
        self.assertEqual(status, 1)
        self.assertEqual(opened, [])
        self.assertTrue(path.exists())
        self.assertTrue(any("could not be opened" in text for text in said))

    def test_a_wrong_code_may_be_retried_on_the_same_download(self):
        code = device_setup.new_unlock_code()
        path = self.handoff(code)
        device = FakeDevice()
        status, said = self.run_script(
            path, ["0000-0000-0000-0000-0000-0000-0000-0000", code, "1", "net", "pw", "yes"],
            device)
        self.assertEqual(status, 0, said)
        self.assertTrue(device.launched)
        self.assertFalse(path.exists())

    def test_a_mismatched_bundle_revision_stops_before_the_device(self):
        other = dict(BUNDLE)
        other["revision"] = "0" * 64
        code = device_setup.new_unlock_code()
        path = self.handoff(code, bundle=other)
        device = FakeDevice()
        status, said = self.run_script(path, [code], device)
        self.assertEqual(status, 1)
        self.assertFalse(device.writes)
        self.assertTrue(any("different Presto app" in text for text in said))
        self.assertTrue(path.exists())


class PayloadValidationTests(unittest.TestCase):
    def sealed(self, module, value, code):
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        from cryptography.hazmat.primitives.kdf.hkdf import HKDF
        salt, nonce = b"\x01" * 16, b"\x02" * 12
        key = HKDF(algorithm=hashes.SHA256(), length=32, salt=salt,
                   info=module.KDF_INFO).derive(module.unlock_code_bytes(code))
        sealed = AESGCM(key).encrypt(nonce, json.dumps(value).encode("utf-8"), module.AAD)
        return json.dumps({"v": 1, "kdf": "HKDF-SHA256", "cipher": "AES-256-GCM",
                           "salt": base64.b64encode(salt).decode("ascii"),
                           "nonce": base64.b64encode(nonce).decode("ascii"),
                           "ct": base64.b64encode(sealed).decode("ascii")}).encode("utf-8")

    def test_a_good_payload_opens_and_every_bad_field_is_refused(self):
        module = rendered_module()
        code = device_setup.new_unlock_code()
        good = device_setup.build_payload(BUNDLE, "192.168.1.50", 8787, TOKEN, "presto",
                                          "0.50.0")
        self.assertEqual(module.open_handoff(self.sealed(module, good, code), code), good)
        cases = {
            "host": lambda value: value["daemon"].update(host="localhost"),
            "host scheme": lambda value: value["daemon"].update(host="http://192.168.1.5"),
            "port": lambda value: value["daemon"].update(port=0),
            "token": lambda value: value["daemon"].update(token="short"),
            "settings": lambda value: value["device"]["settings"].update(poll_interval=1),
            "name": lambda value: value["device"].update(name="x" * 33),
            "revision": lambda value: value["app"].update(revision="0" * 63),
            "label": lambda value: value["app"].update(label="0" * 12),
            "extra": lambda value: value.update(extra=1),
            "version": lambda value: value.update(v=2),
            "wifi choice": lambda value: value["device"].update(wifi="maybe"),
            "wifi choice type": lambda value: value["device"].update(wifi=True),
        }
        for name, change in cases.items():
            value = json.loads(json.dumps(good))
            change(value)
            with self.subTest(name=name), self.assertRaises(module.SetupStopped):
                module.open_handoff(self.sealed(module, value, code), code)
        for choice in ("set", "keep"):
            value = json.loads(json.dumps(good))
            value["device"]["wifi"] = choice
            self.assertEqual(module.open_handoff(self.sealed(module, value, code), code), value)
        legacy = json.loads(json.dumps(good))
        del legacy["device"]["wifi"]
        self.assertEqual(module.open_handoff(self.sealed(module, legacy, code), code), legacy)


class BoardAndPortTests(ScriptCase):
    def test_a_board_that_is_not_a_presto_is_refused_before_writing(self):
        code = device_setup.new_unlock_code()
        path = self.handoff(code)
        device = FakeDevice(board="Raspberry Pi Pico 2")
        status, said = self.run_script(path, [code, "1"], device)
        self.assertEqual(status, 1)
        self.assertFalse(device.writes)
        self.assertFalse(device.launched)
        self.assertTrue(any("not a Pimoroni Presto" in text for text in said))
        self.assertTrue(path.exists())

    def test_a_port_is_never_chosen_automatically(self):
        module = rendered_module()
        say, asked = [], []

        def ask(text, secret=False):
            asked.append(text)
            return "2"

        self.assertEqual(module.choose_port([("COM3", "Presto"), ("COM4", "other")], None,
                                            ask, say.append), "COM4")
        self.assertEqual(say[0], "USB devices:")
        self.assertTrue(any("COM3" in text for text in say))
        self.assertTrue(any("COM4" in text for text in say))
        self.assertIn("never chosen for you", asked[0])

    def test_a_named_port_must_be_one_of_the_connected_devices(self):
        module = rendered_module()
        self.assertEqual(module.choose_port([("COM3", "Presto")], "com3",
                                            lambda text, secret=False: "", print), "COM3")
        with self.assertRaises(module.SetupStopped):
            module.choose_port([("COM3", "Presto")], "COM9",
                               lambda text, secret=False: "", print)
        with self.assertRaises(module.SetupStopped):
            module.choose_port([], None, lambda text, secret=False: "1", print)

    def test_mpremote_port_listing_is_parsed(self):
        module = rendered_module()
        text = ("COM3 1A2B:3C4D Pimoroni Presto\n"
                "/dev/ttyACM0 2E8A:000F MicroPython board\n")
        self.assertEqual(module.parse_ports(text),
                         [("COM3", "1A2B:3C4D Pimoroni Presto"),
                          ("/dev/ttyACM0", "2E8A:000F MicroPython board")])


class PreservationTests(ScriptCase):
    def test_existing_private_settings_are_kept_unless_explicitly_replaced(self):
        code = device_setup.new_unlock_code()
        path = self.handoff(code, wifi="keep")
        device = FakeDevice(existing_install(), device_id="a" * 32, name="old-name")
        before = dict(device.files)
        status, said = self.run_script(path, [code, "1", "n", "yes"], device)
        self.assertEqual(status, 0, said)
        self.assertEqual(device.files[CONFIG], before[CONFIG])
        self.assertEqual(device.files[SECRETS], before[SECRETS])
        self.assertEqual(device.files["/main.py"], before["/main.py"])
        self.assertTrue(any("keep the existing file" in text for text in said))
        for target, data in BUNDLE["files"].items():
            self.assertEqual(device.files[target], data)
        self.assertNotIn("/ai_dash_files/old_helper.py", device.files)

    def test_replacing_configuration_keeps_the_existing_device_identifier(self):
        code = device_setup.new_unlock_code()
        path = self.handoff(code, wifi="keep")
        device = FakeDevice(existing_install(), device_id="b" * 32, name="old-name")
        status, said = self.run_script(path, [code, "1", "y", "yes"], device)
        self.assertEqual(status, 0, said)
        written = device.files[CONFIG].decode("ascii")
        self.assertIn("DEVICE_ID = '%s'" % ("b" * 32), written)
        self.assertIn("DEVICE_NAME = 'old-name'", written)
        self.assertIn("DAEMON_HOST = '192.168.1.50'", written)
        self.assertEqual(device.files[SECRETS], existing_install()[SECRETS])

    def test_keeping_the_wifi_asks_nothing_and_leaves_the_file_alone(self):
        code = device_setup.new_unlock_code()
        path = self.handoff(code, wifi="keep")
        device = FakeDevice(existing_install(), device_id="a" * 32, name="old-name")
        before = dict(device.files)
        status, said = self.run_script(path, [code, "1", "n", "yes"], device)
        self.assertEqual(status, 0, said)
        self.assertEqual(device.files[SECRETS], before[SECRETS])
        blob = "\n".join(said)
        self.assertNotIn("Wi-Fi network name", blob)
        self.assertNotIn("Replace the Wi-Fi settings", blob)
        self.assertIn("/secrets.py: keep the existing file", blob)

    def test_keeping_the_wifi_on_a_presto_that_has_none_stops_before_writing(self):
        code = device_setup.new_unlock_code()
        path = self.handoff(code, wifi="keep")
        files = existing_install()
        del files[SECRETS]
        device = FakeDevice(files, device_id="a" * 32, name="old-name")
        before = dict(device.files)
        status, said = self.run_script(path, [code, "1"], device)
        self.assertEqual(status, 1)
        self.assertEqual(device.files, before)
        self.assertEqual(device.writes, [])
        self.assertFalse(device.launched)
        self.assertTrue(path.exists(), "the handoff is kept for the corrected download")
        blob = "\n".join(said)
        self.assertIn("has no Wi-Fi settings", blob)
        self.assertIn("ticked", blob)

    def test_setting_the_wifi_always_asks_even_when_a_file_exists(self):
        code = device_setup.new_unlock_code()
        path = self.handoff(code, wifi="set")
        device = FakeDevice(existing_install(), device_id="a" * 32, name="old-name")
        status, said = self.run_script(path, [code, "1", "n", "Office 2G", "new-wifi-pass", "yes"],
                                       device)
        self.assertEqual(status, 0, said)
        written = device.files[SECRETS].decode("ascii")
        self.assertIn("WIFI_SSID = 'Office 2G'", written)
        self.assertIn("WIFI_PASSWORD = 'new-wifi-pass'", written)
        self.assertNotIn("Replace the Wi-Fi settings", "\n".join(said))
        self.assert_no_secret(said, "new-wifi-pass")

    def test_a_setup_file_from_an_older_daemon_still_asks_as_before(self):
        # Same app revision, so the script accepts it, but it has no Wi-Fi choice.
        code = device_setup.new_unlock_code()
        payload = device_setup.build_payload(BUNDLE, "192.168.1.50", 8787, TOKEN, "presto", "0.50.0")
        del payload["device"]["wifi"]
        with unittest.mock.patch.object(device_setup, "validate_payload", side_effect=lambda value: value):
            data = device_setup.build_handoff(payload, code)
        path = self.folder / device_setup.HANDOFF_NAME
        path.write_bytes(data)
        device = FakeDevice(existing_install(), device_id="a" * 32, name="old-name")
        status, said = self.run_script(path, [code, "1", "n", "n", "yes"], device)
        self.assertEqual(status, 0, said)
        self.assertTrue(any("Replace the Wi-Fi settings" in text for text in said))

    def test_a_slot_device_is_refused_before_anything_is_written(self):
        # Setup installs the single-folder app; on a bootstrap device that would
        # leave a second, older copy beside the slots.
        code = device_setup.new_unlock_code()
        path = self.handoff(code)
        files = existing_install()
        files["/ai_dash_boot.py"] = b"bootstrap"
        device = FakeDevice(files, device_id="a" * 32, name="old-name")
        before = dict(device.files)
        status, said = self.run_script(path, [code, "1"], device)
        self.assertEqual(status, 1)
        self.assertEqual(device.files, before)
        self.assertEqual(device.writes, [])
        self.assertFalse(device.launched)
        self.assertTrue(path.exists(), "the handoff is kept")
        blob = "\n".join(said)
        self.assertIn("set-daemon", blob)
        self.assertIn("A/B app slots", blob)

    def test_an_unrelated_file_on_a_fresh_device_is_left_alone(self):
        code = device_setup.new_unlock_code()
        path = self.handoff(code)
        device = FakeDevice({"/ai_dash_files/my_notes.py": b"mine"})
        status, said = self.run_script(path, [code, "1", "net", "pw", "yes"], device)
        self.assertEqual(status, 0, said)
        self.assertEqual(device.files["/ai_dash_files/my_notes.py"], b"mine")


class FailureAndRetryTests(ScriptCase):
    def test_an_interrupted_transfer_rolls_back_and_keeps_the_handoff(self):
        code = device_setup.new_unlock_code()
        path = self.handoff(code, wifi="keep")
        device = FakeDevice(existing_install(), device_id="a" * 32, name="x",
                            fail_write="/ai_dash_files/ui.py.presto-new")
        before = dict(device.files)
        status, said = self.run_script(path, [code, "1", "n", "yes"], device)
        self.assertTrue(any("Type yes to install" in text for text in said), said)
        self.assertEqual(status, 1)
        for target, data in before.items():
            self.assertEqual(device.files.get(target), data)
        self.assertTrue(path.exists())
        self.assertFalse(device.launched)

    def test_a_corrupt_arrival_never_replaces_the_installed_app(self):
        code = device_setup.new_unlock_code()
        path = self.handoff(code, wifi="keep")
        device = FakeDevice(existing_install(), device_id="a" * 32, name="x",
                            corrupt={"/ai_dash_files/cards.py.presto-new"})
        before = dict(device.files)
        status, said = self.run_script(path, [code, "1", "n", "yes"], device)
        self.assertTrue(any("Type yes to install" in text for text in said), said)
        self.assertEqual(status, 1)
        for target in BUNDLE["files"]:
            self.assertEqual(device.files[target], before[target])
        self.assertTrue(path.exists())

    def test_a_failed_commit_restores_every_file_it_moved(self):
        code = device_setup.new_unlock_code()
        path = self.handoff(code, wifi="keep")
        device = FakeDevice(existing_install(), device_id="a" * 32, name="x",
                            fail_rename="/ai_dash_files/cards.py")
        before = dict(device.files)
        status, said = self.run_script(path, [code, "1", "n", "yes"], device)
        self.assertTrue(any("Type yes to install" in text for text in said), said)
        self.assertEqual(status, 1)
        for target in BUNDLE["files"]:
            self.assertEqual(device.files[target], before[target])
        self.assertTrue(path.exists())

    def test_the_same_download_finishes_a_retry_and_leaves_no_staging_files(self):
        code = device_setup.new_unlock_code()
        path = self.handoff(code, wifi="keep")
        device = FakeDevice(existing_install(), device_id="a" * 32, name="x")
        device.files["/ai_dash_files/ui.py.presto-new"] = b"left behind"
        device.files["/ai_dash_files/home.py.presto-prev"] = b"left behind"
        status, said = self.run_script(path, [code, "1", "n", "yes"], device)
        self.assertEqual(status, 0, said)
        leftovers = [path for path in device.files
                     if path.endswith((".presto-new", ".presto-prev"))]
        self.assertEqual(leftovers, [])
        self.assertFalse(path.exists())

    def test_a_device_that_cannot_reach_the_daemon_is_reported_and_can_be_retried(self):
        code = device_setup.new_unlock_code()
        path = self.handoff(code)
        for reach, needle in (("wifi", "Wi-Fi"), ("unauthorised", "token"),
                              ("http_404", "port"), ("error", "could not reach")):
            with self.subTest(reach=reach):
                device = FakeDevice(reach=reach)
                status, said = self.run_script(path, [code, "1", "net", "pw", "yes"], device)
                self.assertEqual(status, 1)
                self.assertTrue(device.launched, "the app is started so the screen can say why")
                self.assertTrue(path.exists(), "a failed setup keeps the handoff")
                self.assertTrue(any(needle in text for text in said), said)


class StandaloneExecutionTests(unittest.TestCase):
    """The rendered file is a real program on a computer with only Python.

    These run it in a subprocess from a folder that holds nothing but it, with
    the environment that points at this repository removed, so the claim that
    the admin page's download needs no checkout is checked rather than assumed.
    """

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        (self.folder / "presto_setup.py").write_bytes(
            device_setup.render_setup_script(BUNDLE, "0.50.0"))
        self.environment = {name: value for name, value in os.environ.items()
                            if name not in ("PYTHONPATH", "PYTHONHOME")}
        self.environment["PYTHONDONTWRITEBYTECODE"] = "1"

    def run_script(self, arguments, isolated=False):
        flags = ["-I"] + (["-S"] if isolated else [])
        return subprocess.run([sys.executable, *flags, "presto_setup.py", *arguments],
                              cwd=self.folder, capture_output=True, text=True,
                              timeout=90, env=self.environment,
                              stdin=subprocess.DEVNULL)

    @staticmethod
    def output(result):
        return result.stdout + result.stderr

    def test_it_prints_its_usage_and_needs_no_third_party_module_to_load(self):
        # -S removes site-packages, so cryptography is not importable: reaching
        # the parser means the file itself imports only the standard library.
        result = self.run_script(["--help"], isolated=True)
        self.assertEqual(result.returncode, 0, self.output(result))
        self.assertIn("presto_setup.py", result.stdout)

    def test_a_missing_encryption_library_is_explained_not_a_traceback(self):
        result = self.run_script(["--handoff", "absent.enc"], isolated=True)
        self.assertEqual(result.returncode, 1, self.output(result))
        self.assertNotIn("Traceback", self.output(result))
        self.assertIn("pip install", result.stdout)
        self.assertIn("cryptography", result.stdout)
        self.assertIn("mpremote", result.stdout)

    def test_a_missing_handoff_is_reported_and_nothing_is_written(self):
        result = self.run_script(["--handoff", "absent.enc"])
        self.assertEqual(result.returncode, 1, self.output(result))
        self.assertNotIn("Traceback", self.output(result))
        self.assertIn("The setup file could not be read", result.stdout)
        self.assertEqual(sorted(path.name for path in self.folder.iterdir()),
                         ["presto_setup.py"], "setup writes nothing on this computer")

    def test_the_file_imports_third_party_modules_only_where_they_are_used(self):
        tree = ast.parse(module_source())
        top_level = set()
        for node in tree.body:
            if isinstance(node, ast.Import):
                top_level.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                top_level.add((node.module or "").split(".")[0])
        self.assertEqual(top_level, {"argparse", "base64", "binascii", "getpass",
                                     "hashlib", "json", "os", "re", "sys"})


class DependencyTests(ScriptCase):
    def test_missing_cryptography_is_explained_instead_of_crashing(self):
        module = rendered_module()
        real = __import__

        def blocked(name, *args, **kwargs):
            if name.split(".")[0] == "cryptography":
                raise ImportError("simulated")
            return real(name, *args, **kwargs)

        code = device_setup.new_unlock_code()
        path = self.handoff(code)
        said = []
        with unittest.mock.patch("builtins.__import__", side_effect=blocked):
            status = module.main(["--handoff", str(path)], device_factory=lambda port: None,
                                 ask=lambda text, secret=False: code, say=said.append)
        self.assertEqual(status, 1)
        self.assertTrue(any("pip install" in text for text in said))
        self.assertIn("cryptography", "\n".join(said))

    def test_missing_usb_tools_are_explained_before_conversion(self):
        module = rendered_module()
        real = __import__

        def blocked(name, *args, **kwargs):
            if name.split(".")[0] in ("serial", "mpremote"):
                raise ImportError("simulated")
            return real(name, *args, **kwargs)

        with unittest.mock.patch("builtins.__import__", side_effect=blocked):
            with self.assertRaises(module.SetupStopped) as caught:
                module.list_ports()
        self.assertIn("mpremote", str(caught.exception))

    def test_a_missing_handoff_names_the_file_it_wants(self):
        module = rendered_module()
        with self.assertRaises(module.SetupStopped) as caught:
            module.find_handoff(None)
        self.assertIn("presto-setup.enc", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
