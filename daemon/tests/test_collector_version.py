"""The collector's version moves whenever the collector does.

The collector is a single file that users copy to other machines, so the only
way the daemon can tell that a copy is out of date is the version the copy
reports. A version that was not bumped when the file changed would make an old
copy look current. So every released version is recorded here with the hash of
the file it describes, and the test fails when the file no longer matches.

To change the collector:

1. Edit ``presto_collector.py``.
2. Raise ``COLLECTOR_VERSION`` by one.
3. Add the new version and the hash this test prints to ``RELEASED``.

While nothing has been released, version 1 is simply whatever the file is, and
step 3 means replacing its hash.
"""

import hashlib
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from presto_ai_dash.collectors import presto_collector as collector

# Version -> SHA-256 of the file with Windows line endings normalised to \n.
RELEASED = {
    "1": "5a2a060e10656d43d49cc754a8b6d8f5ea161d49e13333de88b51b8d8d6f02f0",
}


def file_hash():
    raw = Path(collector.__file__).read_bytes().replace(b"\r\n", b"\n")
    return hashlib.sha256(raw).hexdigest()


class CollectorVersionTests(unittest.TestCase):
    def test_the_file_matches_the_hash_recorded_for_its_version(self):
        version, digest = collector.COLLECTOR_VERSION, file_hash()
        self.assertEqual(
            RELEASED.get(version), digest,
            "presto_collector.py changed without a new version.\n"
            "Raise COLLECTOR_VERSION (now %r) and add\n    %r: %r,\nto RELEASED."
            % (version, str(int(version) + 1) if version.isdigit() else "?", digest))

    def test_versions_are_whole_numbers_counting_up_by_one(self):
        numbers = sorted(int(version) for version in RELEASED)
        self.assertEqual(numbers, list(range(1, len(numbers) + 1)))
        self.assertTrue(all(version.isdigit() for version in RELEASED))

    def test_the_current_version_is_the_newest_released_one(self):
        self.assertEqual(int(collector.COLLECTOR_VERSION), max(int(v) for v in RELEASED))

    def test_the_version_is_short_ascii_the_daemon_will_accept(self):
        from presto_ai_dash.ingest import MAX_VERSION_CHARS
        version = collector.COLLECTOR_VERSION
        self.assertTrue(version.isascii() and version.isdigit())
        self.assertLessEqual(len(version), MAX_VERSION_CHARS)

    def test_every_envelope_carries_the_version_and_the_mode_name(self):
        envelope = collector.heartbeat_envelope("laptop")
        self.assertEqual(envelope["collector"]["version"], collector.COLLECTOR_VERSION)
        self.assertEqual(envelope["collector"]["name"], collector.COLLECTOR_NAME)
        self.assertIn(collector.COLLECTOR_NAME, collector.MODES)


if __name__ == "__main__":
    unittest.main()
