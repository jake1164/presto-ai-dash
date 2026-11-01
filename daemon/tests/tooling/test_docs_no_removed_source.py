"""The removed demo source must not creep back into the current documents.

The daemon's `fake` source was deleted; sample data is the daemon's demo mode
now. A stale sentence that still offers the source as a thing to add would send
a reader looking for a connector the daemon does not have, so the current
documents are scanned for the word.
"""

import os
import re
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))
DOCS_DIR = os.path.join(REPO_ROOT, "docs")
README = os.path.join(REPO_ROOT, "README.md")

WORD = re.compile(r"\bfake\b", re.IGNORECASE)


def markdown_files():
    """Every Markdown file the site and the repository publish, by relative path."""
    paths = [os.path.relpath(README, REPO_ROOT)]
    for folder, _subfolders, names in os.walk(DOCS_DIR):
        for name in names:
            if name.endswith(".md"):
                paths.append(os.path.relpath(os.path.join(folder, name), REPO_ROOT))
    return sorted(paths)


class NoRemovedSourceInCurrentDocsTests(unittest.TestCase):
    def test_the_scan_sees_the_documents(self):
        """A scan that finds nothing at all would pass for the wrong reason."""
        found = markdown_files()
        self.assertGreater(len(found), 30)
        self.assertIn(os.path.join("docs", "README.md"), found)
        self.assertIn(os.path.join("docs", "user", "index.md"), found)

    def test_the_word_match_is_a_whole_word(self):
        self.assertTrue(WORD.search("the Fake source"))
        self.assertTrue(WORD.search("[sources.fake]"))
        self.assertFalse(WORD.search("fakery"))

    def test_no_current_document_mentions_the_removed_source(self):
        offenders = []
        for relative in markdown_files():
            with open(os.path.join(REPO_ROOT, relative), encoding="utf-8") as handle:
                for number, line in enumerate(handle, 1):
                    if WORD.search(line):
                        offenders.append("%s:%d: %s" % (relative, number, line.strip()))
        self.assertEqual(
            offenders, [],
            "the removed demo source is still described; use demo-mode wording instead")


if __name__ == "__main__":
    unittest.main()
