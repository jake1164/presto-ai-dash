"""Structural tests for the Presto app.

These enforce the rule that makes the rest of the suite possible: only
``main.py`` may import ``presto`` or ``machine``. If a pure module grows a
hardware dependency, this fails loudly instead of silently making the logic
untestable.
"""

import os
import re
import unittest

import presto_path

PRESTO_DIR = presto_path.PRESTO_DIR

# Modules that are allowed to touch hardware.
HARDWARE_MODULES = {"main.py", "ui.py"}

# Import statements that pull in the Pimoroni firmware.
HARDWARE_IMPORTS = re.compile(
    r"^\s*(?:from|import)\s+(presto|machine|_presto|picographics|picovector)\b",
    re.MULTILINE,
)


def _presto_files():
    for name in sorted(os.listdir(PRESTO_DIR)):
        if name.endswith(".py"):
            yield name


class TestLayering(unittest.TestCase):

    def test_only_main_and_ui_import_hardware(self):
        offenders = []
        for name in _presto_files():
            if name in HARDWARE_MODULES:
                continue
            with open(os.path.join(PRESTO_DIR, name), encoding="utf-8") as handle:
                source = handle.read()
            if HARDWARE_IMPORTS.search(source):
                offenders.append(name)
        self.assertEqual(offenders, [],
                         "these modules must stay hardware-free: %s" % offenders)

    def test_pure_modules_are_importable(self):
        """Every non-hardware module must import cleanly under CPython."""
        import importlib

        for name in _presto_files():
            if name in HARDWARE_MODULES:
                continue
            module_name = name[:-3]
            if module_name.startswith("config"):
                # config.example.py is a template, not an importable module.
                continue
            importlib.import_module(module_name)

    def test_expected_modules_exist(self):
        expected = {"main.py", "net.py", "cards.py", "gestures.py",
                    "alerts.py", "ui.py", "config.example.py"}
        present = set(_presto_files())
        self.assertTrue(expected.issubset(present),
                        "missing: %s" % (expected - present))


class TestSyntax(unittest.TestCase):

    def test_every_module_compiles(self):
        import py_compile
        import tempfile

        for name in _presto_files():
            path = os.path.join(PRESTO_DIR, name)
            with open(path, encoding="utf-8") as handle:
                source = handle.read()
            try:
                compile(source, path, "exec")
            except SyntaxError as exc:  # pragma: no cover
                self.fail("%s does not compile: %s" % (name, exc))
            # Also exercise py_compile so the check matches the real tool.
            with tempfile.NamedTemporaryFile(suffix=".pyc", delete=False) as tmp:
                tmp.close()
                try:
                    py_compile.compile(path, cfile=tmp.name, doraise=True)
                finally:
                    os.remove(tmp.name)


class TestConfigExample(unittest.TestCase):

    def test_config_example_is_valid_python(self):
        path = os.path.join(PRESTO_DIR, "config.example.py")
        with open(path, encoding="utf-8") as handle:
            source = handle.read()
        compile(source, path, "exec")

    def test_config_example_defines_the_keys_main_needs(self):
        """The example config must cover every key main.py reads."""
        path = os.path.join(PRESTO_DIR, "config.example.py")
        with open(path, encoding="utf-8") as handle:
            source = handle.read()

        main_path = os.path.join(PRESTO_DIR, "main.py")
        with open(main_path, encoding="utf-8") as handle:
            main_source = handle.read()

        used = set(re.findall(r'getattr\(config,\s*"([A-Z_]+)"', main_source))
        self.assertTrue(used, "main.py should read config via getattr")

        defined = set(re.findall(r"^([A-Z][A-Z0-9_]*)\s*=", source, re.MULTILINE))
        missing = used - defined
        self.assertEqual(missing, set(),
                         "config.example.py is missing: %s" % sorted(missing))


if __name__ == "__main__":
    unittest.main()
