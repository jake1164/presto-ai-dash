"""One explicitly versioned application identity for the wheel and the device.

The daemon compares a device's revision with the identity of its own bundle, so
the two ends must name the same revision scheme. A mismatch would not be
dangerous, but every device would read as ``unknown``: the check happens at
release time rather than as a fleet-wide surprise.
"""

import importlib.util
from pathlib import Path
import sys
import unittest

import presto_path

TOOLS = Path(presto_path.REPO_ROOT) / "tools"
if str(TOOLS) not in sys.path:
    sys.path.append(str(TOOLS))

SRC = Path(presto_path.REPO_ROOT) / "daemon" / "src"
if str(SRC) not in sys.path:
    sys.path.append(str(SRC))

import presto_bundle as bundle  # noqa: E402
from presto_ai_dash import device_install  # noqa: E402


def load_build_release():
    path = TOOLS / "build_release.py"
    spec = importlib.util.spec_from_file_location("_release_build_check", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class BundleIdentityTests(unittest.TestCase):
    def setUp(self):
        self.release = load_build_release()

    def test_the_checkout_bundle_declares_a_known_scheme(self):
        built = bundle.snapshot()
        self.assertEqual(built["scheme"], bundle.REVISION_SCHEME)
        self.assertIn(built["scheme"], ("legacy-v1", "slot-v1"))
        self.assertIsNone(built["min_bootstrap"])

    def test_the_legacy_layout_never_requires_a_bootstrap(self):
        built = bundle.snapshot()
        self.assertEqual(bundle.REVISION_SCHEME, "legacy-v1")
        self.assertIsNone(bundle.MIN_BOOTSTRAP)
        self.assertIsNone(built["min_bootstrap"])

    def test_the_daemon_and_the_packager_agree(self):
        self.assertEqual(device_install.REVISION_SCHEME, bundle.REVISION_SCHEME)
        self.assertEqual(device_install.MIN_BOOTSTRAP, bundle.MIN_BOOTSTRAP)

    def test_the_release_check_accepts_the_real_bundle(self):
        self.assertEqual(self.release.check_bundle_identity(bundle.snapshot()),
                         bundle.REVISION_SCHEME)

    def test_the_device_and_the_daemon_declare_the_same_scheme(self):
        built = bundle.snapshot()
        device_source = built["files"]["/ai_dash_files/lifecycle.py"].decode("utf-8")
        match = self.release.DEVICE_SCHEME.search(device_source)
        self.assertIsNotNone(match)
        self.assertEqual(match.group(1), built["scheme"])

    def test_a_bundle_without_the_device_parser_is_refused(self):
        built = bundle.snapshot()
        built = dict(built, files={path: data for path, data in built["files"].items()
                                   if path != "/ai_dash_files/lifecycle.py"})
        with self.assertRaises(ValueError):
            self.release.check_bundle_identity(built)

    def test_a_device_declaring_another_scheme_is_refused(self):
        built = bundle.snapshot()
        files = dict(built["files"])
        files["/ai_dash_files/lifecycle.py"] = (
            'SCHEME = "slot-v1"\n').encode("ascii")
        with self.assertRaises(ValueError):
            self.release.check_bundle_identity(dict(built, files=files))

    def test_a_bundle_requiring_a_bootstrap_is_refused(self):
        built = dict(bundle.snapshot(), min_bootstrap=1)
        with self.assertRaises(ValueError):
            self.release.check_bundle_identity(built)

    def test_an_unknown_scheme_is_refused(self):
        with self.assertRaises(ValueError):
            self.release.check_bundle_identity(dict(bundle.snapshot(), scheme="v9"))


class AppSnapshotIdentityTests(unittest.TestCase):
    def test_app_snapshot_carries_the_identity_the_daemon_compares(self):
        snapshot = device_install.app_snapshot()
        self.assertEqual(snapshot["scheme"], "legacy-v1")
        self.assertIsNone(snapshot["min_bootstrap"])
        self.assertEqual(len(snapshot["revision"]), 64)


if __name__ == "__main__":
    unittest.main()
