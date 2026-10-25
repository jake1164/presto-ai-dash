"""The vendored WebAssembly runtime must be the bytes we recorded.

The clickable demo ships MicroPython's WebAssembly build from this repository.
An unreviewed change to either file would silently change what every reader
runs, so the hashes in ``SHA256SUMS`` are checked here: replacing the runtime
is a deliberate act that updates the file and the notice together.
"""

import hashlib
import unittest
from pathlib import Path

import presto_path

VENDOR = Path(presto_path.REPO_ROOT) / "docs" / "user" / "demo" / "vendor"
SUMS = VENDOR / "SHA256SUMS"


def recorded_hashes():
    entries = {}
    for line in SUMS.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        digest, name = line.split()
        entries[name] = digest.lower()
    return entries


class WasmVendorTests(unittest.TestCase):
    def test_the_recorded_hashes_match_the_committed_files(self):
        entries = recorded_hashes()
        self.assertEqual(set(entries), {"micropython.js", "micropython.wasm"})
        for name, digest in entries.items():
            with self.subTest(name=name):
                actual = hashlib.sha256((VENDOR / name).read_bytes()).hexdigest()
                self.assertEqual(
                    actual, digest,
                    "%s does not match SHA256SUMS. A WebAssembly runtime upgrade "
                    "must update the file, SHA256SUMS and THIRD_PARTY_NOTICES.md "
                    "deliberately; do not rewrite the recorded hash to make this "
                    "test pass." % name)

    def test_the_sizes_are_the_ones_the_plan_verified(self):
        self.assertEqual((VENDOR / "micropython.wasm").stat().st_size, 449553)
        self.assertEqual((VENDOR / "micropython.js").stat().st_size, 110120)

    def test_the_sums_name_the_package_and_version_they_came_from(self):
        text = SUMS.read_text(encoding="utf-8")
        self.assertIn("@micropython/micropython-webassembly-pyscript", text)
        self.assertIn("1.29.0-6", text)


if __name__ == "__main__":
    unittest.main()
