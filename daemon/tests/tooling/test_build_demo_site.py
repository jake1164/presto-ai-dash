"""The generated demo site: complete, deterministic, and never private.

The clickable documentation demo is built from the same app files the device
runs, so the build output is a deployment artefact: it must contain everything
the page needs, the same bytes on every build, and none of the device's private
configuration.

The full builds run as the real command in a subprocess: a build loads the app
modules through the preview's loader, and doing that inside the test process
would re-import ``net`` and other app modules under the other tests' feet.
"""

import hashlib
import json
import os
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import presto_path

TOOLS = os.path.join(presto_path.REPO_ROOT, "tools")
TOOL = os.path.join(TOOLS, "build_demo_site.py")
if TOOLS not in sys.path:
    sys.path.append(TOOLS)

import build_demo_site  # noqa: E402
from presto_bundle import app_files, is_private_name  # noqa: E402


def expected_library():
    """The public app modules plus the host drawing code, and nothing else."""
    names = {os.path.basename(target) for _source, target in app_files().items()
             if target.startswith("/ai_dash_files/")}
    names |= {"fonts.py", "framebuffer.py"}
    return names


def file_hashes(root):
    found = {}
    for folder, _dirs, names in os.walk(root):
        for name in names:
            path = os.path.join(folder, name)
            with open(path, "rb") as handle:
                found[os.path.relpath(path, root).replace(os.sep, "/")] = \
                    hashlib.sha256(handle.read()).hexdigest()
    return found


def run_build(out):
    result = subprocess.run(
        [sys.executable, TOOL, "--out", out], cwd=presto_path.REPO_ROOT,
        capture_output=True, text=True, timeout=180)
    if result.returncode != 0:
        raise AssertionError("the build failed:\n%s\n%s" % (result.stdout, result.stderr))
    return result


class BuildOutputTests(unittest.TestCase):
    def test_the_output_holds_the_app_the_data_the_poster_and_the_hashes(self):
        with tempfile.TemporaryDirectory() as out:
            result = run_build(out)
            library = expected_library()
            self.assertEqual(set(file_hashes(out)),
                             {"demo.json", "hashes.json", "poster.png"} |
                             {"lib/" + name for name in library})
            self.assertEqual(len(os.listdir(os.path.join(out, "lib"))), len(library))
            self.assertIn("demo.json", result.stdout)
            with open(os.path.join(out, "poster.png"), "rb") as handle:
                poster = handle.read()
            self.assertEqual(poster[:8], b"\x89PNG\r\n\x1a\n")
            self.assertEqual(struct.unpack(">II", poster[16:24]), (480, 480))

    def test_the_data_file_covers_every_step_and_names_every_scene(self):
        from presto_ai_dash import demo

        with tempfile.TemporaryDirectory() as out:
            run_build(out)
            with open(os.path.join(out, "demo.json"), encoding="utf-8") as handle:
                data = json.load(handle)
            self.assertEqual(data["loop_seconds"], demo.LOOP_SECONDS)
            self.assertEqual(data["step_seconds"], demo.STEP_SECONDS)
            # A static host cannot list a folder, so the page reads the names here.
            self.assertEqual(data["lib"], sorted(expected_library()))
            self.assertEqual([scene["name"] for scene in data["scenes"]],
                             [name for name, _start in demo.SCENES])
            expected = list(range(0, demo.LOOP_SECONDS, demo.STEP_SECONDS))
            self.assertEqual([step["t"] for step in data["steps"]], expected)
            card_ids = sorted(card["id"] for card in data["steps"][0]["payload"]["cards"])
            for step in data["steps"]:
                self.assertIs(step["payload"]["demo"], True)
                self.assertEqual(sorted(step["details"]), card_ids)
                self.assertEqual(sorted(card["id"] for card in step["payload"]["cards"]),
                                 card_ids)

    def test_the_hashes_cover_home_and_every_card_at_every_scene(self):
        from presto_ai_dash import demo

        with tempfile.TemporaryDirectory() as out:
            run_build(out)
            with open(os.path.join(out, "hashes.json"), encoding="utf-8") as handle:
                hashes = json.load(handle)
            self.assertEqual(hashes["algorithm"], "sha256")
            self.assertEqual((hashes["width"], hashes["height"], hashes["channels"]),
                             (240, 240, 3))
            self.assertIn("dash", hashes["cards"])
            self.assertEqual(len(hashes["cards"]), 7)
            self.assertEqual(sorted(hashes["scenes"]),
                             sorted(name for name, _start in demo.SCENES))
            for scene, screens in hashes["scenes"].items():
                self.assertEqual(sorted(screens), sorted(hashes["cards"]), scene)
                for card_id, digest in screens.items():
                    self.assertRegex(digest, r"^[0-9a-f]{64}$", card_id)

    def test_the_data_file_is_inside_the_size_limit(self):
        with tempfile.TemporaryDirectory() as out:
            run_build(out)
            size = os.path.getsize(os.path.join(out, "demo.json"))
            self.assertLessEqual(size, build_demo_site.MAX_DEMO_BYTES)
            self.assertGreater(size, 0)


class DeterminismTests(unittest.TestCase):
    def test_two_builds_are_byte_identical(self):
        with tempfile.TemporaryDirectory() as one, \
                tempfile.TemporaryDirectory() as two:
            run_build(one)
            run_build(two)
            self.assertEqual(file_hashes(one), file_hashes(two))


class PrivateFileTests(unittest.TestCase):
    def test_no_private_app_file_reaches_the_page(self):
        with tempfile.TemporaryDirectory() as out:
            run_build(out)
            names = os.listdir(os.path.join(out, "lib"))
            self.assertEqual(set(names), expected_library())
            for name in names:
                self.assertFalse(is_private_name(name), name)
                self.assertNotIn("secret", name.lower(), name)
                self.assertFalse(name.startswith("config"), name)
            # png.py is CPython-only and the page must not carry it.
            self.assertNotIn("png.py", names)


class SizeLimitTests(unittest.TestCase):
    def test_a_demo_file_over_the_limit_fails_the_build(self):
        real = build_demo_site.demo.demo_payload

        def fat(second, now=None):
            payload = real(second, now=now)
            payload["padding"] = "x" * 16000
            return payload

        with tempfile.TemporaryDirectory() as out:
            with patch.object(build_demo_site.demo, "demo_payload", fat):
                with self.assertRaises(SystemExit) as caught:
                    build_demo_site.build(out)
        self.assertIn(str(build_demo_site.MAX_DEMO_BYTES), str(caught.exception))


class OutputGuardTests(unittest.TestCase):
    def test_it_refuses_to_clear_a_directory_that_is_not_a_previous_build(self):
        with tempfile.TemporaryDirectory() as out:
            keep = os.path.join(out, "notes.txt")
            with open(keep, "w", encoding="utf-8") as handle:
                handle.write("keep me")
            with self.assertRaises(SystemExit):
                build_demo_site.build(out)
            self.assertTrue(os.path.exists(keep), "a mistyped --out deleted a file")


if __name__ == "__main__":
    unittest.main()
