"""The admin page's help links: they must point at a page and an anchor.

The map in ``web/admin.js`` is the only place a documentation URL is built.
This test reads it and checks every entry: the page must exist under
``docs/user`` and the anchor must be a heading in that page.

The anchor is checked with a pure-Python slug function that is equivalent to
python-markdown's ``toc`` default: lowercase, NFKD-normalised, everything
that is not a word character, a space or a hyphen dropped, and runs of
spaces and hyphens collapsed into one hyphen.
"""

import os
import re
import sys
import unittest
import unicodedata

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DOCS_DIR = os.path.join(REPO_ROOT, "docs", "user")
ADMIN_JS = os.path.join(REPO_ROOT, "daemon", "src", "presto_ai_dash", "web", "admin.js")
ADMIN_HTML = os.path.join(REPO_ROOT, "daemon", "src", "presto_ai_dash", "web", "index.html")
ADMIN_CSS = os.path.join(REPO_ROOT, "daemon", "src", "presto_ai_dash", "web", "admin.css")

# The base URL of the documentation site. It is written once, in admin.js.
DOCS_BASE = "https://jake1164.github.io/presto-ai-dash/"


def slugify(text):
    """python-markdown's default slug, in pure Python."""
    value = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    value = re.sub(r"[^\w\s-]", "", value).strip().lower()
    return re.sub(r"[-\s]+", "-", value)


def headings(text):
    """The headings of a Markdown page, with their slugs.

    Fenced code blocks are ignored: a "# Windows" line inside a shell
    example is not a heading.
    """
    found = []
    fenced = False
    for line in text.splitlines():
        if line.strip().startswith("```"):
            fenced = not fenced
            continue
        if fenced:
            continue
        match = re.match(r"^(#{1,6})\s+(.*?)\s*#*\s*$", line)
        if match:
            found.append((match.group(2), slugify(match.group(2))))
    return found


def read(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


class HelpMapTests(unittest.TestCase):
    """The map in admin.js, and the links built from it."""

    def setUp(self):
        self.js = read(ADMIN_JS)
        self.html = read(ADMIN_HTML)
        self.css = read(ADMIN_CSS)

    def help_map(self):
        """The help map, read out of admin.js."""
        match = re.search(r"const HELP = \{(.*?)\n  \};", self.js, re.S)
        self.assertIsNotNone(match, "the HELP map is gone from admin.js")
        entries = re.findall(
            r'"([a-z0-9-]+)":\s*\{\s*page:\s*"([^"]+)",\s*anchor:\s*"([^"]+)"\s*\}',
            match.group(1))
        self.assertTrue(entries, "the HELP map is empty")
        return entries

    def test_every_help_page_exists(self):
        for key, page, _anchor in self.help_map():
            with self.subTest(key=key):
                path = os.path.join(DOCS_DIR, page.replace("/", os.sep))
                self.assertTrue(os.path.isfile(path),
                                "the help page for %s does not exist: %s" % (key, page))
                self.assertTrue(page.startswith("docs/user/") or "/" in page)
                self.assertFalse(page.startswith("/"), "the page must be relative to the site")

    def test_every_help_anchor_is_a_heading(self):
        for key, page, anchor in self.help_map():
            with self.subTest(key=key):
                path = os.path.join(DOCS_DIR, page.replace("/", os.sep))
                text = read(path)
                slugs = [slug for _title, slug in headings(text)]
                self.assertIn(anchor, slugs,
                              "the help link for %s points at an anchor that does not "
                              "exist in %s (the headings are: %s)" % (key, page, slugs))

    def test_the_anchors_are_unique_in_their_page(self):
        """A repeated heading gets a numbered anchor, so a link would break."""
        for key, page, anchor in self.help_map():
            with self.subTest(key=key):
                path = os.path.join(DOCS_DIR, page.replace("/", os.sep))
                slugs = [slug for _title, slug in headings(read(path))]
                self.assertEqual(slugs.count(anchor), 1,
                                 "the anchor %s appears twice in %s" % (anchor, page))

    def test_every_key_used_on_the_page_is_in_the_map(self):
        used = set(re.findall(r'data-help="([^"]+)"', self.html))
        used |= set(re.findall(r'helpLink\("([^"]+)"', self.js))
        known = {key for key, _page, _anchor in self.help_map()}
        # The source type guide is built from the type id.
        for key in list(used):
            if key.startswith("source-"):
                used.discard(key)
        # The header guide follows the currently selected tab.
        if "tab-" in used:
            used.remove("tab-")
            used.update("tab-" + tab for tab in ("services", "status", "settings"))
        self.assertTrue(used)
        self.assertEqual(sorted(used - known), [],
                         "the page uses help keys the map does not have")

    def test_the_source_type_guides_are_all_in_the_map(self):
        known = {key for key, _page, _anchor in self.help_map()}
        for source_type in ("claude", "codex", "opencode-go", "ollama-cloud",
                             "ollama", "litellm", "fake"):
            self.assertIn("source-" + source_type, known)

    def test_there_is_exactly_one_documentation_url(self):
        """No scattered URLs: the base and the map are the only places."""
        self.assertEqual(self.js.count(DOCS_BASE), 1,
                         "the documentation base URL must be written once")
        self.assertEqual(self.html.count(DOCS_BASE), 0,
                         "the page must not carry the documentation URL itself")
        for match in re.finditer(r"https?://[^\s\"')]+", self.js):
            self.assertEqual(match.group(0), DOCS_BASE,
                             "a documentation URL is written outside the base constant")

    def test_the_links_leave_nothing_behind(self):
        """No query string, no referrer, and a new tab."""
        self.assertIn('link.rel = "noopener noreferrer"', self.js)
        self.assertIn('link.referrerPolicy = "no-referrer"', self.js)
        self.assertIn('link.target = "_blank"', self.js)
        self.assertIn('link.textContent = label', self.js)
        self.assertIn('link.setAttribute("aria-label", "Help: " + label)', self.js)
        # The URL is built from the base and the map: no query string.
        self.assertNotIn("?", self.js.split("const HELP")[1].split("};")[0])
        self.assertIn('entry.page.replace(/\\.md$/, "/") + "#" + entry.anchor', self.js)

    def test_the_help_link_can_be_tapped_and_is_focusable(self):
        self.assertIn(".help-link", self.css)
        self.assertRegex(self.css, r"\.help-link\s*\{[^}]*min-height:\s*24px")
        self.assertRegex(self.css, r"\.help-link:focus[^{]*\{[^}]*outline:")

    def test_the_page_says_what_the_help_links_do(self):
        self.assertIn("Help opens the public documentation site in a new tab. Nothing from this",
                      self.html)


if __name__ == "__main__":
    unittest.main()
