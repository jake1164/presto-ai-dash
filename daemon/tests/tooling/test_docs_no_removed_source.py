"""The removed demo source must not creep back into the current documents.

The daemon's `fake` source was deleted; sample data is the daemon's demo mode
now. A stale sentence that still offers the source as a thing to add would send
a reader looking for a connector the daemon does not have, so the current
documents are scanned for the word. The planning and milestone records keep
their wording: they describe what was true when they were written, and
rewriting them would falsify the history. Those files are listed here, with
the reason, and nowhere else is exempt.
"""

import os
import re
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))
DOCS_DIR = os.path.join(REPO_ROOT, "docs")
README = os.path.join(REPO_ROOT, "README.md")

# Historical records, kept as written. Each is a dated plan, readiness report
# or milestone log, not a description of the daemon today.
HISTORICAL = (
    os.path.join("docs", "history"),
    os.path.join("docs", "implementation-readiness.md"),
    os.path.join("docs", "m3-readiness.md"),
    os.path.join("docs", "milestones.md"),
    os.path.join("docs", "presto_ai_dash_project_plan.md"),
)

WORD = re.compile(r"\bfake\b", re.IGNORECASE)


def markdown_files():
    """Every Markdown file the site and the repository publish, by relative path."""
    paths = [os.path.relpath(README, REPO_ROOT)]
    for folder, _subfolders, names in os.walk(DOCS_DIR):
        for name in names:
            if name.endswith(".md"):
                paths.append(os.path.relpath(os.path.join(folder, name), REPO_ROOT))
    return sorted(paths)


def is_historical(relative):
    for entry in HISTORICAL:
        if relative == entry or relative.startswith(entry + os.sep):
            return True
    return False


class NoRemovedSourceInCurrentDocsTests(unittest.TestCase):
    def test_the_scan_sees_the_documents(self):
        """A scan that finds nothing at all would pass for the wrong reason."""
        found = markdown_files()
        self.assertGreater(len(found), 40)
        self.assertIn(os.path.join("docs", "setup.md"), found)
        self.assertIn(os.path.join("docs", "user", "index.md"), found)
        self.assertTrue(any(is_historical(path) for path in found))

    def test_the_word_match_is_a_whole_word(self):
        self.assertTrue(WORD.search("the Fake source"))
        self.assertTrue(WORD.search("[sources.fake]"))
        self.assertFalse(WORD.search("fakery"))

    def test_no_current_document_mentions_the_removed_source(self):
        offenders = []
        for relative in markdown_files():
            if is_historical(relative):
                continue
            with open(os.path.join(REPO_ROOT, relative), encoding="utf-8") as handle:
                for number, line in enumerate(handle, 1):
                    if WORD.search(line):
                        offenders.append("%s:%d: %s" % (relative, number, line.strip()))
        self.assertEqual(
            offenders, [],
            "the removed demo source is still described; use demo-mode wording "
            "instead, or add the file to HISTORICAL with a reason")

    def test_the_historical_files_are_where_the_word_is_allowed(self):
        """The exemption list only covers files that exist and do use the word."""
        used = set()
        for relative in markdown_files():
            with open(os.path.join(REPO_ROOT, relative), encoding="utf-8") as handle:
                if WORD.search(handle.read()):
                    used.add(relative)
        unlisted = sorted(path for path in used if not is_historical(path))
        self.assertEqual(unlisted, [])
        for entry in HISTORICAL:
            with self.subTest(entry=entry):
                self.assertTrue(
                    entry in used or any(path.startswith(entry + os.sep) for path in used),
                    "%s is exempt but no longer mentions the source; remove the exemption"
                    % entry)


if __name__ == "__main__":
    unittest.main()
