"""The container's Presto preview switch and the files the image carries for it."""

from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
sys.path.insert(0, str(ROOT / "docker"))

from presto_ai_dash import preview_host  # noqa: E402
from presto_ai_dash.config import ConfigError  # noqa: E402
import collect_preview  # noqa: E402


class SettingsTests(unittest.TestCase):
    def test_it_is_off_unless_asked_for(self):
        self.assertIsNone(preview_host.preview_settings({}))
        self.assertIsNone(preview_host.preview_settings({"DASH_PRESTO_PREVIEW": "0"}))

    def test_on_listens_on_loopback_at_8790_by_default(self):
        self.assertEqual(preview_host.preview_settings({"DASH_PRESTO_PREVIEW": "1"}),
                         ("127.0.0.1", 8790, ["127.0.0.1:8790", "localhost:8790"]))

    def test_the_host_port_and_names_can_be_set(self):
        host, port, names = preview_host.preview_settings({
            "DASH_PRESTO_PREVIEW": "yes", "DASH_PRESTO_PREVIEW_HOST": "0.0.0.0",
            "DASH_PRESTO_PREVIEW_PORT": "9000",
            "DASH_PRESTO_PREVIEW_ALLOWED_HOSTS": "Localhost:19000, 127.0.0.1:19000"})
        self.assertEqual((host, port), ("0.0.0.0", 9000))
        self.assertEqual(names, ["localhost:19000", "127.0.0.1:19000"])

    def test_unusable_values_are_refused(self):
        for env in ({"DASH_PRESTO_PREVIEW": "maybe"},
                    {"DASH_PRESTO_PREVIEW": "1", "DASH_PRESTO_PREVIEW_PORT": "http"},
                    {"DASH_PRESTO_PREVIEW": "1", "DASH_PRESTO_PREVIEW_PORT": "70000"},
                    {"DASH_PRESTO_PREVIEW": "1", "DASH_PRESTO_PREVIEW_ALLOWED_HOSTS": "http://x/"}):
            with self.subTest(env=env), self.assertRaises(ConfigError):
                preview_host.preview_settings(env)

    def test_a_bad_setting_or_missing_files_leave_the_daemon_running(self):
        with self.assertLogs("presto_ai_dash.preview_host", "ERROR"):
            self.assertIsNone(preview_host.start_preview(
                8787, "t", environ={"DASH_PRESTO_PREVIEW": "maybe"}))
        with tempfile.TemporaryDirectory() as empty, \
                self.assertLogs("presto_ai_dash.preview_host", "ERROR"):
            self.assertIsNone(preview_host.start_preview(
                8787, "t", root=empty, environ={"DASH_PRESTO_PREVIEW": "1"}))
        self.assertIsNone(preview_host.start_preview(8787, "t", environ={}))


class ImageFilesTests(unittest.TestCase):
    """collect_preview keeps the public app and never a device's private files."""

    def test_private_files_and_their_copies_never_reach_the_image(self):
        with tempfile.TemporaryDirectory() as folder:
            source, target = Path(folder, "src"), Path(folder, "out")
            (source / "presto" / "launcher").mkdir(parents=True)
            (source / "tools" / "presto_preview").mkdir(parents=True)
            shutil.copyfile(ROOT / "tools" / "presto_bundle.py", source / "tools" / "presto_bundle.py")
            for name in ("main.py", "ui.py", "config.py", "secrets.py", "secrets_old.py",
                         "config-backup.py", "my_secrets.py", "config.example.py"):
                (source / "presto" / name).write_text("X = 1\n")
            (source / "presto" / "launcher" / "ai_dash.py").write_text("X = 1\n")
            (source / "tools" / "presto_preview" / "live.py").write_text("X = 1\n")
            collect_preview.collect(source, target)
            kept = sorted(str(path.relative_to(target)).replace("\\", "/")
                          for path in target.rglob("*") if path.is_file())
        self.assertEqual(kept, ["presto/launcher/ai_dash.py", "presto/main.py", "presto/ui.py",
                                "tools/presto_bundle.py", "tools/presto_preview/live.py"])

    def test_the_real_app_inventory_carries_no_private_file(self):
        with tempfile.TemporaryDirectory() as folder:
            collect_preview.collect(ROOT, folder)
            names = [path.name for path in Path(folder, "presto").rglob("*.py")]
        self.assertIn("main.py", names)
        for name in names:
            self.assertFalse("secret" in name.lower() or name.lower().startswith("config"), name)


if __name__ == "__main__":
    unittest.main()
