"""The collector's setup command: installing the file the admin page downloads.

Everything runs in temporary folders; no real home folder or token is touched.
"""

import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from presto_ai_dash.collectors import presto_collector as collector

URL = "http://server:8787/api/v1/ingest"


def setup_file(**changes):
    data = {"kind": "presto-collector-setup", "url": URL, "service": "codex",
            "name": "Work Codex", "token_file": "codex-abc.token", "token": "s3cret-token"}
    data.update(changes)
    return {key: value for key, value in data.items() if value is not None}


class SetupTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.downloads = os.path.join(self.folder.name, "Downloads")
        os.mkdir(self.downloads)
        self.config = os.path.join(self.folder.name, "home", ".presto-ai-dash", "collector.json")

    def download(self, name="presto-collector-setup-codex-abc.json", **changes):
        path = os.path.join(self.downloads, name)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(setup_file(**changes), handle)
        return path

    def run_setup(self, **options):
        out = io.StringIO()
        options.setdefault("folders", [self.downloads])
        options.setdefault("check", False)
        code = collector.setup(config_path=self.config, out=out, **options)
        return code, out.getvalue()

    def test_a_download_becomes_a_config_and_a_token_file_and_is_deleted(self):
        path = self.download()
        code, text = self.run_setup()
        self.assertEqual(code, 0, text)
        with open(self.config, encoding="utf-8") as handle:
            config = json.load(handle)
        self.assertEqual(config["url"], URL)
        self.assertEqual(config["accounts"], [{"service": "codex", "name": "Work Codex",
                                               "token_file": "codex-abc.token"}])
        token_path = os.path.join(os.path.dirname(self.config), "codex-abc.token")
        with open(token_path, encoding="utf-8") as handle:
            self.assertEqual(handle.read().strip(), "s3cret-token")
        self.assertFalse(os.path.exists(path), "the setup file holds the token")
        collector.load_config(self.config)
        self.assertNotIn("s3cret-token", text)

    def test_keep_leaves_the_download(self):
        path = self.download()
        self.assertEqual(self.run_setup(keep=True)[0], 0)
        self.assertTrue(os.path.exists(path))

    def test_a_second_account_is_added_and_a_repeat_replaces(self):
        self.download()
        self.run_setup()
        with open(os.path.join(self.downloads, "presto-collector-setup-claude-x.json"),
                  "w", encoding="utf-8") as handle:
            json.dump(setup_file(service="claude", name="Home", token_file="claude-x.token",
                                 token="other"), handle)
        self.download(token="rotated")
        self.assertEqual(self.run_setup()[0], 0)
        with open(self.config, encoding="utf-8") as handle:
            accounts = json.load(handle)["accounts"]
        self.assertEqual(sorted(a["token_file"] for a in accounts),
                         ["claude-x.token", "codex-abc.token"])
        with open(os.path.join(os.path.dirname(self.config), "codex-abc.token"),
                  encoding="utf-8") as handle:
            self.assertEqual(handle.read().strip(), "rotated")

    def test_no_download_says_where_to_get_one(self):
        code, text = self.run_setup()
        self.assertEqual(code, 2)
        self.assertIn("Download setup file", text)
        self.assertFalse(os.path.exists(self.config))

    def test_bad_files_are_refused_and_nothing_is_written(self):
        for changes in ({"kind": "other"}, {"url": "http://REPLACE-WITH-DASH-ADDRESS:8787/x"},
                        {"service": "gemini"}, {"token_file": "../evil.token"},
                        {"token_file": ".hidden"}, {"token": "has space"}, {"token": None},
                        {"extra": "x"}):
            with self.subTest(changes=changes):
                path = self.download(**changes)
                code, _ = self.run_setup()
                self.assertEqual(code, 2)
                self.assertTrue(os.path.exists(path), "a refused file is left alone")
                self.assertFalse(os.path.exists(self.config))
                os.remove(path)

    def test_a_broken_existing_config_is_not_overwritten(self):
        os.makedirs(os.path.dirname(self.config))
        with open(self.config, "w", encoding="utf-8") as handle:
            handle.write("{not json")
        self.download()
        code, text = self.run_setup()
        self.assertEqual(code, 2)
        self.assertIn("cannot be read", text)
        with open(self.config, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "{not json")

    @unittest.skipIf(os.name == "nt", "POSIX file modes")
    def test_the_token_file_is_private(self):
        self.download()
        self.run_setup()
        mode = os.stat(os.path.join(os.path.dirname(self.config), "codex-abc.token")).st_mode
        self.assertEqual(mode & 0o077, 0)

    def test_the_check_runs_once_after_installing(self):
        self.download()
        with patch.object(collector, "watch", return_value=0) as watch:
            code, text = self.run_setup(check=True)
        self.assertEqual(code, 0)
        watch.assert_called_once()
        self.assertTrue(watch.call_args.kwargs["once"])
        self.assertIn("python presto_collector.py watch", text)


if __name__ == "__main__":
    unittest.main()
