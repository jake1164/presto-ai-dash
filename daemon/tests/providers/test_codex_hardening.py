"""The Codex helper process: which program may be started, and with what environment.

The daemon starts the configured Codex executable on every poll. The admin page
can set it, so it must name Codex and not any program or a network share. The
program is also started without the daemon's own secrets: its tokens, the
master key and a provider key variable.
"""

import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from presto_ai_dash import child_env
from presto_ai_dash.codex_login import _LoginProcess
from presto_ai_dash.config import load_config
from presto_ai_dash.management import validate_settings
from presto_ai_dash.sources._codex_rpc import (CodexReportingError, ReportingSession,
                                               executable_setting_problem, find_executable)
from presto_ai_dash.sources.codex import CodexSource
from admin.test_management import ManagementBase

SECRETS = {"DASH_ENCRYPTION_KEY": "master-key-value", "DASH_ADMIN_TOKEN": "admin-token-value",
           "DASH_SHARED_TOKEN": "shared-token-value", "DASH_PORT": "8787",
           "LITELLM_API_KEY": "provider-key-value"}


def fake_process():
    return SimpleNamespace(stdout=io.BytesIO(b""), stdin=io.BytesIO(),
                           terminate=lambda: None, kill=lambda: None,
                           poll=lambda: 0, wait=lambda timeout=None: 0)


class ExecutableSettingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def test_the_names_codex_is_installed_under_are_accepted(self):
        absolute = str(Path(self.temp.name) / "codex.exe")
        for value in ("", "codex", "Codex.EXE", "codex.cmd", "codex.bat", "codex-linux-x64",
                      absolute, str(Path(self.temp.name) / "codex")):
            with self.subTest(value=value):
                self.assertIsNone(executable_setting_problem(value))

    def test_a_network_path_is_refused(self):
        for value in ("\\\\attacker\\share\\codex.exe", "//attacker/share/codex", "\\\\host\\c$\\codex.exe"):
            with self.subTest(value=value):
                self.assertIn("network share", executable_setting_problem(value))

    def test_any_other_program_is_refused(self):
        for value in ("whoami", "cmd.exe", "powershell", "python", "notcodex.exe", "mycodex",
                      "codex.ps1", "codex.com", "codex.dll", sys.executable):
            with self.subTest(value=value):
                self.assertIsNotNone(executable_setting_problem(value))

    def test_a_relative_path_is_refused(self):
        for value in ("tools/codex.exe", "..\\codex.exe", "./codex", "bin\\codex"):
            with self.subTest(value=value):
                self.assertIn("full path", executable_setting_problem(value))

    def test_the_polling_path_refuses_before_it_looks_at_the_disk(self):
        with patch("presto_ai_dash.sources._codex_rpc.shutil.which") as which:
            with self.assertRaises(CodexReportingError) as caught:
                find_executable(sys.executable)
        which.assert_not_called()
        self.assertIn("named codex", str(caught.exception))

    def test_saving_a_refused_executable_raises_a_value_error(self):
        with self.assertRaisesRegex(ValueError, "named codex"):
            validate_settings(CodexSource, {"executable": sys.executable})
        validate_settings(CodexSource, {"executable": ""})
        validate_settings(CodexSource, {"executable": "codex"})

    def test_a_stored_setting_that_is_now_refused_becomes_a_source_error_not_a_process(self):
        source = CodexSource({"executable": sys.executable})
        with patch("presto_ai_dash.sources._codex_rpc.subprocess.Popen") as popen:
            snapshot = source.collect()
        popen.assert_not_called()
        self.assertEqual(snapshot.status, "offline")
        self.assertIn("named codex", snapshot.error)

    def test_a_refused_executable_in_the_toml_is_a_configuration_error(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "dash.toml"
            path.write_text('[sources.codex]\nenabled = true\nexecutable = "whoami"\n',
                            encoding="utf-8")
            with patch.dict(os.environ):
                for name in list(os.environ):
                    if name.startswith("DASH_"):
                        del os.environ[name]
                with self.assertRaises(Exception) as caught:
                    load_config(path)
        self.assertIn("named codex", str(caught.exception))


class ExecutableSavedThroughTheAdminTests(ManagementBase):
    def test_the_manager_refuses_the_setting_and_stores_nothing(self):
        before = self.storage.source_instances()
        with self.assertRaisesRegex(ValueError, "named codex"):
            self.manager.save({"type": "codex", "name": "Codex",
                               "config": {"executable": sys.executable}})
        self.assertEqual(self.storage.source_instances(), before)

    def test_an_edit_that_swaps_in_another_program_is_refused(self):
        row = self.manager.save({"type": "codex", "name": "Codex", "config": {}})
        with self.assertRaisesRegex(ValueError, "network share"):
            self.manager.save({"revision": row["revision"],
                               "config": {"executable": "\\\\attacker\\share\\codex.exe"}},
                              row["id"])


class ChildEnvironmentTests(unittest.TestCase):
    def test_the_daemons_secrets_are_left_out_and_the_rest_is_kept(self):
        with patch.dict(os.environ, {**SECRETS, "Dash_Mixed_Case": "x", "PRESTO_TEST_KEEP": "kept"}):
            env = child_env.child_environment()
            everything_else = {name for name in os.environ
                               if not name.upper().startswith("DASH_") and name not in SECRETS}
        self.assertEqual([name for name in env
                          if name.upper().startswith("DASH_") or name in SECRETS], [])
        self.assertEqual(env["PRESTO_TEST_KEEP"], "kept")
        self.assertTrue(everything_else)
        self.assertEqual(everything_else, set(env))

    def test_a_variable_a_configuration_names_is_hidden_too(self):
        self.addCleanup(child_env._hidden.discard, "MY_PROVIDER_KEY")
        with patch.dict(os.environ, {"MY_PROVIDER_KEY": "secret-value"}):
            self.assertIn("MY_PROVIDER_KEY", child_env.child_environment())
            child_env.hide("my_provider_key")
            self.assertNotIn("MY_PROVIDER_KEY", child_env.child_environment())

    def test_loading_a_configuration_hides_its_provider_key_variable(self):
        self.addCleanup(child_env._hidden.discard, "ACME_GATEWAY_KEY")
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "dash.toml"
            path.write_text('[sources.litellm]\napi_key_env = "ACME_GATEWAY_KEY"\n', encoding="utf-8")
            with patch.dict(os.environ, {"ACME_GATEWAY_KEY": "gateway-key-value"}):
                for name in list(os.environ):
                    if name.startswith("DASH_"):
                        del os.environ[name]
                load_config(path)
                self.assertNotIn("ACME_GATEWAY_KEY", child_env.child_environment())

    def test_a_real_child_process_cannot_see_the_secrets_but_can_run(self):
        code = ("import os,sys;"
                "print(sorted(n for n in os.environ if n.startswith('DASH_') or n=='LITELLM_API_KEY'));"
                "print('PATH' in os.environ or 'Path' in os.environ)")
        with patch.dict(os.environ, SECRETS):
            result = subprocess.run([sys.executable, "-c", code], env=child_env.child_environment(),
                                    capture_output=True, text=True, timeout=30)
        lines = result.stdout.split("\n")
        self.assertEqual(lines[0], "[]", result.stderr)
        self.assertEqual(lines[1], "True")


class WhatTheCodexProcessesAreStartedWithTests(unittest.TestCase):
    def started_with(self, make):
        captured = {}

        def popen(argv, **kwargs):
            captured.update(kwargs, argv=argv)
            return fake_process()

        with patch.dict(os.environ, SECRETS), \
                patch("presto_ai_dash.sources._codex_rpc.subprocess.Popen", side_effect=popen), \
                patch("presto_ai_dash.codex_login.subprocess.Popen", side_effect=popen):
            made = make()
            made.closed.set()
        return captured

    def test_the_reporting_session_gets_no_daemon_secrets(self):
        captured = self.started_with(lambda: ReportingSession("codex", "", 5))
        for name in SECRETS:
            self.assertNotIn(name, captured["env"])
        self.assertEqual(captured["argv"][1:], ["app-server", "--listen", "stdio://"])

    def test_the_login_process_gets_no_daemon_secrets(self):
        captured = self.started_with(lambda: _LoginProcess("codex", "", 5))
        for name in SECRETS:
            self.assertNotIn(name, captured["env"])

    def test_the_profile_directory_is_still_passed_on(self):
        home = str(Path(tempfile.gettempdir()).resolve())
        captured = self.started_with(lambda: ReportingSession("codex", home, 5))
        self.assertEqual(captured["env"]["CODEX_HOME"], home)
        for name in SECRETS:
            self.assertNotIn(name, captured["env"])


if __name__ == "__main__":
    unittest.main()
