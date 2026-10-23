"""The database and its key are private to the account that runs the daemon.

A file takes its permissions from the umask, or on Windows from the folder it
is in, which for a folder under D:\\ means every signed-in user can edit it.
The database holds the configuration that decides which program the daemon
runs and where a saved key is sent, so it must not be editable by others.
"""

import os
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from presto_ai_dash import private_files
from presto_ai_dash.app import Daemon
from presto_ai_dash.credentials import load_cipher
from presto_ai_dash.storage import Storage

WINDOWS = os.name == "nt"


class PrivateFilesBase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.dir = Path(self.temp.name)

    def is_private(self, path):
        """True when nobody beyond the owner (and on Windows SYSTEM and admins) has access."""
        if WINDOWS:
            protected, aces = private_files._read_dacl(path)
            allowed = {private_files._current_user_sid(), private_files._SYSTEM,
                       private_files._ADMINISTRATORS}
            return protected and aces is not None and {sid for sid, _, _ in aces} <= allowed
        return not stat.S_IMODE(os.stat(path).st_mode) & 0o077

    def open_up(self, path):
        """Make a file as open as a default umask or an inheriting folder would."""
        if WINDOWS:
            user = private_files._current_user_sid()
            private_files._apply_sddl(path, "D:(A;;FA;;;%s)(A;;FA;;;AU)" % user)
        else:
            os.chmod(path, 0o666)

    def is_open(self, path):
        if WINDOWS:
            return "Authenticated Users" in private_files._broad_write_access(path)
        return bool(stat.S_IMODE(os.stat(path).st_mode) & 0o077)


class CreateAndProtectTests(PrivateFilesBase):
    def test_a_new_file_is_private_from_the_start(self):
        path = self.dir / "dash.db"
        self.assertTrue(private_files.create_private(path))
        self.assertTrue(path.exists())
        self.assertTrue(self.is_private(path))

    def test_an_existing_file_is_left_alone_by_create(self):
        path = self.dir / "dash.db"
        path.write_bytes(b"data")
        self.open_up(path)
        self.assertFalse(private_files.create_private(path))
        self.assertEqual(path.read_bytes(), b"data")
        self.assertTrue(self.is_open(path))

    def test_protect_closes_an_open_file_and_keeps_its_contents(self):
        path = self.dir / "dash.db"
        path.write_bytes(b"data")
        self.open_up(path)
        self.assertTrue(self.is_open(path))
        self.assertTrue(private_files.protect(path))
        self.assertTrue(self.is_private(path))
        self.assertFalse(self.is_open(path))
        self.assertEqual(path.read_bytes(), b"data")

    def test_protect_is_idempotent_and_the_file_stays_usable(self):
        path = self.dir / "dash.db"
        path.write_bytes(b"data")
        private_files.protect(path)
        self.assertFalse(private_files.protect(path))
        with open(path, "ab") as handle:
            handle.write(b"more")
        self.assertEqual(path.read_bytes(), b"datamore")
        path.unlink()

    def test_memory_databases_and_missing_files_are_ignored(self):
        for value in (":memory:", "", str(self.dir / "absent.db")):
            with self.subTest(value=value):
                self.assertFalse(private_files.protect(value))
        self.assertFalse(private_files.create_private(":memory:"))
        self.assertEqual(private_files.exposure(":memory:"), [])

    def test_a_failure_to_change_permissions_is_logged_and_never_raised(self):
        path = self.dir / "dash.db"
        path.write_bytes(b"data")
        with patch.object(private_files, "_restrict", side_effect=OSError("denied")):
            with self.assertLogs("presto_ai_dash.private_files", level="WARNING") as logged:
                self.assertFalse(private_files.protect(path))
        text = "\n".join(logged.output)
        self.assertIn("dash.db", text)
        self.assertNotIn(self.temp.name, text)


class WhereTheDaemonCreatesFilesTests(PrivateFilesBase):
    def test_a_new_database_is_created_private(self):
        path = self.dir / "dash.db"
        Storage(path).close()
        self.assertTrue(self.is_private(path))

    def test_an_existing_open_database_is_closed_when_opened(self):
        path = self.dir / "dash.db"
        Storage(path).close()
        self.open_up(path)
        self.assertTrue(self.is_open(path))
        storage = Storage(path)
        self.addCleanup(storage.close)
        self.assertTrue(self.is_private(path))

    def test_the_database_still_works_afterwards(self):
        path = self.dir / "dash.db"
        storage = Storage(path)
        storage.close()
        again = Storage(path)
        self.addCleanup(again.close)
        self.assertIsNotNone(again.source_instances())

    def test_a_new_key_file_is_private(self):
        path = self.dir / "dash.db"
        with patch.dict(os.environ):
            os.environ.pop("DASH_ENCRYPTION_KEY", None)
            load_cipher(str(path))
        self.assertTrue(self.is_private(Path(str(path) + ".key")))

    @unittest.skipUnless(WINDOWS, "an existing key file is only re-protected on Windows")
    def test_an_existing_open_key_file_is_closed_on_windows(self):
        path = self.dir / "dash.db"
        with patch.dict(os.environ):
            os.environ.pop("DASH_ENCRYPTION_KEY", None)
            load_cipher(str(path))
            key = Path(str(path) + ".key")
            self.open_up(key)
            load_cipher(str(path), existing=True)
        self.assertTrue(self.is_private(key))


class ExposureTests(PrivateFilesBase):
    def test_a_private_file_in_a_private_folder_reports_nothing(self):
        path = self.dir / "dash.db"
        private_files.create_private(path)
        self.assertEqual(private_files.exposure(path), [])

    @unittest.skipUnless(WINDOWS, "Windows permission lists")
    def test_windows_reports_a_folder_every_user_can_write_to(self):
        user = private_files._current_user_sid()
        private_files._apply_sddl(self.dir, "D:P(A;OICI;FA;;;%s)(A;OICI;FA;;;AU)" % user)
        path = self.dir / "dash.db"
        path.write_bytes(b"data")
        self.assertIn("Authenticated Users", private_files._broad_write_access(path))
        private_files.protect(path)
        self.assertEqual(private_files._broad_write_access(path), [])
        problems = private_files.exposure(path)
        self.assertEqual(len(problems), 1)
        self.assertIn("its folder", problems[0])
        self.assertIn("Authenticated Users", problems[0])

    @unittest.skipUnless(WINDOWS, "Windows permission lists")
    def test_windows_ignores_a_read_only_grant_to_everyone(self):
        user = private_files._current_user_sid()
        private_files._apply_sddl(self.dir, "D:P(A;OICI;FA;;;%s)(A;OICI;FR;;;WD)" % user)
        path = self.dir / "dash.db"
        private_files.create_private(path)
        self.assertEqual(private_files.exposure(path), [])

    @unittest.skipIf(WINDOWS, "POSIX modes")
    def test_posix_reports_a_folder_other_users_can_write_to(self):
        os.chmod(self.dir, 0o777)
        path = self.dir / "dash.db"
        private_files.create_private(path)
        problems = private_files.exposure(path)
        self.assertEqual(len(problems), 1)
        self.assertIn("its folder", problems[0])

    @unittest.skipIf(WINDOWS, "POSIX modes")
    def test_posix_accepts_a_sticky_shared_folder(self):
        os.chmod(self.dir, 0o1777)
        path = self.dir / "dash.db"
        private_files.create_private(path)
        self.assertEqual(private_files.exposure(path), [])

    @unittest.skipIf(WINDOWS, "POSIX modes")
    def test_posix_reports_a_group_writable_file(self):
        path = self.dir / "dash.db"
        path.write_bytes(b"data")
        os.chmod(path, 0o664)
        self.assertTrue(any("the file" in problem for problem in private_files.exposure(path)))

    @unittest.skipIf(WINDOWS, "POSIX modes")
    def test_posix_modes_after_create_and_protect(self):
        path = self.dir / "dash.db"
        private_files.create_private(path)
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        os.chmod(path, 0o644)
        private_files.protect(path)
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)


class StartupCheckTests(PrivateFilesBase):
    def test_a_warning_is_logged_for_each_exposed_file(self):
        with patch.object(private_files, "exposure",
                          side_effect=lambda path: ["its folder of %s can be changed by x" % Path(path).name]):
            with self.assertLogs("presto_ai_dash.private_files", level="WARNING") as logged:
                private_files.check_private_files(["dash.db", "dash.db.key"])
        self.assertEqual(len(logged.records), 2)
        self.assertIn("only this account", logged.output[0])

    def test_nothing_is_logged_when_nothing_is_exposed(self):
        path = self.dir / "dash.db"
        private_files.create_private(path)
        with self.assertNoLogs("presto_ai_dash.private_files", level="WARNING"):
            private_files.check_private_files([path])

    def test_the_daemon_checks_the_database_and_its_key_file(self):
        fake = SimpleNamespace(config={"daemon": {"database": "dash.db"}, "admin": {"key_file": ""}})
        with patch.dict(os.environ), patch.object(private_files, "check_private_files") as check:
            os.environ.pop("DASH_ENCRYPTION_KEY", None)
            Daemon._check_private_files(fake)
        check.assert_called_once_with(["dash.db", "dash.db.key"])

    def test_the_daemon_skips_the_key_file_when_the_key_is_in_the_environment(self):
        fake = SimpleNamespace(config={"daemon": {"database": "dash.db"}, "admin": {"key_file": "k"}})
        with patch.dict(os.environ, {"DASH_ENCRYPTION_KEY": "x"}), \
                patch.object(private_files, "check_private_files") as check:
            Daemon._check_private_files(fake)
        check.assert_called_once_with(["dash.db"])

    def test_a_configured_key_file_is_the_one_checked(self):
        fake = SimpleNamespace(config={"daemon": {"database": "dash.db"},
                                       "admin": {"key_file": "elsewhere.key"}})
        with patch.dict(os.environ), patch.object(private_files, "check_private_files") as check:
            os.environ.pop("DASH_ENCRYPTION_KEY", None)
            Daemon._check_private_files(fake)
        check.assert_called_once_with(["dash.db", "elsewhere.key"])


if __name__ == "__main__":
    unittest.main()
