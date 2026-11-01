"""The website's analytics stay what they say they are.

Analytics here means the public site only. The daemon, its admin page and the
Presto app must send nothing anywhere, and nothing in them may even mention a
tracker. The other tests keep the documented event list, the code that sends
those events, and the browser check in step, so adding an event in one place
and forgetting the others fails here rather than in a report nobody can read.

The behaviour itself -- consent, one event per click, the demo -- is checked in
a real browser by tools/check_site_analytics.py, which needs .venv-browser.
"""

import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "tools"))

import check_site_analytics as browser_check  # noqa: E402

ANALYTICS_PAGE = ROOT / "docs" / "user" / "javascripts" / "analytics.js"
LANDING = ROOT / "website" / "index.html"
CONFIG = ROOT / "mkdocs.yml"
REFERENCE = ROOT / "docs" / "analytics.md"
PRIVACY = ROOT / "docs" / "user" / "privacy" / "index.md"
DEMO = ROOT / "docs" / "user" / "demo" / "demo.js"

# What a tracker in the installed product would look like. The bare word
# "analytics" is not on the list: a comment saying there is none is fine.
TRACKER = re.compile(
    r"googletagmanager|google-analytics|googlesyndication|googleadservices"
    r"|\bgtag\s*\(|dataLayer|G-82FD55P3RH|prestoAnalytics"
    r"|plausible\.io|posthog|mixpanel|segment\.(io|com)|sentry\.io|amplitude",
    re.IGNORECASE)


def read(path):
    return path.read_text(encoding="utf-8")


def event_table():
    """The reference page's event table, and nothing after it."""
    text = read(REFERENCE)
    start = text.index("## Events")
    end = text.index("### Parameters", start)
    return text[start:end]


def documented_events():
    """The event names in that table."""
    names = set()
    for line in event_table().splitlines():
        match = re.match(r"^\|\s*`([a-z_]+)`\s*\|", line)
        if match:
            names.add(match.group(1))
    return names


def code_events():
    """The event names the site's own code reports."""
    names = set()
    names.update(re.findall(r'report\("([a-z_]+)"', read(DEMO)))
    names.update(re.findall(r'return \["([a-z_]+)",', read(ANALYTICS_PAGE)))
    return names


class TelemetryFreeTests(unittest.TestCase):
    """The installed product sends nothing, and says nothing about sending."""

    def product_files(self):
        for folder in ("daemon/src", "presto"):
            for path in sorted((ROOT / folder).rglob("*")):
                if path.is_file() and path.suffix in (".py", ".js", ".html", ".css", ".json"):
                    yield path

    def test_no_tracker_is_mentioned_anywhere_in_the_product(self):
        found = []
        for path in self.product_files():
            try:
                text = read(path)
            except UnicodeDecodeError:
                continue
            for match in TRACKER.finditer(text):
                found.append("%s: %s" % (path.relative_to(ROOT), match.group(0)))
        self.assertEqual(found, [], "the installed product must not mention a tracker: %r" % found)

    def test_the_measurement_id_appears_only_in_the_public_site(self):
        allowed = {LANDING, CONFIG, REFERENCE, PRIVACY, ANALYTICS_PAGE,
                   ROOT / "tools" / "check_site_analytics.py",
                   ROOT / "tools" / "check_docs_site.py",
                   ROOT / "daemon" / "tests" / "tooling" / "test_site_analytics.py",
                   ROOT / "daemon" / "tests" / "tooling" / "test_check_docs_site.py"}
        found = []
        for path in ROOT.rglob("*"):
            if not path.is_file() or "build" in path.parts or ".git" in path.parts:
                continue
            if path.suffix not in (".py", ".js", ".html", ".yml", ".md", ".json"):
                continue
            if path in allowed:
                continue
            try:
                if "G-82FD55P3RH" in read(path):
                    found.append(str(path.relative_to(ROOT)))
            except UnicodeDecodeError:
                continue
        self.assertEqual(found, [], "the measurement ID leaked outside the site: %r" % found)


class CatalogueTests(unittest.TestCase):
    """One list of events, agreed on by the code, the reference and the check."""

    def test_the_reference_documents_exactly_the_events_the_check_knows(self):
        self.assertEqual(documented_events(), set(browser_check.EVENTS))

    def test_the_code_reports_no_event_that_is_not_documented(self):
        self.assertEqual(code_events() - documented_events(), set())

    def test_every_documented_event_is_actually_reported(self):
        # install_click and outbound_click come from the shared script's
        # classification rather than a literal, so they are in code_events().
        self.assertEqual(documented_events() - code_events(), set())

    def test_the_parameters_in_the_reference_match_the_event_table(self):
        table = event_table()
        for name, params in browser_check.EVENTS.items():
            row = re.search(r"^\|\s*`%s`\s*\|.*$" % re.escape(name), table, re.M)
            self.assertIsNotNone(row, "%s has no row in %s" % (name, REFERENCE.name))
            for param in params:
                if name == "install_click" and param == "install_method":
                    pass  # named in the parameters table, not repeated per row
                self.assertIn(param, row.group(0),
                              "%s's row does not mention %s" % (name, param))

    def test_only_setup_pages_have_an_install_method(self):
        guides = set(re.findall(r"getting-started/([a-z-]+)\.md", read(CONFIG)))
        self.assertTrue(guides, "the navigation has no getting-started pages")
        script = read(ANALYTICS_PAGE)
        guides.remove("what-it-does")
        self.assertNotIn('["what-it-does", "overview"]', script)
        for guide in guides:
            self.assertIn('"%s"' % guide, script,
                          "getting-started/%s.md is not classified in analytics.js" % guide)

    def test_the_outbound_destinations_are_the_documented_ones(self):
        script = read(ANALYTICS_PAGE)
        used = set(re.findall(r',\s*"((?:github|pimoroni|support)[a-z_]*)"\]', script))
        self.assertTrue(used)
        for destination in used:
            self.assertIn("`%s`" % destination, read(REFERENCE),
                          "%s is reported but not documented" % destination)


class WiringTests(unittest.TestCase):
    def test_the_product_page_loads_the_shared_script_with_the_documented_property(self):
        landing = read(LANDING)
        tag = re.search(r'<script src="docs/javascripts/analytics\.js" data-ga4="([^"]+)"', landing)
        self.assertIsNotNone(tag, "website/index.html must load the shared analytics script")
        configured = re.search(r"(?m)^\s+property:\s*(G-[A-Z0-9]+)\s*$", read(CONFIG)).group(1)
        self.assertEqual(tag.group(1), configured,
                         "the product page and the documentation must measure one property")

    def test_the_product_page_asks_first_and_says_so(self):
        landing = read(LANDING)
        self.assertIn("data-landing-consent", landing)
        self.assertIn('class="consent-text"', landing)
        self.assertIn('footer-note', landing)
        self.assertRegex(landing, r'data-consent-accept')
        self.assertRegex(landing, r'data-consent-reject')
        self.assertRegex(landing, r'data-consent-open')

    def test_the_product_page_never_loads_analytics_as_a_tag(self):
        # A <script src="…googletagmanager…"> would reach Google before the
        # reader answered. The script is started from code, after consent.
        self.assertNotIn("googletagmanager.com/gtag/js", read(LANDING))

    def test_the_documentation_loads_the_shared_script_before_the_demo(self):
        javascripts = re.search(r"(?ms)^extra_javascript:\n((?:\s+- .*\n)+)", read(CONFIG))
        self.assertIsNotNone(javascripts)
        listed = re.findall(r"-\s*(\S+)", javascripts.group(1))
        self.assertIn("javascripts/analytics.js", listed)
        self.assertLess(listed.index("javascripts/analytics.js"), listed.index("demo/demo.js"),
                        "the demo reports through the shared script, so it must load first")

    def test_the_shared_key_follows_materials_scope(self):
        # The whole scheme rests on the product page resolving the path
        # Material scopes its stored choices to. If this changes, consent
        # silently stops being shared and the browser check catches it.
        script = read(ANALYTICS_PAGE)
        self.assertIn('new URL("docs/", location.href).pathname', script)
        self.assertIn('window.__md_scope.pathname', script)
        self.assertNotRegex(read(CONFIG), r"(?m)^\s+scope:\s",
                            "extra.scope would move every stored choice and orphan "
                            "existing visitors' consent and theme")

    def test_the_privacy_page_separates_the_website_from_the_application(self):
        privacy = read(PRIVACY)
        self.assertIn("Google Analytics", privacy)
        self.assertIn("The website and the demo", privacy)
        self.assertRegex(privacy, r"(?i)the application you install")
        self.assertRegex(privacy, r"(?i)contains no analytics of any kind")


if __name__ == "__main__":
    unittest.main()
