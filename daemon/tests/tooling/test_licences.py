"""The licence travels with what is distributed, and the notices are there.

The project is MIT. A wheel, an image and a copy of the repository should each
carry that licence, and the one piece of someone else's work in the repository,
the bitmap font data generated from Pimoroni's fonts, should carry theirs.
"""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def read(*parts):
    return (ROOT.joinpath(*parts)).read_text(encoding="utf-8").replace("\r\n", "\n")


class ProjectLicenceTests(unittest.TestCase):
    def test_the_daemon_package_carries_the_same_licence_as_the_repository(self):
        # setuptools packs a LICENSE beside pyproject.toml into the wheel; the
        # repository's own sits one folder up, out of its reach, so there is a
        # copy, and this keeps it a copy.
        self.assertEqual(read("daemon", "LICENSE"), read("LICENSE"))

    def test_the_licence_is_mit_and_the_package_metadata_says_so(self):
        self.assertTrue(read("LICENSE").startswith("MIT License\n"))
        self.assertRegex(read("daemon", "pyproject.toml"), r'(?m)^license\s*=\s*\{\s*text\s*=\s*"MIT"\s*\}')

    def test_the_licence_names_a_holder_and_a_year(self):
        self.assertRegex(read("LICENSE"), r"Copyright \(c\) 20\d\d \S+")


class ImageTests(unittest.TestCase):
    DOCKERFILE = read("Dockerfile")

    def test_the_image_says_it_is_mit(self):
        self.assertRegex(self.DOCKERFILE, r'org\.opencontainers\.image\.licenses="MIT"')

    def test_the_image_carries_the_licence_and_the_notices(self):
        self.assertRegex(self.DOCKERFILE, r"(?m)^COPY LICENSE THIRD_PARTY_NOTICES\.md /licenses/\s*$")

    def test_the_wheel_built_in_the_image_gets_its_licence(self):
        builder = self.DOCKERFILE.split("AS builder", 1)[1].split("\nFROM ", 1)[0]
        self.assertIn("daemon/LICENSE", builder)

    def test_the_build_context_does_not_leave_those_files_out(self):
        ignored = {line.strip() for line in read(".dockerignore").splitlines()
                   if line.strip() and not line.startswith("#")}
        for name in ("LICENSE", "THIRD_PARTY_NOTICES.md", "daemon/LICENSE"):
            self.assertNotIn(name, ignored)
        self.assertNotIn("daemon", ignored)


class FontNoticeTests(unittest.TestCase):
    NOTICES = read("THIRD_PARTY_NOTICES.md")

    def test_the_notice_holds_the_upstream_copyright_and_permission_text(self):
        for required in ("Copyright (c) 2021 Pimoroni Ltd",
                         "Permission is hereby granted, free of charge, to any person obtaining a copy",
                         "The above copyright notice and this permission notice shall be included in all",
                         'THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND'):
            self.assertIn(required, self.NOTICES)

    def test_the_notice_names_where_the_data_came_from_and_where_it_is(self):
        self.assertIn("tools/presto_preview/fonts.py", self.NOTICES)
        self.assertIn("pimoroni-pico", self.NOTICES)
        self.assertIn("font6_data.cpp", self.NOTICES)
        self.assertIn("font8_data.cpp", self.NOTICES)

    def test_the_file_with_the_font_data_points_at_the_notice(self):
        header = read("tools", "presto_preview", "fonts.py").split('"""', 2)[1]
        self.assertIn("Pimoroni", header)
        self.assertIn("THIRD_PARTY_NOTICES.md", header)

    def test_the_readme_no_longer_says_no_fonts_are_shipped(self):
        readme = read("README.md")
        self.assertNotIn("mascots, fonts or artwork", readme)
        self.assertIn("THIRD_PARTY_NOTICES.md", readme)

    def test_the_documentation_front_page_says_the_same(self):
        front = read("docs", "user", "index.md")
        self.assertNotIn("no provider logos, fonts or artwork", front)
        self.assertIn("THIRD_PARTY_NOTICES.md", front)

    def test_no_font_data_is_on_the_device(self):
        # The notice says the Presto uses its firmware's fonts. Hold it to that:
        # the glyph tables must not appear anywhere under presto/.
        for path in (ROOT / "presto").glob("*.py"):
            text = path.read_text(encoding="utf-8")
            self.assertNotRegex(text, r"(?m)^\s*FONTS\s*=\s*\{", path.name)
            self.assertNotIn("'glyphs'", text, path.name)
            self.assertNotIn('"glyphs"', text, path.name)

    def test_every_dependency_the_notice_names_is_one_the_project_declares(self):
        pyproject = read("daemon", "pyproject.toml")
        self.assertIn("cryptography", pyproject)
        self.assertIn("cryptography", self.NOTICES)
        self.assertIn("mpremote", pyproject)
        self.assertIn("mpremote", self.NOTICES)


if __name__ == "__main__":
    unittest.main()
