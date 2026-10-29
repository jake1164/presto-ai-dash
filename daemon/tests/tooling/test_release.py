"""The release: tag check, the app inside the wheel, USB commands, workflow files."""

from contextlib import redirect_stderr
import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import presto_path

ROOT = Path(presto_path.REPO_ROOT)
TOOLS = ROOT / "tools"
if str(TOOLS) not in sys.path:
    sys.path.append(str(TOOLS))
import build_release
import presto_bundle
from presto_ai_dash import app, device_install


class TagTests(unittest.TestCase):
    def setUp(self):
        project, package = build_release.declared_versions()
        self.version = project
        self.assertEqual(project, package, "pyproject.toml and __version__ must agree")

    def test_the_checked_in_version_has_a_tag(self):
        base = self.version.split("rc")[0]
        tag = "v" + base if base == self.version else "v%s-rc%s" % (base, self.version.split("rc")[1])
        self.assertEqual(build_release.check_tag(tag), self.version)

    def test_a_tag_names_one_pep_440_version(self):
        self.assertEqual(build_release.tag_version("v0.50.0"), "0.50.0")
        self.assertEqual(build_release.tag_version("v0.50.0-rc4"), "0.50.0rc4")
        self.assertEqual(build_release.tag_version("v0.50.0-rc04"), "0.50.0rc4")
        self.assertEqual(build_release.tag_version("v1.2.3-b2"), "1.2.3b2")

    def test_a_release_and_its_candidates_need_their_own_versions(self):
        with patch.object(build_release, "declared_versions", return_value=("0.50.0rc4",) * 2):
            self.assertEqual(build_release.check_tag("v0.50.0-rc4"), "0.50.0rc4")
            for tag in ("v0.50.0", "v0.50.0-rc3", "v0.50.1-rc4"):
                with self.assertRaises(ValueError, msg=tag):
                    build_release.check_tag(tag)
        with patch.object(build_release, "declared_versions", return_value=("0.50.0",) * 2):
            self.assertEqual(build_release.check_tag("v0.50.0"), "0.50.0")
            with self.assertRaises(ValueError):
                build_release.check_tag("v0.50.0-rc4")

    def test_other_tags_are_refused(self):
        for tag in ("v9.9.9", self.version, "v1.2", "latest", "v%s;rm" % self.version,
                    "v0.50.0-test", "v0.50.0-rc", "v0.50.0-rc1.post1"):
            with self.assertRaises(ValueError, msg=tag):
                build_release.check_tag(tag)


class BundledAppTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name) / "presto_app"
        self.bundle = build_release.stage(ROOT, self.folder)

    def test_the_bundled_app_has_the_checkouts_revision_and_files(self):
        bundled = device_install.bundled_snapshot(self.folder)
        self.assertEqual(bundled["revision"], presto_bundle.snapshot()["revision"])
        self.assertEqual(bundled["files"], presto_bundle.snapshot()["files"])

    def test_private_device_files_are_never_staged(self):
        names = {path.name for path in self.folder.rglob("*") if path.is_file()}
        self.assertFalse({"config.py", "secrets.py", "config.example.py"} & names)

    def test_byte_compiled_copies_made_by_pip_are_ignored(self):
        cache = self.folder / "ai_dash_files" / "__pycache__"
        cache.mkdir()
        (cache / "ui.cpython-313.pyc").write_bytes(b"\0")
        self.assertEqual(device_install.bundled_snapshot(self.folder)["revision"],
                         self.bundle["revision"])

    def test_a_changed_file_is_refused(self):
        (self.folder / "ai_dash_files" / "ui.py").write_text("x = 1\n", encoding="utf-8")
        with self.assertRaises(device_install.NoBundledApp):
            device_install.bundled_snapshot(self.folder)

    def test_a_missing_folder_says_what_to_do(self):
        with self.assertRaises(device_install.NoBundledApp) as caught:
            device_install.bundled_snapshot(Path(self.temp.name) / "absent")
        self.assertIn("release wheel", str(caught.exception))


class WheelCheckTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.bundle = build_release.stage(ROOT, Path(self.temp.name) / "stage")

    def wheel(self, extra=(), drop=None, drop_template=False):
        path = Path(self.temp.name) / "x.whl"
        with zipfile.ZipFile(path, "w") as archive:
            for target, data in self.bundle["files"].items():
                if target != drop:
                    archive.writestr("presto_ai_dash/presto_app" + target, data)
            if not drop_template:
                template = (ROOT / "daemon" / "src" / "presto_ai_dash" / "setup"
                            / "presto_setup_template.py")
                archive.writestr("presto_ai_dash/setup/presto_setup_template.py",
                                 template.read_bytes())
            for name in extra:
                archive.writestr(name, b"x")
        return path

    def test_a_complete_clean_wheel_passes(self):
        build_release.check_wheel(self.wheel(extra=["presto_ai_dash/config.py"]), self.bundle)

    def test_a_wheel_without_the_setup_template_fails(self):
        with self.assertRaisesRegex(ValueError, "presto_setup_template"):
            build_release.check_wheel(self.wheel(drop_template=True), self.bundle)

    def test_a_missing_app_file_fails(self):
        with self.assertRaises(ValueError):
            build_release.check_wheel(self.wheel(drop="/ai_dash_files/ui.py"), self.bundle)

    def test_private_files_fail(self):
        for name in ("dash.toml", "x/dash.db", "x/dash.db.key", ".env",
                     "presto_ai_dash/presto_app/ai_dash_files/secrets.py",
                     "presto_ai_dash/presto_app/ai_dash_files/config.py"):
            with self.assertRaises(ValueError, msg=name):
                build_release.check_wheel(self.wheel(extra=[name]), self.bundle)


class CommandTests(unittest.TestCase):
    def test_install_presto_without_a_bundle_explains_itself(self):
        error = io.StringIO()
        with patch.object(device_install, "bundled_snapshot",
                          side_effect=device_install.NoBundledApp("no app here")), \
                redirect_stderr(error):
            self.assertEqual(app.main(["install-presto", "--port", "COM3"]), 2)
        self.assertIn("no app here", error.getvalue())

    def test_a_port_is_required(self):
        error = io.StringIO()
        with redirect_stderr(error):
            code = device_install.run("sync", None, {"revision": "a" * 64})
        self.assertEqual(code, 2)
        self.assertIn("--port", error.getvalue())

    def test_install_runs_sync_and_check_runs_check(self):
        for command, action in (("install-presto", "sync"), ("check-presto", "check")):
            with patch.object(device_install, "bundled_snapshot", return_value={"revision": "a" * 64}), \
                    patch.object(device_install, "run", return_value=0) as run:
                self.assertEqual(device_install.main(command, ["--port", "COM3"]), 0)
            self.assertEqual(run.call_args.args[:2], (action, "COM3"))


class PublishedFilesTests(unittest.TestCase):
    def test_the_workflow_runs_on_version_tags_and_keeps_latest_for_full_releases(self):
        text = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
        self.assertIn("tags:", text)
        self.assertIn("'v[0-9]+.[0-9]+.[0-9]+'", text)
        self.assertIn("--prerelease", text)
        # latest is tagged and pushed only inside the "not a pre-release" test.
        guard = text.index('if [[ "$VERSION" != *-* ]]')
        self.assertLess(guard, text.index('"$IMAGE:latest"'))
        self.assertNotIn('push "$IMAGE:latest"', text[:guard])
        self.assertLess(text.index("unittest discover"), text.index("docker push"))

    def test_compose_pulls_the_published_image_by_default(self):
        text = (ROOT / "compose.yaml").read_text(encoding="utf-8")
        self.assertIn("ghcr.io/jake1164/presto-ai-dash:latest", text)
        self.assertNotIn("\n    build:", text)
        self.assertIn("build: .", (ROOT / "compose.build.yaml").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
