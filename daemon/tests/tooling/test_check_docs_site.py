"""The documentation site makes no request to a third party, and a check keeps it so.

The site promised "loads nothing from a third party" while the theme's
repository widget asked api.github.com for the repository's stars on every page
view. tools/check_docs_site.py reads the built site and fails the build if
anything of the kind comes back. These tests give the checker pages that should
pass and pages that should not, and check the repository's own wiring.
"""

import re
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "tools"))

import check_docs_site as checker  # noqa: E402


def page(body="", head=""):
    return "<!doctype html><html><head>%s</head><body>%s</body></html>" % (head, body)


class CheckerCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def write(self, name, text):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def problems(self, **pages):
        for name, text in pages.items():
            self.write(name.replace("__", "/").replace("_html", ".html"), text)
        return checker.check(self.root)


class HtmlTests(CheckerCase):
    def test_a_site_of_local_resources_and_ordinary_links_is_clean(self):
        html = page(
            '<a href="https://github.com/x/y">repo</a><a href="//example.org/">protocol relative link</a>'
            '<img src="images/a.png"><img src="data:image/png;base64,AAAA">',
            '<link rel="stylesheet" href="assets/main.css"><script src="assets/bundle.js"></script>'
            '<link rel="canonical" href="https://jake1164.github.io/presto-ai-dash/">'
            '<link rel="alternate" href="https://example.org/feed"><link rel="icon" href="a.png">')
        self.assertEqual(self.problems(index_html=html), [])

    def test_the_repository_widget_is_refused(self):
        html = page('<a class="md-source" data-md-component="source" href="x">x</a>')
        found = self.problems(index_html=html)
        self.assertEqual(len(found), 1)
        self.assertIn("repository widget", found[0])
        self.assertIn("api.github.com", found[0])

    def test_a_script_from_another_host_is_refused(self):
        for src in ("https://cdn.example/x.js", "http://cdn.example/x.js", "//cdn.example/x.js"):
            with self.subTest(src=src):
                found = self.problems(index_html=page(head='<script src="%s"></script>' % src))
                self.assertEqual(len(found), 1, found)
                self.assertIn("cdn.example", found[0])

    def test_every_kind_of_loaded_resource_is_refused(self):
        cases = [
            '<link rel="stylesheet" href="https://f.example/a.css">',
            '<link rel="preload" href="https://f.example/a.woff2" as="font">',
            '<link rel="preconnect" href="https://f.example">',
            '<link rel="dns-prefetch" href="https://f.example">',
            '<link rel="icon" href="https://f.example/a.png">',
            '<link rel="manifest" href="https://f.example/m.json">',
            '<link rel="STYLESHEET" href="https://f.example/a.css">',
            '<img src="https://f.example/a.png">',
            '<img srcset="a.png 1x, https://f.example/b.png 2x">',
            '<iframe src="https://f.example/"></iframe>',
            '<video poster="https://f.example/p.png" src="v.mp4"></video>',
            '<audio src="https://f.example/a.mp3"></audio>',
            '<source src="https://f.example/a.webm">',
            '<object data="https://f.example/o"></object>',
            '<embed src="https://f.example/e">',
        ]
        for markup in cases:
            with self.subTest(markup=markup):
                found = self.problems(index_html=page(markup))
                self.assertEqual(len(found), 1, (markup, found))
                self.assertIn("f.example", found[0])

    def test_a_problem_names_the_page_it_is_on(self):
        found = self.problems(guides__thing_html=page('<img src="https://f.example/a.png">'))
        self.assertTrue(found[0].startswith("guides/thing.html"), found)


class AnalyticsTests(CheckerCase):
    """Google Analytics is the one third party allowed, and only with the notice."""

    SCRIPT = ('<script id="__analytics">var t=document.createElement("script");'
              't.src="https://www.googletagmanager.com/gtag/js?id=G-TEST";</script>')
    NOTICE = ('<div class="md-copyright"><div class="md-copyright__highlight">'
              'This site uses Google Analytics to count page views. '
              '<a href="privacy/">Privacy</a></div></div>')

    def test_analytics_with_the_notice_is_clean(self):
        self.assertEqual(self.problems(index_html=page(self.NOTICE, self.SCRIPT)), [])

    def test_analytics_without_the_notice_is_refused(self):
        found = self.problems(index_html=page("", self.SCRIPT))
        self.assertEqual(len(found), 1, found)
        self.assertIn("without the footer notice", found[0])

    def test_a_notice_without_a_privacy_link_is_refused(self):
        notice = self.NOTICE.replace('<a href="privacy/">Privacy</a>', "")
        self.assertEqual(len(self.problems(index_html=page(notice, self.SCRIPT))), 1)

    def test_the_script_may_also_be_a_tag_in_the_page(self):
        tag = '<script async src="https://www.googletagmanager.com/gtag/js?id=G-TEST"></script>'
        self.assertEqual(self.problems(index_html=page(self.NOTICE, tag)), [])
        found = self.problems(index_html=page("", tag))
        self.assertIn("without the footer notice", found[0])

    def test_no_other_host_hides_in_an_inline_script(self):
        script = self.SCRIPT.replace("</script>", 'fetch("https://t.example/hit");</script>')
        found = self.problems(index_html=page(self.NOTICE, script))
        self.assertEqual(len(found), 1, found)
        self.assertIn("t.example", found[0])

    def test_a_page_without_analytics_needs_no_notice(self):
        self.assertEqual(self.problems(index_html=page("", "<script>var a=1;</script>")), [])


class StylesheetTests(CheckerCase):
    def test_local_urls_and_data_urls_are_fine(self):
        self.write("index.html", page())
        self.write("assets/a.css", 'a{background:url(img/x.png)} b{background:url("data:image/svg+xml;base64,AA")}')
        self.assertEqual(checker.check(self.root), [])

    def test_a_stylesheet_fetching_from_another_host_is_refused(self):
        self.write("index.html", page())
        for css in ('@import url("https://f.example/a.css");', "@import 'https://f.example/a.css';",
                    "a{background:url(https://f.example/x.png)}",
                    'a{src:url( "//f.example/font.woff2" )}'):
            with self.subTest(css=css):
                self.write("assets/a.css", css)
                found = checker.check(self.root)
                self.assertEqual(len(found), 1, found)
                self.assertIn("f.example", found[0])


class ScriptTests(CheckerCase):
    BUNDLE = "assets/javascripts/bundle.abc123.min.js"

    def setUp(self):
        super().setUp()
        self.write("index.html", page())

    def test_hosts_the_bundle_names_that_are_accounted_for_pass(self):
        self.write(self.BUNDLE, 'a("https://unpkg.com/p");b(`https://api.github.com/repos`);'
                                '/* https://clipboardjs.com */')
        self.assertEqual(checker.check(self.root), [])

    def test_a_new_host_in_the_bundle_has_to_be_looked_at(self):
        self.write(self.BUNDLE, 'a("https://unpkg.com/p");fetch("https://stats.example/hit")')
        found = checker.check(self.root)
        self.assertEqual(len(found), 1, found)
        self.assertIn("stats.example", found[0])
        self.assertIn("KNOWN_SCRIPT_HOSTS", found[0])

    def test_urls_in_comments_of_other_scripts_are_ignored(self):
        self.write("assets/javascripts/lunr/min/lunr.fr.min.js",
                   "/* see https://snowball.tartarus.org/ and https://github.com/x */ var a=1;")
        self.assertEqual(checker.check(self.root), [])

    def test_a_request_to_an_outside_address_in_another_script_is_refused(self):
        for code in ('fetch("https://t.example/a")', "importScripts('https://t.example/a.js')",
                     'navigator.sendBeacon("https://t.example/b")', 'new WebSocket("wss://t.example/c")',
                     'x.open("GET","https://t.example/d")', "new EventSource('https://t.example/e')",
                     "fetch(`https://t.example/f`)"):
            with self.subTest(code=code):
                self.write("assets/other.js", code)
                found = checker.check(self.root)
                self.assertEqual(len(found), 1, (code, found))
                self.assertIn("t.example", found[0])

    def test_a_request_to_a_local_path_is_fine(self):
        self.write("assets/other.js", 'fetch("search/search_index.json");importScripts("lunr.js")')
        self.assertEqual(checker.check(self.root), [])


class CommandLineTests(CheckerCase):
    def test_exit_codes(self):
        self.write("index.html", page())
        self.assertEqual(checker.main([str(self.root)]), 0)
        self.write("index.html", page(head='<script src="https://f.example/a.js"></script>'))
        self.assertEqual(checker.main([str(self.root)]), 1)
        self.assertEqual(checker.main([]), 2)

    def test_a_missing_or_empty_directory_fails_instead_of_passing(self):
        self.assertTrue(checker.check(self.root / "nope"))
        self.assertTrue(checker.check(self.root))  # exists, holds no pages


class RepositoryWiringTests(unittest.TestCase):
    """The pieces that keep the claim true are in place."""

    def test_the_override_removes_exactly_the_attribute_that_calls_github(self):
        override = (ROOT / "docs" / "overrides" / "partials" / "source.html").read_text(encoding="utf-8")
        markup = re.sub(r"\{#-.*?-#\}", "", override, flags=re.S)  # not the comment explaining it
        self.assertNotIn("data-md-component", markup)
        self.assertIn('class="md-source"', markup)
        self.assertIn("config.repo_url", markup)

    def test_the_site_is_built_with_the_override_and_not_published_with_it(self):
        config = (ROOT / "mkdocs.yml").read_text(encoding="utf-8")
        self.assertRegex(config, r"(?m)^\s+custom_dir:\s*docs/overrides\s*$")
        self.assertRegex(config, r"(?m)^docs_dir:\s*docs/user\s*$")  # so the override is not a page

    def test_the_documentation_build_runs_the_check(self):
        workflow = (ROOT / ".github" / "workflows" / "docs.yml").read_text(encoding="utf-8")
        self.assertIn("python tools/check_docs_site.py build/docs/site", workflow)
        # and a change to the override or the checker triggers the build
        self.assertGreaterEqual(workflow.count("docs/overrides/**"), 2)
        self.assertGreaterEqual(workflow.count("tools/check_docs_site.py"), 3)

    def test_analytics_is_configured_and_the_footer_discloses_it(self):
        config = (ROOT / "mkdocs.yml").read_text(encoding="utf-8")
        self.assertRegex(config, r"(?m)^\s+provider:\s*google\s*$")
        self.assertRegex(config, r"(?m)^\s+property:\s*G-[A-Z0-9]+\s*$")
        footer = (ROOT / "docs" / "overrides" / "partials" / "copyright.html").read_text(encoding="utf-8")
        self.assertIn("Google Analytics", footer)
        self.assertIn("privacy/", footer)

    def test_the_privacy_page_discloses_analytics(self):
        privacy = (ROOT / "docs" / "user" / "privacy" / "index.md").read_text(encoding="utf-8")
        self.assertIn("Google Analytics", privacy)
        self.assertIn("policies.google.com/privacy", privacy)

    def test_the_privacy_claims_say_what_remains(self):
        privacy = (ROOT / "docs" / "user" / "privacy" / "index.md").read_text(encoding="utf-8")
        self.assertIn("unpkg.com", privacy)
        self.assertIn("ResizeObserver", privacy)


if __name__ == "__main__":
    unittest.main()
