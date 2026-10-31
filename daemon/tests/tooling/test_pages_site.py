"""The product and docs must coexist without breaking existing deep links."""

from pathlib import Path
import sys
import tempfile
import unittest
from urllib.parse import urljoin

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "tools"))
from build_pages_site import build


class PagesSiteTests(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory()
        self.addCleanup(self.scratch.cleanup)
        self.root = Path(self.scratch.name)
        self.docs = self.root / "mkdocs"
        self.website = self.root / "website"
        self.output = self.root / "pages"
        for folder in (self.docs, self.website):
            folder.mkdir()
        (self.website / "index.html").write_text("Product placeholder", encoding="utf-8")
        for name, content in {
            "index.html": "Docs home",
            "404.html": "Page not found",
            "guides/claude/index.html": '<h2 id="setup">Setup</h2>',
            "demo/index.html": '<script src="demo.js"></script>',
            "demo/demo.js": "demo runtime",
            "demo/vendor/micropython.wasm": "wasm fixture",
            "assets/theme.css": "theme fixture",
        }.items():
            path = self.docs / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")

    def test_product_docs_and_demo_have_separate_routes(self):
        build(self.docs, self.website, self.output)
        self.assertEqual((self.output / "index.html").read_text(), "Product placeholder")
        self.assertEqual((self.output / "docs/index.html").read_text(), "Docs home")
        for name in ("demo/demo.js", "demo/vendor/micropython.wasm", "assets/theme.css"):
            self.assertEqual((self.output / "docs" / name).read_bytes(), (self.docs / name).read_bytes())
        self.assertTrue((self.output / ".nojekyll").is_file())
        self.assertEqual((self.output / "404.html").read_text(), "Page not found")

    def test_legacy_links_preserve_project_prefix_queries_and_anchors(self):
        build(self.docs, self.website, self.output)
        base = "https://example.test/presto-ai-dash/"
        for name, target in (("guides/claude/", "../../docs/guides/claude/"),
                             ("demo/", "../docs/demo/"), ("landing/", "../")):
            text = (self.output / name / "index.html").read_text(encoding="utf-8")
            self.assertIn('href="%s"' % target, text)
            self.assertIn("+ location.search + location.hash", text)
            expected = base + ("docs/" + name if name != "landing/" else "")
            self.assertEqual(urljoin(base + name, target), expected)

    def test_rebuild_removes_stale_product_files(self):
        build(self.docs, self.website, self.output)
        (self.output / "obsolete.html").touch()
        build(self.docs, self.website, self.output)
        self.assertFalse((self.output / "obsolete.html").exists())

    def test_product_cannot_take_over_docs_or_legacy_routes(self):
        (self.website / "docs").mkdir()
        with self.assertRaisesRegex(ValueError, "must not overwrite"):
            build(self.docs, self.website, self.output)
        (self.website / "docs").rmdir()
        (self.website / "guides/claude").mkdir(parents=True)
        (self.website / "guides/claude/index.html").touch()
        with self.assertRaisesRegex(ValueError, "legacy docs URL"):
            build(self.docs, self.website, self.output)

    def test_missing_cross_site_links_and_domain_root_paths_fail_the_build(self):
        for target in ("docs/missing/", "/docs/"):
            with self.subTest(target=target):
                (self.website / "index.html").write_text('<a href="%s">Docs</a>' % target)
                with self.assertRaisesRegex(ValueError, "broken Pages links"):
                    build(self.docs, self.website, self.output)

    def test_removing_an_existing_page_or_heading_fails_the_build(self):
        legacy = {"guides/claude/index.html": {
            "page": "guides/claude/index.html", "anchors": ["setup"]}}
        page = self.docs / "guides/claude/index.html"
        page.write_text('<h2 id="renamed">Setup</h2>')
        with self.assertRaisesRegex(ValueError, "missing heading"):
            build(self.docs, self.website, self.output, legacy)
        page.unlink()
        with self.assertRaisesRegex(ValueError, "missing replacement"):
            build(self.docs, self.website, self.output, legacy)

    def test_a_moved_page_keeps_its_published_route(self):
        legacy = {"old-guide/index.html": {
            "page": "guides/claude/index.html", "anchors": ["setup"]}}
        build(self.docs, self.website, self.output, legacy)
        text = (self.output / "old-guide/index.html").read_text(encoding="utf-8")
        self.assertIn('../docs/guides/claude/', text)
        self.assertIn("+ location.search + location.hash", text)

    def test_old_homepage_headings_forward_without_redirecting_the_product_home(self):
        (self.website / "index.html").write_text("<html><head></head><body>Product</body></html>")
        (self.docs / "index.html").write_text('<h2 id="start-here">Start</h2>')
        legacy = {"index.html": {"page": "index.html", "anchors": ["start-here"]}}
        build(self.docs, self.website, self.output, legacy)
        home = (self.output / "index.html").read_text(encoding="utf-8")
        self.assertIn('var anchors = ["start-here"]', home)
        self.assertIn("if (anchors.indexOf(fragment) !== -1)", home)
        self.assertIn('location.replace("docs/" + location.search + location.hash)', home)
        self.assertIn("Product", home)


if __name__ == "__main__":
    unittest.main()
