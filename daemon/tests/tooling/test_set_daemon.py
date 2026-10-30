"""Pointing a set-up Presto at a different daemon over USB.

The device scripts are the real ones: they run here under CPython on a
temporary folder standing in for the Presto's filesystem, so the rewrite, the
safety checks and the rollback are exercised, not described. Only the radio
(the reach check) and the USB transport are replaced.
"""

import contextlib
import io
import json
from pathlib import Path
import re
import sys
import tempfile
import unittest
from unittest.mock import patch

import presto_path

TOOLS = Path(presto_path.REPO_ROOT) / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

from presto_ai_dash import app, device_install  # noqa: E402

OLD_TOKEN = "old-token-0123456789abcdef"
NEW_TOKEN = "new-token-9876543210-fedcba"
ORIGINAL = (
    "# Presto AI Dash device settings.\n"
    "# Private: this file holds the shared device token.\n"
    "\n"
    "DAEMON_HOST = '10.0.0.2'\n"
    "DAEMON_PORT = 8787\n"
    "SHARED_TOKEN = '" + OLD_TOKEN + "'\n"
    "\n"
    "DEVICE_NAME = 'desk'\n"
    "DEVICE_ID = '" + "a" * 32 + "'\n"
    "POLL_INTERVAL = 15\n")
WIFI_ORIGINAL = (
    "# Presto AI Dash Wi-Fi credentials.\n"
    "WIFI_SSID = 'Home 2G'\n"
    "WIFI_PASSWORD = 'old-wifi-password'\n")
NEW_WIFI_PASSWORD = "new-wifi-password-42"
GUARD = re.compile(r"if sys\.implementation\.name.*\n    raise RuntimeError\(.*\)\n")


class FakePresto:
    """A USB transport that runs the device scripts against a folder."""

    def __init__(self, root, config=ORIGINAL, reach="ok", boot=False, wifi=WIFI_ORIGINAL,
                 joined=None):
        self.root = Path(root)
        self.folder = self.root / "ai_dash_files"
        self.folder.mkdir(parents=True)
        if config is not None:
            (self.folder / "config.py").write_bytes(config.encode("utf-8"))
        if boot:
            (self.root / "ai_dash_boot.py").write_bytes(b"boot")
        if wifi is not None:
            (self.root / "secrets.py").write_bytes(wifi.encode("utf-8"))
        self.joined = joined
        self.reach = reach
        self.calls = []
        self.scripts = []
        self.launches = []
        self.before_script = None

    @property
    def config(self):
        return (self.folder / "config.py").read_bytes()

    @property
    def wifi(self):
        return (self.root / "secrets.py").read_bytes()

    def __call__(self, port, arguments):
        self.calls.append(list(arguments))
        if arguments[:2] == ["exec", "--no-follow"] or arguments == ["exec", device_install.READY_PROBE]:
            return ""
        if arguments[:3] == ["soft-reset", "exec", "--no-follow"]:
            self.launches.append(arguments[3])
            return ""
        assert arguments[:2] == ["soft-reset", "run"], arguments
        text = Path(arguments[2]).read_text(encoding="utf-8")
        if "network.WLAN" in text:
            self.scripts.append("reach")
            joined = self.reach != "wifi" if self.joined is None else self.joined
            return device_install.MARKER + json.dumps({"result": self.reach, "joined": joined})
        self.scripts.append("apply" if "def apply" in text else "settle")
        if self.before_script:
            self.before_script(self.scripts[-1])
        text = GUARD.sub("", text).replace("/ai_dash_files", (self.root / "ai_dash_files").as_posix())
        text = text.replace("/ai_dash_boot.py", (self.root / "ai_dash_boot.py").as_posix())
        text = text.replace("'/secrets.py'", repr((self.root / "secrets.py").as_posix()))
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            exec(compile(text, "device_script.py", "exec"), {})
        return output.getvalue()


class SetDaemonCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.receipt = Path(self.temp.name) / "state" / "receipt.json"

    def presto(self, **options):
        self.count = getattr(self, "count", 0) + 1
        return FakePresto(Path(self.temp.name) / ("presto%d" % self.count), **options)

    def set_daemon(self, presto, host="192.168.1.50", port=8787, token=None, verify=True):
        return device_install.set_daemon_operation(
            "TESTPORT", host, port, token, verify=verify, remote=presto, receipt=self.receipt)


class ChangeTests(SetDaemonCase):
    def test_only_the_address_changes_and_the_rest_is_kept_byte_for_byte(self):
        presto = self.presto()
        result = self.set_daemon(presto, "192.168.1.50", 9000)
        expected = ORIGINAL.replace("'10.0.0.2'", "'192.168.1.50'").replace("8787", "9000")
        self.assertEqual(presto.config.decode(), expected)
        self.assertEqual(result["reach"], "ok")
        self.assertFalse(result["token_replaced"])

    def test_the_token_changes_only_when_one_is_given(self):
        presto = self.presto()
        self.set_daemon(presto, token=NEW_TOKEN)
        text = presto.config.decode()
        self.assertIn("SHARED_TOKEN = '%s'\n" % NEW_TOKEN, text)
        self.assertNotIn(OLD_TOKEN, text)
        self.assertIn("DEVICE_ID = '%s'" % ("a" * 32), text)
        kept = self.presto_again()
        self.set_daemon(kept)
        self.assertIn(OLD_TOKEN, kept.config.decode())

    def presto_again(self):
        self.temp2 = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp2.cleanup)
        return FakePresto(self.temp2.name)

    def test_windows_line_endings_are_kept(self):
        presto = self.presto(config=ORIGINAL.replace("\n", "\r\n"))
        self.set_daemon(presto)
        data = presto.config
        self.assertEqual(data.count(b"\r\n"), ORIGINAL.count("\n"))
        self.assertNotIn(b"\n", data.replace(b"\r\n", b""))
        self.assertIn(b"DAEMON_HOST = '192.168.1.50'\r\n", data)

    def test_no_stage_or_backup_file_is_left_behind(self):
        presto = self.presto()
        self.set_daemon(presto)
        self.assertEqual(sorted(path.name for path in presto.folder.iterdir()), ["config.py"])

    def test_the_usb_session_starts_from_the_launcher_and_ends_by_relaunching(self):
        presto = self.presto()
        self.set_daemon(presto)
        self.assertEqual(presto.calls[0], ["exec", "--no-follow", device_install.RESET_TO_LAUNCHER])
        self.assertEqual(presto.scripts, ["apply", "reach", "settle"])
        self.assertEqual(presto.launches, [device_install.LAUNCH_AUTO])

    def test_the_token_never_appears_in_anything_the_tool_prints_or_returns(self):
        presto = self.presto()
        out, err = io.StringIO(), io.StringIO()
        with patch.dict("os.environ", {"NEW_PRESTO_TOKEN": NEW_TOKEN}), \
                patch("importlib.util.find_spec", return_value=object()):
            code = device_install.set_daemon_main(
                ["--port", "TESTPORT", "--daemon-host", "192.168.1.50",
                 "--token-env", "NEW_PRESTO_TOKEN"],
                receipt=self.receipt, remote=presto, out=out, err=err)
        self.assertEqual(code, 0, err.getvalue())
        for stream in (out.getvalue(), err.getvalue()):
            self.assertNotIn(NEW_TOKEN, stream)
            self.assertNotIn(OLD_TOKEN, stream)
        self.assertIn("Pointed the Presto at 192.168.1.50:8787", out.getvalue())
        self.assertIn("token was replaced", out.getvalue())
        self.assertIn("SHARED_TOKEN = '%s'" % NEW_TOKEN, presto.config.decode())

    def test_the_relaunch_picks_the_layout_on_the_device(self):
        # One selector runs on the Presto, so the host never guesses the layout.
        for boot in (False, True):
            with self.subTest(boot=boot):
                presto = self.presto_with(boot)
                self.set_daemon(presto)
                self.assertEqual(presto.launches, [device_install.LAUNCH_AUTO])
        compile(device_install.LAUNCH_AUTO, "launch", "exec")
        self.assertIn("boot.main()", device_install.LAUNCH_AUTO)
        self.assertIn("ai_dash_app import main", device_install.LAUNCH_AUTO)

    def presto_with(self, boot):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        return FakePresto(folder.name, boot=boot)

    def test_no_reach_check_skips_the_radio(self):
        presto = self.presto(reach="error")
        result = self.set_daemon(presto, verify=False)
        self.assertEqual(presto.scripts, ["apply", "settle"])
        self.assertEqual(result["reach"], "skipped")
        self.assertIn("'192.168.1.50'", presto.config.decode())


class RollbackTests(SetDaemonCase):
    def test_a_daemon_the_device_cannot_reach_puts_the_old_settings_back(self):
        for reach, words in (("error", "could not reach a daemon"),
                             ("wifi", "could not join the Wi-Fi"),
                             ("unauthorised", "refused its device token"),
                             ("http_404", "did not answer as the daemon")):
            with self.subTest(reach=reach):
                presto = self.presto_fresh(reach)
                with self.assertRaises(RuntimeError) as caught:
                    self.set_daemon(presto, "192.168.1.77", 9001, token=NEW_TOKEN)
                self.assertEqual(presto.config.decode(), ORIGINAL)
                self.assertIn(words, str(caught.exception))
                self.assertIn("previous settings were put back", str(caught.exception))
                self.assertNotIn(NEW_TOKEN, str(caught.exception))
                self.assertEqual(sorted(path.name for path in presto.folder.iterdir()),
                                 ["config.py"])
                self.assertEqual(len(presto.launches), 1)

    def presto_fresh(self, reach):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        return FakePresto(folder.name, reach=reach)

    def test_a_failed_restore_says_where_the_old_file_is(self):
        presto = self.presto(reach="error")

        def lose_backup(kind):
            if kind == "settle":
                (presto.folder / "config.py.presto-prev").unlink()

        presto.before_script = lose_backup
        with self.assertRaisesRegex(RuntimeError, "config.py.presto-prev"):
            self.set_daemon(presto)

    def test_an_interrupted_earlier_run_is_undone_before_the_next_one_starts(self):
        presto = self.presto()
        # What a cut left behind: the original parked, an unverified file in place.
        (presto.folder / "config.py.presto-prev").write_bytes(ORIGINAL.encode())
        (presto.folder / "config.py").write_bytes(b"DAEMON_HOST = 'junk'\n")
        self.set_daemon(presto)
        text = presto.config.decode()
        self.assertIn("DEVICE_ID = '%s'" % ("a" * 32), text)
        self.assertIn("DAEMON_HOST = '192.168.1.50'", text)
        self.assertNotIn("junk", text)
        self.assertEqual(sorted(path.name for path in presto.folder.iterdir()), ["config.py"])

    def test_a_lost_config_is_recovered_from_the_parked_original(self):
        presto = self.presto(config=None)
        (presto.folder / "config.py.presto-prev").write_bytes(ORIGINAL.encode())
        self.set_daemon(presto)
        self.assertIn("DEVICE_ID = '%s'" % ("a" * 32), presto.config.decode())


class WifiTests(SetDaemonCase):
    def set_wifi(self, presto, ssid="Office 2G", password=NEW_WIFI_PASSWORD, verify=True):
        return device_install.set_wifi_operation(
            "TESTPORT", ssid, password, verify=verify, remote=presto, receipt=self.receipt)

    def test_only_the_two_wifi_lines_change(self):
        presto = self.presto()
        config = presto.config
        result = self.set_wifi(presto)
        self.assertEqual(presto.wifi.decode(), WIFI_ORIGINAL.replace("Home 2G", "Office 2G")
                         .replace("old-wifi-password", NEW_WIFI_PASSWORD))
        self.assertEqual(presto.config, config, "the daemon settings are never touched")
        self.assertTrue(result["joined"])
        self.assertEqual(result["daemon"], "ok")
        self.assertEqual(presto.scripts, ["apply", "reach", "settle"])

    def test_a_network_it_cannot_join_puts_the_old_wifi_back(self):
        presto = self.presto(reach="wifi")
        with self.assertRaises(RuntimeError) as caught:
            self.set_wifi(presto)
        self.assertEqual(presto.wifi.decode(), WIFI_ORIGINAL)
        self.assertIn("could not join that Wi-Fi network", str(caught.exception))
        self.assertIn("previous settings were put back", str(caught.exception))
        self.assertNotIn(NEW_WIFI_PASSWORD, str(caught.exception))
        self.assertEqual(sorted(path.name for path in presto.root.iterdir()),
                         ["ai_dash_files", "secrets.py"])
        self.assertEqual(len(presto.launches), 1)

    def test_a_network_it_joins_is_kept_even_if_the_daemon_is_not_reachable_from_it(self):
        for code in ("error", "unauthorised", "http_404"):
            with self.subTest(daemon=code):
                presto = self.presto(reach=code, joined=True)
                result = self.set_wifi(presto)
                self.assertIn("Office 2G", presto.wifi.decode())
                self.assertEqual(result["daemon"], code)
                self.assertTrue(result["joined"])

    def test_an_open_network_has_an_empty_password(self):
        presto = self.presto()
        result = self.set_wifi(presto, "Cafe", "")
        self.assertIn("WIFI_PASSWORD = ''", presto.wifi.decode())
        self.assertTrue(result["open"])

    def test_awkward_passwords_survive_the_round_trip(self):
        for password in ("with spaces and 'quotes'", 'double " and back\\slash\\n',
                         "#hash = equals", "\\x41" * 4):
            with self.subTest(password=password):
                presto = self.presto()
                self.set_wifi(presto, "Odd 'name'", password)
                space = {}
                exec(presto.wifi.decode(), space)
                self.assertEqual(space["WIFI_PASSWORD"], password)
                self.assertEqual(space["WIFI_SSID"], "Odd 'name'")

    def test_a_missing_secrets_file_is_created_and_removed_again_on_failure(self):
        presto = self.presto(wifi=None)
        self.set_wifi(presto)
        text = presto.wifi.decode()
        self.assertTrue(text.startswith("# Presto AI Dash Wi-Fi credentials."))
        self.assertIn("WIFI_SSID = 'Office 2G'", text)
        failing = self.presto(wifi=None, reach="wifi")
        with self.assertRaises(RuntimeError):
            self.set_wifi(failing)
        self.assertFalse((failing.root / "secrets.py").exists())
        self.assertEqual(sorted(path.name for path in failing.root.iterdir()), ["ai_dash_files"])

    def test_a_secrets_file_of_an_unexpected_shape_is_not_touched(self):
        shapes = {"duplicate": WIFI_ORIGINAL + "WIFI_SSID = 'again'\n",
                  "missing": "WIFI_SSID = 'only'\n",
                  "not python": WIFI_ORIGINAL + "X = (\n"}
        for label, text in shapes.items():
            with self.subTest(shape=label):
                presto = self.presto(wifi=text)
                with self.assertRaisesRegex(RuntimeError, "was not changed"):
                    self.set_wifi(presto)
                self.assertEqual(presto.wifi.decode(), text)

    def test_an_interrupted_earlier_run_is_undone_first(self):
        presto = self.presto()
        (presto.root / "secrets.py.presto-prev").write_bytes(WIFI_ORIGINAL.encode())
        (presto.root / "secrets.py").write_bytes(b"WIFI_SSID = 'junk'\nWIFI_PASSWORD = 'x'\n")
        self.set_wifi(presto)
        self.assertNotIn("junk", presto.wifi.decode())
        self.assertFalse((presto.root / "secrets.py.presto-prev").exists())

    def test_bad_credentials_stop_before_any_usb_traffic(self):
        cases = [("", "password123"), ("x" * 33, "password123"), ("caf\u00e9", "password123"),
                 ("tab\tname", "password123"), ("Net", "short77"), ("Net", "x" * 64),
                 ("Net", "p\u00e4ssword12"), (None, "password123"), ("Net", None)]
        for ssid, password in cases:
            with self.subTest(ssid=ssid, password=password):
                presto = self.presto()
                with self.assertRaises(ValueError) as caught:
                    self.set_wifi(presto, ssid, password)
                self.assertEqual(presto.calls, [])
                if isinstance(password, str) and password:
                    self.assertNotIn(password, str(caught.exception))

    def test_no_join_check_skips_the_radio(self):
        presto = self.presto(reach="wifi")
        result = self.set_wifi(presto, verify=False)
        self.assertEqual(presto.scripts, ["apply", "settle"])
        self.assertFalse(result["joined"])
        self.assertEqual(result["daemon"], "skipped")


class WifiCommandLineTests(SetDaemonCase):
    def run_main(self, arguments, presto=None, **options):
        out, err = io.StringIO(), io.StringIO()
        with patch("importlib.util.find_spec", return_value=object()):
            code = device_install.set_wifi_main(
                arguments, receipt=self.receipt, remote=presto or self.presto(), out=out, err=err,
                **options)
        return code, out.getvalue(), err.getvalue()

    def test_the_password_comes_from_the_environment_and_is_never_printed(self):
        presto = self.presto()
        with patch.dict("os.environ", {"WIFI_PW": NEW_WIFI_PASSWORD}):
            code, out, err = self.run_main(["--port", "COM3", "--ssid", "Office 2G",
                                            "--password-env", "WIFI_PW"], presto)
        self.assertEqual(code, 0, err)
        self.assertNotIn(NEW_WIFI_PASSWORD, out + err)
        self.assertNotIn("old-wifi-password", out + err)
        self.assertIn("Wi-Fi network set to Office 2G", out)
        self.assertIn("reached its daemon", out)
        self.assertIn(NEW_WIFI_PASSWORD, presto.wifi.decode())

    def test_without_a_variable_the_password_is_asked_for_hidden(self):
        asked = []
        presto = self.presto()
        code, out, err = self.run_main(
            ["--port", "COM3", "--ssid", "Office 2G"], presto, interactive=True,
            prompt=lambda text: asked.append(text) or NEW_WIFI_PASSWORD)
        self.assertEqual(code, 0, err)
        self.assertEqual(len(asked), 1)
        self.assertNotIn(NEW_WIFI_PASSWORD, out + err + asked[0])

    def test_with_no_terminal_and_no_variable_it_refuses_rather_than_hang(self):
        code, _out, err = self.run_main(["--port", "COM3", "--ssid", "Office 2G"],
                                        interactive=False, prompt=lambda text: self.fail("asked"))
        self.assertEqual(code, 2)
        self.assertIn("--password-env", err)

    def test_open_and_a_password_variable_are_exclusive(self):
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            device_install.set_wifi_main(["--port", "COM3", "--ssid", "x", "--open",
                                          "--password-env", "A"])

    def test_open_sets_no_password(self):
        presto = self.presto()
        code, _out, _err = self.run_main(["--port", "COM3", "--ssid", "Cafe", "--open"], presto)
        self.assertEqual(code, 0)
        self.assertIn("WIFI_PASSWORD = ''", presto.wifi.decode())

    def test_a_network_that_cannot_be_joined_exits_two_and_keeps_the_old_one(self):
        presto = self.presto(reach="wifi")
        code, out, err = self.run_main(["--port", "COM3", "--ssid", "Nowhere", "--open"], presto)
        self.assertEqual(code, 2)
        self.assertIn("could not join", err)
        self.assertEqual(out, "")
        self.assertEqual(presto.wifi.decode(), WIFI_ORIGINAL)

    def test_joined_but_daemon_elsewhere_points_at_set_daemon(self):
        presto = self.presto(reach="error", joined=True)
        code, out, _err = self.run_main(["--port", "COM3", "--ssid", "Office 2G", "--open"], presto)
        self.assertEqual(code, 0)
        self.assertIn("run set-daemon", out)

    def test_an_explicit_port_is_required(self):
        for arguments in (["--ssid", "x", "--open"], ["--port", "auto", "--ssid", "x", "--open"]):
            code, _out, err = self.run_main(arguments)
            self.assertEqual(code, 2)
            self.assertIn("explicit --port", err)

    def test_the_package_command_and_the_repository_tool_both_reach_it(self):
        import presto_device
        with patch.object(device_install, "set_wifi_main", return_value=0) as entry:
            self.assertEqual(app.main(["set-wifi", "--port", "COM3", "--ssid", "x"]), 0)
            self.assertEqual(entry.call_args.args[0], ["--port", "COM3", "--ssid", "x"])
            self.assertEqual(presto_device.main(["set-wifi", "--port", "COM3", "--ssid", "x"]), 0)
            self.assertEqual(entry.call_args.kwargs["receipt"], presto_device.RECEIPT)


class ScriptShapeTests(unittest.TestCase):
    def test_every_device_script_is_valid_python_and_prints_one_result_line(self):
        scripts = {
            "reach": device_install.HELPERS + device_install._DAEMON_REACH,
            "apply": device_install._daemon_script(device_install._DAEMON_APPLY, WANT={"DAEMON_HOST": "1.2.3.4"}),
            "settle": device_install._daemon_script(device_install._DAEMON_SETTLE, RESTORE=False, BEFORE=None),
        }
        for name, text in scripts.items():
            with self.subTest(script=name):
                compile(text, name, "exec")
                self.assertEqual(text.count("PRESTO_SYNC:"), 1)

    def test_the_reach_check_cycles_the_radio_and_bounds_every_wait(self):
        text = device_install._DAEMON_REACH
        self.assertIn("wlan.active(False)", text)
        self.assertIn("settimeout(8)", text)
        # The token is only ever read into the request, never printed.
        self.assertEqual(text.count("print("), 1)
        self.assertNotIn("SHARED_TOKEN)", text.replace("config.SHARED_TOKEN.encode()", ""))


class RefusalTests(SetDaemonCase):
    def test_a_device_without_settings_is_sent_to_setup(self):
        presto = self.presto(config=None)
        with self.assertRaisesRegex(RuntimeError, "Set up a Presto"):
            self.set_daemon(presto)
        self.assertEqual(list(presto.folder.iterdir()), [])

    def test_a_settings_file_that_is_not_the_expected_shape_is_not_touched(self):
        shapes = {
            "duplicate host": ORIGINAL + "DAEMON_HOST = '10.9.9.9'\n",
            "no port": ORIGINAL.replace("DAEMON_PORT = 8787\n", ""),
            "no host": ORIGINAL.replace("DAEMON_HOST = '10.0.0.2'\n", "# DAEMON_HOST = 'x'\n"),
            "not python": ORIGINAL + "DEVICE_NAME = (\n",
        }
        for label, text in shapes.items():
            with self.subTest(shape=label):
                folder = tempfile.TemporaryDirectory()
                self.addCleanup(folder.cleanup)
                presto = FakePresto(folder.name, config=text)
                with self.assertRaisesRegex(RuntimeError, "was not changed"):
                    self.set_daemon(presto)
                self.assertEqual(presto.config.decode(), text)
                self.assertEqual(presto.scripts, ["apply"])

    def test_a_token_is_required_to_exist_in_the_file_before_it_can_be_replaced(self):
        presto = self.presto(config=ORIGINAL.replace("SHARED_TOKEN", "OTHER_TOKEN"))
        with self.assertRaisesRegex(RuntimeError, "was not changed"):
            self.set_daemon(presto, token=NEW_TOKEN)

    def test_a_bad_address_stops_before_any_usb_traffic(self):
        for host in ("localhost", "printer.local", "127.0.0.1", "0.0.0.0", "224.0.0.1",
                     "256.1.1.1", "", "1.2.3", "::1", "192.168.1.50 "):
            with self.subTest(host=host):
                presto = self.presto_unused()
                with self.assertRaises(ValueError):
                    self.set_daemon(presto, host)
                self.assertEqual(presto.calls, [])
        for port in (0, -1, 65536, "8787", None, True):
            with self.subTest(port=port):
                presto = self.presto_unused()
                with self.assertRaises(ValueError):
                    self.set_daemon(presto, "192.168.1.50", port)
                self.assertEqual(presto.calls, [])

    def presto_unused(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        return FakePresto(folder.name)


class TokenSourceTests(unittest.TestCase):
    def test_a_good_token_is_returned(self):
        self.assertEqual(device_install.token_from_environment("X", {"X": NEW_TOKEN}), NEW_TOKEN)

    def test_refusals_never_repeat_the_value(self):
        secret = "short secret"
        for environ, name in (({}, "X"), ({"X": ""}, "X"), ({"X": secret}, "X"),
                              ({"X": "q7x-weak9"}, "X"), ({"X": "change-me"}, "X"),
                              ({"X": "aaaaaaaaaaaaaaaaaaaaaaaa"}, "X"),
                              ({"X": "tokén-0123456789abcdef"}, "X"),
                              ({"X": NEW_TOKEN}, "not a name"), ({"X": NEW_TOKEN}, "1X"),
                              ({"X": NEW_TOKEN}, None)):
            with self.subTest(name=name, value=environ.get("X")):
                with self.assertRaises(ValueError) as caught:
                    device_install.token_from_environment(name, environ)
                for value in environ.values():
                    if value:
                        self.assertNotIn(value, str(caught.exception))


class CommandLineTests(SetDaemonCase):
    def run_main(self, arguments, presto=None):
        out, err = io.StringIO(), io.StringIO()
        with patch("importlib.util.find_spec", return_value=object()):
            code = device_install.set_daemon_main(
                arguments, receipt=self.receipt, remote=presto or self.presto(), out=out, err=err)
        return code, out.getvalue(), err.getvalue()

    def test_an_explicit_port_is_required(self):
        for arguments in (["--daemon-host", "192.168.1.50"],
                          ["--port", "auto", "--daemon-host", "192.168.1.50"],
                          ["--port", "list", "--daemon-host", "192.168.1.50"]):
            with self.subTest(arguments=arguments):
                code, _out, err = self.run_main(arguments)
                self.assertEqual(code, 2)
                self.assertIn("explicit --port", err)

    def test_the_host_is_required(self):
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            device_install.set_daemon_main(["--port", "COM3"])

    def test_a_failure_exits_two_and_leaves_the_old_settings(self):
        presto = self.presto(reach="unauthorised")
        code, out, err = self.run_main(
            ["--port", "COM3", "--daemon-host", "192.168.1.50"], presto)
        self.assertEqual(code, 2)
        self.assertIn("refused its device token", err)
        self.assertEqual(presto.config.decode(), ORIGINAL)
        self.assertEqual(out, "")

    def test_the_default_port_is_8787(self):
        presto = self.presto()
        code, out, _err = self.run_main(["--port", "COM3", "--daemon-host", "192.168.1.50"], presto)
        self.assertEqual(code, 0)
        self.assertIn("192.168.1.50:8787", out)

    def test_the_package_command_and_the_repository_tool_both_reach_it(self):
        with patch.object(device_install, "set_daemon_main", return_value=0) as entry:
            self.assertEqual(app.main(["set-daemon", "--port", "COM3",
                                       "--daemon-host", "192.168.1.50"]), 0)
        self.assertEqual(entry.call_args.args[0], ["--port", "COM3", "--daemon-host", "192.168.1.50"])
        import presto_device
        with patch.object(device_install, "set_daemon_main", return_value=0) as entry:
            self.assertEqual(presto_device.main(["set-daemon", "--port", "COM3",
                                                 "--daemon-host", "192.168.1.50"]), 0)
        self.assertEqual(entry.call_args.args[0], ["--port", "COM3", "--daemon-host", "192.168.1.50"])
        self.assertEqual(entry.call_args.kwargs["receipt"], presto_device.RECEIPT)


class SlotDeviceGuardTests(unittest.TestCase):
    """The older single-folder installer must not run on a bootstrap device."""

    def run_install(self, layout):
        launches = []

        def remote(port, arguments):
            launches.append(arguments)
            return ""

        out, err = io.StringIO(), io.StringIO()
        with patch("importlib.util.find_spec", return_value=object()), \
                patch.object(device_install, "device_layout", return_value=layout), \
                patch.object(device_install, "run_remote", side_effect=remote), \
                patch.object(device_install, "operate", return_value=[]) as operate:
            code = device_install.run("sync", "COM3", {"revision": "a" * 64}, out=out, err=err)
        return code, err.getvalue(), operate, launches

    def test_a_bootstrap_device_is_refused_and_relaunched(self):
        code, err, operate, launches = self.run_install({"boot": True, "legacy": False})
        self.assertEqual(code, 2)
        self.assertIn("A/B app slots", err)
        operate.assert_not_called()
        self.assertEqual(launches, [["soft-reset", "exec", "--no-follow", device_install.LAUNCH_AUTO]])

    def test_a_legacy_device_goes_ahead_as_before(self):
        for layout in ({"boot": False, "legacy": True}, {"boot": True, "legacy": True}):
            with self.subTest(layout=layout):
                code, _err, operate, _launches = self.run_install(layout)
                self.assertEqual(code, 0)
                operate.assert_called_once()


if __name__ == "__main__":
    unittest.main()
