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
import io
import json
import os
from pathlib import Path
import re
import shutil
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
        self.resets = []
        self.log = []

    def _marker(self, value):
        return (MARK + json.dumps(value)).encode("ascii")

    def run(self, code, timeout=20):
        text = bytes(code).decode("utf-8")
        if "os.uname().machine" in text:
            return self._marker({"micropython": True, "machine": self.board})
        if "'reach': result" in text:
            return self._marker({"reach": self.reach})
        if "DEVICE_ID" in text:
            self.log.append("config_state")
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

    def read(self, path):
        return bytes(self.files[path])

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

    def reset_to_launcher(self):
        self.resets.append(len(self.writes))
        self.log.append("reset")

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


def slot_install():
    """A Presto on the over-the-air layout: bootstrap, two slots, private settings."""
    return {
        "/ai_dash_boot.py": b"bootstrap",
        "/ai_dash_boot_files/boot.py": b"boot",
        "/ai_dash_slots/a/ai_dash_app.py": b"slot a app",
        "/ai_dash_slots/b/ai_dash_app.py": b"slot b app",
        CONFIG: b"DAEMON_HOST = '10.0.0.2'\nDEVICE_NAME = 'desk'\nDEVICE_ID = '%s'\n" % (b"a" * 32),
        SECRETS: b"WIFI_SSID = 'old'\nWIFI_PASSWORD = 'old-pass'\n",
        "/main.py": b"stock launcher",
    }


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
        # What is plugged into this computer must not decide whether a test passes.
        module.list_ports = lambda: [("COM3", "USB Serial Device")]
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
        status, said = self.run_script(path, [code, "Home Wi-Fi", "s3cret-pass", "yes"],
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
        status, said = self.run_script(self.handoff(code), [code, "net", "pw", "yes"],
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
            path, ["0000-0000-0000-0000-0000-0000-0000-0000", code, "net", "pw", "yes"],
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
        status, said = self.run_script(path, [code], device)
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


class MergeConfigTests(unittest.TestCase):
    """Only the daemon lines of a Presto's own settings change."""

    def merge(self, existing, name="presto", device_id="c" * 32):
        module = rendered_module()
        payload = device_setup.build_payload(BUNDLE, "192.168.1.50", 8787, TOKEN, "presto", "0.50.0")
        return module.merge_config(existing, payload, name, device_id)

    def test_comments_and_other_settings_survive(self):
        existing = (b"# tuned by hand\nPOLL_INTERVAL = 30\nDAEMON_HOST = 'x'  # old\n"
                    b"DAEMON_PORT = 1\nSHARED_TOKEN = 'old'\nDEVICE_ID = '" + b"d" * 32 + b"'\n"
                    b"DEVICE_NAME = 'desk'\n")
        merged = self.merge(existing).decode()
        self.assertIn("# tuned by hand", merged)
        self.assertIn("POLL_INTERVAL = 30", merged)
        self.assertIn("DEVICE_ID = '%s'" % ("d" * 32), merged)
        self.assertIn("DEVICE_NAME = 'desk'", merged)
        self.assertIn("DAEMON_HOST = '192.168.1.50'", merged)
        self.assertIn("DAEMON_PORT = 8787", merged)
        self.assertIn("SHARED_TOKEN = '%s'" % TOKEN, merged)
        self.assertNotIn("'old'", merged)

    def test_windows_line_endings_are_kept(self):
        existing = b"POLL_INTERVAL = 30\r\nDAEMON_HOST = 'x'\r\nDEVICE_ID = '" + b"d" * 32 + b"'\r\n"
        merged = self.merge(existing)
        self.assertIn(b"DAEMON_HOST = '192.168.1.50'\r\n", merged)
        self.assertIn(b"POLL_INTERVAL = 30\r\n", merged)

    def test_missing_daemon_lines_are_added(self):
        merged = self.merge(b"POLL_INTERVAL = 30\n").decode()
        for name in ("DAEMON_HOST", "DAEMON_PORT", "SHARED_TOKEN", "DEVICE_ID", "DEVICE_NAME"):
            self.assertEqual(merged.count(name + " ="), 1, name)

    def test_files_it_will_not_guess_about_come_back_as_none(self):
        for label, existing in (("not python", b"DAEMON_HOST = (\n"),
                                ("duplicate", b"DAEMON_HOST = 'a'\nDAEMON_HOST = 'b'\n"),
                                ("several lines", b"DAEMON_HOST = (\n    'a'\n)\n"),
                                ("not text", b"\xff\xfe\x00DAEMON")):
            with self.subTest(case=label):
                self.assertIsNone(self.merge(existing))

    def test_the_file_is_parsed_never_run(self):
        # It came from a device, so nothing in it may execute on this computer.
        merged = self.merge(b"raise SystemExit(7)\nimport os\nos.system('exit 9')\nPOLL_INTERVAL = 5\n")
        self.assertIn(b"POLL_INTERVAL = 5", merged)


class ConfirmationTests(ScriptCase):
    """The questions a person is asked, counted."""

    def asked_for(self, device, answers, wifi="keep"):
        code = device_setup.new_unlock_code()
        answers = [code if answer == "<code>" else answer for answer in answers]
        path = self.handoff(code, wifi=wifi)
        status, said = self.run_script(path, answers, device)
        prompts = [text for text in said if text.endswith((": ", "] "))]
        return status, said, prompts

    def test_a_presto_with_settings_is_asked_for_the_code_and_one_confirmation(self):
        status, said, prompts = self.asked_for(
            FakeDevice(existing_install(), device_id="a" * 32), ["<code>", ""])
        self.assertEqual(status, 0, said)
        self.assertEqual(len(prompts), 2, prompts)
        self.assertIn("Setup unlock code", prompts[0])
        self.assertIn("Continue? [Y/n]", prompts[1])

    def test_a_new_presto_adds_only_the_wifi_questions(self):
        status, said, prompts = self.asked_for(
            FakeDevice(), ["<code>", "Home", "pass-word-1", ""], wifi="set")
        self.assertEqual(status, 0, said)
        self.assertEqual(len(prompts), 4, prompts)

    def test_enter_y_and_yes_all_continue_and_anything_else_stops(self):
        for answer, status in (("", 0), ("y", 0), ("yes", 0), ("Y", 0), ("n", 2), ("no", 2), ("x", 2)):
            with self.subTest(answer=answer):
                device = FakeDevice(existing_install(), device_id="a" * 32)
                before = dict(device.files)
                got, said, _prompts = self.asked_for(device, ["<code>", answer])
                self.assertEqual(got, status, said)
                if status == 2:
                    self.assertEqual(device.files, before, "declining writes nothing")
                    self.assertFalse(device.launched)

    def test_the_only_usb_device_is_used_without_a_question(self):
        status, said, prompts = self.asked_for(
            FakeDevice(existing_install(), device_id="a" * 32), ["<code>", "yes"])
        self.assertEqual(status, 0, said)
        self.assertTrue(any("Using the only USB device found: COM3" in text for text in said))
        self.assertFalse(any("Choose the Presto" in text for text in prompts))
        # It is still named in the summary the confirmation is for.
        self.assertTrue(any(" on COM3 (" in text for text in said))

    def test_nothing_is_written_to_a_board_that_is_not_a_presto_even_when_it_is_the_only_one(self):
        device = FakeDevice(board="Raspberry Pi Pico 2")
        status, said, _prompts = self.asked_for(device, ["<code>"])
        self.assertEqual(status, 1)
        self.assertFalse(device.writes)
        self.assertTrue(any("not a Pimoroni Presto" in text for text in said))


class MaskedInputTests(unittest.TestCase):
    """A pasted code or password shows as asterisks, so it is clear the paste landed."""

    def windows(self, keys, text="Code: "):
        module = rendered_module()
        queue = iter(keys)
        fake = types.ModuleType("msvcrt")
        fake.getwch = lambda: next(queue)
        out = io.StringIO()
        with unittest.mock.patch.dict(sys.modules, {"msvcrt": fake}), \
                unittest.mock.patch("sys.stdout", out):
            value = module._masked_windows(text)
        return value, out.getvalue()

    def test_every_character_of_a_paste_shows_as_one_asterisk(self):
        paste = "ABCD-EFGH-" * 4
        value, shown = self.windows(list(paste) + ["\r"])
        self.assertEqual(value, paste)
        self.assertEqual(shown, "Code: " + "*" * len(paste) + "\n")
        self.assertNotIn("A", shown.replace("Code", ""))

    def test_backspace_removes_one_and_arrow_keys_are_ignored(self):
        value, shown = self.windows(["a", "b", "\b", "\xe0", "H", "c", "\r"])
        self.assertEqual(value, "ac")
        self.assertEqual(shown, "Code: **\b \b*\n")

    def test_backspace_on_an_empty_line_does_nothing(self):
        value, shown = self.windows(["\b", "x", "\r"])
        self.assertEqual((value, shown), ("x", "Code: *\n"))

    def test_control_c_interrupts(self):
        with self.assertRaises(KeyboardInterrupt):
            self.windows(["a", "\x03"])

    def test_the_same_on_a_unix_terminal(self):
        module = rendered_module()
        restored = []
        termios = types.ModuleType("termios")
        termios.tcgetattr = lambda descriptor: ["saved"]
        termios.tcsetattr = lambda descriptor, when, saved: restored.append(saved)
        termios.TCSADRAIN = 1
        tty = types.ModuleType("tty")
        tty.setcbreak = lambda descriptor: None
        stdin = io.StringIO("ab\x7fc\n")
        stdin.fileno = lambda: 0
        out = io.StringIO()
        with unittest.mock.patch.dict(sys.modules, {"termios": termios, "tty": tty}), \
                unittest.mock.patch("sys.stdin", stdin), unittest.mock.patch("sys.stdout", out):
            value = module._masked_posix("Code: ")
        self.assertEqual(value, "ac")
        self.assertEqual(out.getvalue(), "Code: **\b \b*\n")
        self.assertEqual(restored, [["saved"]], "the terminal is put back")

    def test_without_a_terminal_it_falls_back_to_getpass(self):
        module = rendered_module()
        with unittest.mock.patch.object(module.getpass, "getpass", return_value="secret") as asked, \
                unittest.mock.patch("sys.stdin", io.StringIO("")):
            self.assertEqual(module.masked_input("Code: "), "secret")
        asked.assert_called_once_with("Code: ")

    def test_secret_prompts_use_it(self):
        module = rendered_module()
        with unittest.mock.patch.object(module, "masked_input", return_value="x") as masked:
            self.assertEqual(module.prompt("Password: ", secret=True), "x")
            masked.assert_called_once_with("Password: ")
        with unittest.mock.patch("builtins.input", return_value="plain") as plain:
            self.assertEqual(module.prompt("Name: "), "plain")
            plain.assert_called_once_with("Name: ")


class ErrorReportTests(ScriptCase):
    """What is said when something goes wrong: useful, and never a secret."""

    def test_an_unexpected_error_names_its_type_and_place_but_never_its_text(self):
        code = device_setup.new_unlock_code()
        device = FakeDevice(existing_install(), device_id="a" * 32,
                            fail_write="/ai_dash_files/config.py.presto-new")
        status, said = self.run_script(self.handoff(code, wifi="keep"), [code, "yes"], device)
        self.assertEqual(status, 1)
        blob = "\n".join(said)
        self.assertIn("unexpected error (RuntimeError in ", blob)
        self.assertRegex(blob, r"line \d+\)")
        self.assertNotIn("injected USB failure", blob)
        self.assertIn("kept", blob)

    def test_a_device_error_says_what_it_is_instead_of_calling_it_unexpected(self):
        module = rendered_module()
        module.list_ports = lambda: [("COM3", "USB Serial Device")]
        message = "A file could not be written to the Presto. Reconnect it and run this script again."

        class Failing(FakeDevice):
            def write(self, path, data):
                raise module.DeviceError(message)

        code = device_setup.new_unlock_code()
        path = self.handoff(code, wifi="keep")
        said = []
        answers = [code, "yes"]
        status = module.main(["--handoff", str(path)], device_factory=lambda port: Failing(existing_install(), device_id="a" * 32),
                             ask=lambda text, secret=False: answers.pop(0), say=said.append)
        self.assertEqual(status, 1)
        self.assertIn(message, said)
        self.assertFalse(any("unexpected error" in text for text in said))


class PreservationTests(ScriptCase):
    def test_a_presto_with_settings_keeps_all_of_them_but_the_daemon(self):
        code = device_setup.new_unlock_code()
        path = self.handoff(code, wifi="keep")
        files = existing_install()
        files[CONFIG] = (b"# my settings\nLED_BRIGHTNESS = 0.8\nDAEMON_HOST = '10.0.0.2'  # old\n"
                         b"DEVICE_ID = '" + b"a" * 32 + b"'\nSOUND_ENABLED = False\n")
        device = FakeDevice(files, device_id="a" * 32, name="old-name")
        before = dict(device.files)
        status, said = self.run_script(path, [code, "yes"], device)
        self.assertEqual(status, 0, said)
        written = device.files[CONFIG].decode("ascii")
        for kept_line in ("# my settings", "LED_BRIGHTNESS = 0.8", "SOUND_ENABLED = False",
                          "DEVICE_ID = '%s'" % ("a" * 32)):
            self.assertIn(kept_line, written)
        self.assertIn("DAEMON_HOST = '192.168.1.50'", written)
        self.assertIn("DAEMON_PORT = 8787", written)
        self.assertIn("SHARED_TOKEN = '%s'" % TOKEN, written)
        self.assertNotIn("10.0.0.2", written)
        self.assertEqual(device.files[SECRETS], before[SECRETS])
        self.assertEqual(device.files["/main.py"], before["/main.py"])
        self.assertIn("Every other setting in it stays.", "\n".join(said))
        for target, data in BUNDLE["files"].items():
            self.assertEqual(device.files[target], data)
        self.assertNotIn("/ai_dash_files/old_helper.py", device.files)

    def test_the_identifier_and_name_already_in_the_file_are_kept_once(self):
        code = device_setup.new_unlock_code()
        path = self.handoff(code, wifi="keep")
        files = existing_install()
        files[CONFIG] = (b"DAEMON_HOST = '10.0.0.2'\nDEVICE_NAME = 'desk'\n"
                         b"DEVICE_ID = '" + b"b" * 32 + b"'\n")
        device = FakeDevice(files, device_id="b" * 32, name="desk")
        status, said = self.run_script(path, [code, "yes"], device)
        self.assertEqual(status, 0, said)
        written = device.files[CONFIG].decode("ascii")
        self.assertEqual(written.count("DEVICE_ID"), 1)
        self.assertEqual(written.count("DEVICE_NAME"), 1)
        self.assertIn("DEVICE_ID = '%s'" % ("b" * 32), written)

    def test_a_missing_identifier_is_added_to_the_existing_file(self):
        code = device_setup.new_unlock_code()
        path = self.handoff(code, wifi="keep")
        files = existing_install()
        files[CONFIG] = b"DAEMON_HOST = '10.0.0.2'\nLED_BRIGHTNESS = 0.8\n"
        device = FakeDevice(files)
        status, said = self.run_script(path, [code, "yes"], device)
        self.assertEqual(status, 0, said)
        written = device.files[CONFIG].decode("ascii")
        self.assertIn("LED_BRIGHTNESS = 0.8", written)
        self.assertEqual(len(re.findall(r"DEVICE_ID = '[0-9a-f]{32}'", written)), 1)

    def test_a_settings_file_of_an_unusual_shape_is_rewritten_and_the_summary_says_so(self):
        code = device_setup.new_unlock_code()
        path = self.handoff(code, wifi="keep")
        files = existing_install()
        files[CONFIG] = b"DAEMON_HOST = 'a'\nDAEMON_HOST = 'b'\n"
        device = FakeDevice(files, device_id="a" * 32)
        status, said = self.run_script(path, [code, "yes"], device)
        self.assertEqual(status, 0, said)
        self.assertIn("does not have the usual shape", "\n".join(said))
        self.assertIn("DAEMON_HOST = '192.168.1.50'", device.files[CONFIG].decode("ascii"))

    def test_keeping_the_wifi_asks_nothing_and_leaves_the_file_alone(self):
        code = device_setup.new_unlock_code()
        path = self.handoff(code, wifi="keep")
        device = FakeDevice(existing_install(), device_id="a" * 32, name="old-name")
        before = dict(device.files)
        status, said = self.run_script(path, [code, "yes"], device)
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
        status, said = self.run_script(path, [code], device)
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
        status, said = self.run_script(path, [code, "Office 2G", "new-wifi-pass", "yes"],
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
        status, said = self.run_script(path, [code, "n", "yes"], device)
        self.assertEqual(status, 0, said)
        self.assertTrue(any("Replace the Wi-Fi settings" in text for text in said))

    def test_an_unrelated_file_on_a_fresh_device_is_left_alone(self):
        code = device_setup.new_unlock_code()
        path = self.handoff(code)
        device = FakeDevice({"/ai_dash_files/my_notes.py": b"mine"})
        status, said = self.run_script(path, [code, "net", "pw", "yes"], device)
        self.assertEqual(status, 0, said)
        self.assertEqual(device.files["/ai_dash_files/my_notes.py"], b"mine")


class FailureAndRetryTests(ScriptCase):
    def test_an_interrupted_transfer_rolls_back_and_keeps_the_handoff(self):
        code = device_setup.new_unlock_code()
        path = self.handoff(code, wifi="keep")
        device = FakeDevice(existing_install(), device_id="a" * 32, name="x",
                            fail_write="/ai_dash_files/ui.py.presto-new")
        before = dict(device.files)
        status, said = self.run_script(path, [code, "yes"], device)
        self.assertTrue(any("Continue? [Y/n]" in text for text in said), said)
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
        status, said = self.run_script(path, [code, "yes"], device)
        self.assertTrue(any("Continue? [Y/n]" in text for text in said), said)
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
        status, said = self.run_script(path, [code, "yes"], device)
        self.assertTrue(any("Continue? [Y/n]" in text for text in said), said)
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
        status, said = self.run_script(path, [code, "yes"], device)
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
                status, said = self.run_script(path, [code, "net", "pw", "yes"], device)
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
        self.assertEqual(top_level, {"argparse", "ast", "base64", "binascii", "getpass",
                                     "hashlib", "importlib", "json", "os", "re", "shutil",
                                     "subprocess", "sys", "tempfile", "time", "traceback"})


class PrivateEnvironmentTests(ScriptCase):
    """Missing packages are installed into a temporary environment, with consent."""

    def setUp(self):
        super().setUp()
        self.module = rendered_module()
        self.env_folder = tempfile.mkdtemp(prefix="presto-test-env-")
        self.addCleanup(shutil.rmtree, self.env_folder, True)

    def run_main(self, answers, args=("--port", "COM9"), run=None, call=None, env=None):
        said, asked = [], []

        def ask(text, secret=False):
            asked.append(text)
            if not answers:
                raise AssertionError("unexpected prompt: " + text)
            return answers.pop(0)

        run = run or unittest.mock.MagicMock()
        call = call or unittest.mock.MagicMock(return_value=0)
        started = unittest.mock.patch.dict(os.environ, env or {})
        with started, \
                unittest.mock.patch.object(self.module.importlib.util, "find_spec",
                                           side_effect=lambda name, *a: None), \
                unittest.mock.patch.object(self.module.tempfile, "mkdtemp",
                                           return_value=self.env_folder), \
                unittest.mock.patch.object(self.module.subprocess, "run", run), \
                unittest.mock.patch.object(self.module.subprocess, "call", call):
            status = self.module.main(list(args), ask=ask, say=said.append)
        return status, said, asked, run, call

    def test_it_builds_a_private_environment_and_runs_itself_inside_it(self):
        status, said, asked, run, call = self.run_main([""], call=unittest.mock.MagicMock(return_value=0))
        self.assertEqual(status, 0)
        self.assertEqual(len(asked), 1)
        self.assertIn("private, temporary environment", asked[0])
        venv, pip = run.call_args_list[0].args[0], run.call_args_list[1].args[0]
        self.assertEqual(venv[1:], ["-m", "venv", self.env_folder])
        self.assertEqual(pip[1:4], ["-m", "pip", "install"])
        for package in self.module.REQUIRED_PACKAGES:
            self.assertIn(package, pip)
        command = call.call_args.args[0]
        self.assertEqual(command[0], self.module.private_environment_python(self.env_folder))
        self.assertEqual(command[1], os.path.abspath(self.module.__file__))
        self.assertEqual(command[2:], ["--port", "COM9"], "the same arguments, unchanged")
        self.assertEqual(call.call_args.kwargs["env"][self.module.BOOTSTRAP_ENV], "1")
        self.assertFalse(os.path.exists(self.env_folder), "the environment is deleted")

    def test_the_exit_code_of_the_inner_run_is_the_scripts_own(self):
        status, *_rest = self.run_main(["y"], call=unittest.mock.MagicMock(return_value=2))
        self.assertEqual(status, 2)

    def test_declining_installs_nothing_and_gives_the_manual_instructions(self):
        for answer in ("n", "no", "x"):
            with self.subTest(answer=answer):
                status, said, _asked, run, call = self.run_main([answer])
                self.assertEqual(status, 1)
                run.assert_not_called()
                call.assert_not_called()
                self.assertIn("pip install", "\n".join(said))

    def test_no_terminal_to_answer_on_counts_as_no(self):
        asked = []

        def ask(text, secret=False):
            asked.append(text)
            raise EOFError()

        with unittest.mock.patch.object(self.module.importlib.util, "find_spec",
                                        side_effect=lambda name, *a: None), \
                unittest.mock.patch.object(self.module.subprocess, "run") as run:
            said = []
            status = self.module.main(["--port", "COM9"], ask=ask, say=said.append)
        self.assertEqual(status, 1)
        run.assert_not_called()
        self.assertIn("pip install", "\n".join(said))

    def test_a_failed_install_is_explained_and_cleaned_up(self):
        run = unittest.mock.MagicMock(side_effect=subprocess.CalledProcessError(1, "pip"))
        status, said, _asked, _run, call = self.run_main([""], run=run)
        self.assertEqual(status, 1)
        call.assert_not_called()
        self.assertIn("could not be installed automatically", "\n".join(said))
        self.assertIn("pip install", "\n".join(said))
        self.assertFalse(os.path.exists(self.env_folder))

    def test_a_private_environment_that_still_lacks_them_does_not_build_another(self):
        status, said, asked, run, call = self.run_main(
            [], env={self.module.BOOTSTRAP_ENV: "1"})
        self.assertEqual(status, 1)
        self.assertEqual(asked, [])
        run.assert_not_called()
        call.assert_not_called()

    def test_the_inline_metadata_names_the_same_packages(self):
        # `pipx run presto_setup.py` and `uv run presto_setup.py` read this block.
        source = module_source()
        match = re.search(r"(?m)^# /// script\n((?:#.*\n)+?)# ///$", source)
        self.assertIsNotNone(match)
        text = "".join(line[2:] if line.startswith("# ") else line[1:] + "\n"
                       for line in match.group(1).splitlines(True))
        import tomllib
        declared = tomllib.loads(text)["dependencies"]
        self.assertEqual(declared, list(self.module.REQUIRED_PACKAGES))
        for package in declared:
            self.assertIn(package, self.module.DEPENDENCY_MESSAGE)


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


class SlotDeviceTests(ScriptCase):
    """A Presto that updates over the air keeps its app: only its settings change."""

    def test_a_slot_presto_gets_settings_only_and_keeps_every_app_file(self):
        code = device_setup.new_unlock_code()
        path = self.handoff(code, wifi="keep")
        device = FakeDevice(slot_install(), device_id="a" * 32, name="desk")
        before = dict(device.files)
        status, said = self.run_script(path, [code, "yes"], device)
        self.assertEqual(status, 0, said)
        for target, data in before.items():
            if target != CONFIG:
                self.assertEqual(device.files[target], data, target)
        self.assertEqual(sorted(set(device.files) - set(before)), [])
        written = device.files[CONFIG].decode("ascii")
        self.assertIn("DAEMON_HOST = '192.168.1.50'", written)
        self.assertIn("DEVICE_ID = '%s'" % ("a" * 32), written)
        self.assertIn("DEVICE_NAME = 'desk'", written)
        self.assertTrue(all(target.startswith(CONFIG) for target in device.writes), device.writes)
        self.assertTrue(device.launched)
        self.assertFalse(path.exists(), "the handoff is removed once the Presto reached the daemon")
        blob = "\n".join(said)
        self.assertIn("over the air", blob)
        self.assertNotIn("app files,", blob)
        self.assertIn("/secrets.py: keep the existing file", blob)

    def test_the_board_goes_back_to_the_launcher_before_anything_else_happens(self):
        # A running slot app's watchdog would reset the board mid-session.
        code = device_setup.new_unlock_code()
        device = FakeDevice(slot_install(), device_id="a" * 32, name="desk")
        status, said = self.run_script(self.handoff(code, wifi="keep"), [code, "yes"], device)
        self.assertEqual(status, 0, said)
        self.assertEqual(device.resets, [0], "reset once, before the first write")
        self.assertLess(device.log.index("reset"), device.log.index("config_state"))

    def test_the_wifi_box_still_decides_on_a_slot_presto(self):
        code = device_setup.new_unlock_code()
        device = FakeDevice(slot_install(), device_id="a" * 32, name="desk")
        status, said = self.run_script(self.handoff(code, wifi="set"),
                                       [code, "Office 2G", "new-wifi-pass", "yes"], device)
        self.assertEqual(status, 0, said)
        self.assertIn("WIFI_SSID = 'Office 2G'", device.files[SECRETS].decode("ascii"))
        self.assertEqual(device.files["/ai_dash_slots/a/ai_dash_app.py"], b"slot a app")
        files = slot_install()
        del files[SECRETS]
        empty = FakeDevice(files, device_id="a" * 32, name="desk")
        status, said = self.run_script(self.handoff(code, wifi="keep"), [code], empty)
        self.assertEqual(status, 1)
        self.assertEqual(empty.writes, [])
        self.assertIn("has no Wi-Fi settings", "\n".join(said))

    def test_a_daemon_it_cannot_reach_is_reported_and_the_app_is_still_started(self):
        code = device_setup.new_unlock_code()
        path = self.handoff(code, wifi="keep")
        device = FakeDevice(slot_install(), device_id="a" * 32, name="desk", reach="error")
        status, said = self.run_script(path, [code, "yes"], device)
        self.assertEqual(status, 1)
        self.assertTrue(device.launched, "the app is started so the screen can say why")
        self.assertTrue(path.exists(), "the handoff is kept for another try")
        self.assertTrue(any("could not reach" in text for text in said), said)

    def test_a_single_folder_presto_is_never_reset_and_still_gets_its_app(self):
        code = device_setup.new_unlock_code()
        device = FakeDevice(existing_install(), device_id="a" * 32, name="x")
        status, said = self.run_script(self.handoff(code, wifi="keep"), [code, "yes"], device)
        self.assertEqual(status, 0, said)
        self.assertEqual(device.resets, [])
        for target, data in BUNDLE["files"].items():
            self.assertEqual(device.files[target], data)


class ReconnectTests(unittest.TestCase):
    """MpremoteDevice.reset_to_launcher, with a transport that returns after a delay."""

    def make(self, failures, attempts=30):
        module = rendered_module()
        made = []

        class Transport:
            def __init__(self, port):
                if made and failures[0] > 0:
                    failures[0] -= 1
                    raise OSError("not back yet")
                self.port = port
                self.sent = []
                self.closed = False
                made.append(self)

            def enter_raw_repl(self):
                pass

            def exec_raw_no_follow(self, code):
                self.sent.append(bytes(code))

            def exit_raw_repl(self):
                pass

            def close(self):
                self.closed = True

        fake = types.ModuleType("mpremote.transport_serial")
        fake.SerialTransport = Transport
        package = types.ModuleType("mpremote")
        module.RECONNECT_ATTEMPTS = attempts
        patches = unittest.mock.patch.dict(sys.modules, {"mpremote": package,
                                                         "mpremote.transport_serial": fake})
        patches.start()
        self.addCleanup(patches.stop)
        sleep = unittest.mock.patch.object(module.time, "sleep")
        sleep.start()
        self.addCleanup(sleep.stop)
        return module, made

    def test_it_sends_the_reset_and_reconnects_when_the_board_returns(self):
        module, made = self.make([2])
        device = module.MpremoteDevice("COM9")
        first = device.transport
        device.reset_to_launcher()
        self.assertIn(b"machine.reset()", first.sent[0])
        self.assertTrue(first.closed)
        self.assertEqual(len(made), 2, "the old session and one new one; two attempts failed")
        self.assertIs(device.transport, made[-1])
        self.assertEqual(device.transport.port, "COM9")

    def test_a_board_that_never_returns_is_reported(self):
        module, _made = self.make([99], attempts=3)
        device = module.MpremoteDevice("COM9")
        with self.assertRaises(module.DeviceError) as caught:
            device.reset_to_launcher()
        self.assertIn("did not come back", str(caught.exception))

    def test_it_leaves_the_board_alone_while_the_launcher_finishes_starting(self):
        # Taking the board over a second after a hard reset left the radio unable to
        # join any network until the next hard reset (found on hardware).
        module, _made = self.make([0])
        waits = []
        module.time.sleep.side_effect = waits.append
        device = module.MpremoteDevice("COM9")
        device.reset_to_launcher()
        self.assertGreaterEqual(module.RESET_SETTLE_SECONDS, 5)
        self.assertEqual(waits[0], module.RESET_SETTLE_SECONDS,
                         "the first wait after the reset is the settle time")

    def test_the_reach_check_forgets_the_settings_read_earlier_in_the_session(self):
        # The setup reads the old settings with `import config` in the same session, so
        # an unchanged check would test the old daemon address, not the one just written.
        module = rendered_module()
        code = module.REACH_CODE
        self.assertLess(code.index("sys.modules.pop"), code.index("import config"))
        for name in ("config", "secrets"):
            self.assertIn("'%s'" % name, code[:code.index("import config")])

    def test_launch_picks_the_layout_on_the_device(self):
        module, made = self.make([0])
        device = module.MpremoteDevice("COM9")
        device.launch()
        sent = made[0].sent[-1].decode("ascii")
        self.assertIn("boot.main()", sent)
        self.assertIn("ai_dash_app import main", sent)
        compile(sent, "launch", "exec")


if __name__ == "__main__":
    unittest.main()
