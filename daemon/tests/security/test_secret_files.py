"""Secrets from a file, for a container platform that mounts them.

``DASH_ADMIN_TOKEN_FILE`` is read the way ``DASH_ADMIN_TOKEN`` is. The secret
never appears in an error message, and the variables stay out of a child
program's environment.
"""

import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from presto_ai_dash import child_env, config as config_module, credentials
from presto_ai_dash.effective import effective_settings
from presto_ai_dash.secret_env import (MAX_SECRET_BYTES, SecretFileError,
                                       secret_from_environment)

SECRET = "file-secret-0123456789abcdefghij"


class SecretFileBase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)

    def secret_file(self, text=SECRET, name="secret"):
        path = self.folder / name
        path.write_bytes(text.encode("utf-8") if isinstance(text, str) else text)
        return str(path)


class HelperTests(SecretFileBase):
    def test_a_plain_variable_is_returned(self):
        self.assertEqual(secret_from_environment("X", {"X": "value"}), "value")

    def test_nothing_set_is_none(self):
        self.assertIsNone(secret_from_environment("X", {}))
        self.assertIsNone(secret_from_environment("X", {"X": ""}))

    def test_a_file_is_read_and_its_newline_dropped(self):
        for ending in ("", "\n", "\r\n"):
            with self.subTest(ending=repr(ending)):
                path = self.secret_file(SECRET + ending)
                self.assertEqual(secret_from_environment("X", {"X_FILE": path}), SECRET)

    def test_both_set_is_an_error(self):
        path = self.secret_file()
        with self.assertRaises(SecretFileError) as caught:
            secret_from_environment("X", {"X": "a", "X_FILE": path})
        self.assertNotIn(SECRET, str(caught.exception))

    def test_an_unreadable_file_names_the_path_but_not_the_secret(self):
        missing = str(self.folder / "absent")
        with self.assertRaises(SecretFileError) as caught:
            secret_from_environment("X", {"X_FILE": missing})
        self.assertIn("X_FILE", str(caught.exception))

    def test_an_empty_file_is_an_error(self):
        with self.assertRaises(SecretFileError):
            secret_from_environment("X", {"X_FILE": self.secret_file("\n")})

    def test_a_large_file_is_refused(self):
        path = self.secret_file("a" * (MAX_SECRET_BYTES + 1))
        with self.assertRaises(SecretFileError):
            secret_from_environment("X", {"X_FILE": path})

    def test_a_file_that_is_not_text_is_refused(self):
        with self.assertRaises(SecretFileError) as caught:
            secret_from_environment("X", {"X_FILE": self.secret_file(b"\xff\xfe\x00\x80")})
        self.assertNotIn("xff", str(caught.exception).lower())


class ConfigTests(SecretFileBase):
    def load(self, environ):
        with patch.dict(os.environ, environ, clear=True):
            return config_module.load_config()

    def test_the_two_tokens_come_from_files(self):
        admin = self.secret_file("admin-" + SECRET, "admin")
        shared = self.secret_file("shared-" + SECRET, "shared")
        loaded = self.load({"DASH_ADMIN_TOKEN_FILE": admin, "DASH_SHARED_TOKEN_FILE": shared})
        self.assertEqual(loaded["admin"]["token"], "admin-" + SECRET)
        self.assertEqual(loaded["daemon"]["shared_token"], "shared-" + SECRET)

    def test_a_bad_file_stops_the_start_with_a_clear_message(self):
        with self.assertRaises(config_module.ConfigError) as caught:
            self.load({"DASH_ADMIN_TOKEN_FILE": str(self.folder / "absent")})
        self.assertIn("DASH_ADMIN_TOKEN_FILE", str(caught.exception))

    def test_both_forms_together_stop_the_start(self):
        with self.assertRaises(config_module.ConfigError):
            self.load({"DASH_ADMIN_TOKEN": SECRET,
                       "DASH_ADMIN_TOKEN_FILE": self.secret_file()})

    def test_the_effective_view_says_set_and_never_shows_the_token(self):
        environ = {"DASH_ADMIN_TOKEN_FILE": self.secret_file()}
        loaded = self.load(environ)
        rows = effective_settings(loaded, None, None, environ)
        row = next(item for item in rows if item["name"] == "Admin token")
        self.assertEqual(row["value"], "set")
        self.assertNotIn(SECRET, repr(rows))


class CipherTests(SecretFileBase):
    def test_the_master_key_can_come_from_a_file(self):
        from cryptography.fernet import Fernet
        key = Fernet.generate_key().decode("ascii")
        with patch.dict(os.environ, {"DASH_ENCRYPTION_KEY_FILE": self.secret_file(key + "\n")},
                        clear=True):
            cipher = credentials.load_cipher(":memory:")
        sealed = cipher.encrypt("instance", "provider-key")
        with patch.dict(os.environ, {"DASH_ENCRYPTION_KEY": key}, clear=True):
            again = credentials.load_cipher(":memory:")
        self.assertEqual(again.decrypt("instance", sealed), "provider-key")


class ChildEnvironmentTests(unittest.TestCase):
    def test_the_file_variables_never_reach_a_child(self):
        with patch.dict(os.environ, {"DASH_ADMIN_TOKEN_FILE": "/run/secrets/a",
                                     "DASH_ENCRYPTION_KEY_FILE": "/run/secrets/k",
                                     "PATH": "/bin"}, clear=True):
            environment = child_env.child_environment()
        self.assertEqual(environment, {"PATH": "/bin"})


if __name__ == "__main__":
    unittest.main()
