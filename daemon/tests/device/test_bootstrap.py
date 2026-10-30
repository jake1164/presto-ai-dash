"""The real bootstrap refuses unsafe entry and respects later confirmation."""
import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import presto_path
import activation
from device.test_activation import MemoryStore


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        path = Path(presto_path.PRESTO_DIR) / "bootstrap" / "boot.py"
        spec = importlib.util.spec_from_file_location("bootstrap_under_test", path)
        self.boot = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.boot)
        value = activation.begin_candidate(activation.blank_journal(), "b", "b" * 64,
                                           "1" * 32, "2" * 32, "3" * 32)
        self.store = MemoryStore({activation.JOURNAL_PATH: activation.encode_journal(value)})
        self.main = Mock()
        self.wdt = Mock()
        self.runtime = SimpleNamespace(WDT=Mock(return_value=self.wdt))

    def run_boot(self):
        app = SimpleNamespace(main=self.main)
        with patch.object(activation, "FileStore", return_value=self.store), \
             patch.object(self.boot.os, "chdir"), patch.object(self.boot, "_rearm"), \
             patch.object(self.boot, "_reset") as reset, \
             patch.object(sys, "path", list(sys.path)), \
             patch.dict(sys.modules, {"ai_dash_app": app, "machine": self.runtime}):
            self.boot.main()
        return app, reset

    def test_failed_entry_commit_never_executes_candidate(self):
        self.store.fail_after = 0
        with self.assertRaisesRegex(RuntimeError, "could not be committed"):
            self.run_boot()
        self.main.assert_not_called()
        self.runtime.WDT.assert_not_called()

    def test_corrupt_and_newer_journals_never_import_a_slot(self):
        for contents in ("{", activation.encode_journal(activation.blank_journal(bootstrap=2))):
            self.store = MemoryStore({activation.JOURNAL_PATH: contents})
            with self.assertRaisesRegex(RuntimeError, "repair AI Dash"):
                self.run_boot()
        self.main.assert_not_called()

    def test_watchdog_is_armed_before_candidate_code_runs(self):
        self.main.side_effect = lambda: self.runtime.WDT.assert_called_once_with(timeout=8000)
        app, _ = self.run_boot()
        self.assertIs(app.BOOT_CONTEXT["watchdog"], self.wdt)
        self.assertEqual(activation.load(self.store)[0]["pending"]["tries"], 1)

    def test_missing_watchdog_blocks_candidate_and_keeps_confirmed_slot(self):
        self.runtime.WDT.side_effect = ValueError("unsupported")
        with self.assertRaisesRegex(RuntimeError, "watchdog unavailable"):
            self.run_boot()
        state = activation.load(self.store)[0]
        self.assertEqual(state["active"], "a")
        self.assertIsNone(state["pending"])
        self.assertEqual(state["failure"]["error"], "firmware_unsupported")
        self.main.assert_not_called()

    def test_confirmed_slot_also_receives_watchdog_for_native_network_stalls(self):
        state = activation.blank_journal()
        self.store = MemoryStore({activation.JOURNAL_PATH: activation.encode_journal(state)})
        app, _ = self.run_boot()
        self.assertIs(app.BOOT_CONTEXT['watchdog'], self.wdt)
        self.main.assert_called_once()

    def test_builtin_machine_missing_from_module_cache_is_loaded_before_candidate(self):
        import builtins
        imported = builtins.__import__
        def importer(name, *args, **kwargs):
            return self.runtime if name == 'machine' else imported(name, *args, **kwargs)
        app = SimpleNamespace(main=self.main)
        with patch.object(activation, 'FileStore', return_value=self.store), \
                patch.object(self.boot.os, 'chdir'), patch.object(self.boot, '_rearm'), \
                patch.object(sys, 'path', list(sys.path)), \
                patch.dict(sys.modules, {'ai_dash_app': app}), \
                patch('builtins.__import__', side_effect=importer):
            original = sys.modules.pop('machine', None)
            try:
                self.main.side_effect = lambda: self.runtime.WDT.assert_called_once_with(timeout=8000)
                self.boot.main()
                self.assertIs(sys.modules['machine'], self.runtime)
            finally:
                if original is not None:
                    sys.modules['machine'] = original
                else:
                    sys.modules.pop('machine', None)

    def test_later_crash_cannot_overwrite_durable_confirmation(self):
        def confirmed_then_crashed():
            state = activation.load(self.store)[0]
            activation.save(self.store, activation.confirm(state, "b" * 64))
            raise RuntimeError("later app crash")
        self.main.side_effect = confirmed_then_crashed
        with self.assertRaisesRegex(RuntimeError, "later app crash"):
            self.run_boot()
        state = activation.load(self.store)[0]
        self.assertEqual(state["confirmed"], "b")
        self.assertIsNone(state["failure"])
        self.assertEqual(state["success"]["attempt"], "2" * 32)

