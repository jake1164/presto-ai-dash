"""Crawl files must describe the canonical pages in the published artifact."""

import functools
import gzip
import http.server
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from urllib.request import urlopen
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "tools"))
from build_pages_site import build
from pages_seo import check, indexable_urls, main, sitemap_urls, TAG


def page(url, extra=""):
    return '<html><head><link rel="canonical" href="%s">%s</head></html>' % (url, extra)


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


class PagesSEOTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)
        self.website = self.root / "website"
        self.docs = self.root / "mkdocs"
        self.output = self.root / "pages"
        self.write(self.website / "index.html", page("https://presto-dash.com/"))
        self.write(self.website / "features/index.html", page("https://presto-dash.com/features/"))
        self.write(self.website / "about.html", page("https://presto-dash.com/about.html"))
        self.write(self.docs / "index.html", page("https://presto-dash.com/docs/"))
        self.write(self.docs / "demo/index.html", page("https://presto-dash.com/docs/demo/"))
        self.docs_sitemap = b'''<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://presto-dash.com/docs/</loc></url>
  <url><loc>https://presto-dash.com/docs/demo/</loc></url>
</urlset>
'''
        (self.docs / "sitemap.xml").write_bytes(self.docs_sitemap)
        (self.docs / "sitemap.xml.gz").write_bytes(gzip.compress(self.docs_sitemap))

    def write(self, path, text):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def assemble(self):
        build(self.docs, self.website, self.output)

    def test_generated_xml_covers_all_marketing_pages_without_invented_dates(self):
        self.assemble()
        self.assertEqual(check(self.output), (3, 2))
        self.assertEqual(sitemap_urls(self.output / "sitemap.xml"), [
            "https://presto-dash.com/", "https://presto-dash.com/about.html",
            "https://presto-dash.com/features/",
        ])
        root = ET.parse(self.output / "sitemap.xml").getroot()
        self.assertEqual(root.tag, TAG + "urlset")
        self.assertEqual(root.findall(".//" + TAG + "lastmod"), [])
        self.assertEqual(main([str(self.output)]), 0)

    def test_mkdocs_sitemap_and_compressed_copy_are_preserved_byte_for_byte(self):
        self.assemble()
        for name in ("sitemap.xml", "sitemap.xml.gz"):
            self.assertEqual((self.output / "docs" / name).read_bytes(),
                             (self.docs / name).read_bytes())
        self.assertEqual((self.output / "docs/sitemap.xml").read_bytes(), self.docs_sitemap)

    def test_redirects_errors_and_nonindexable_pages_are_excluded(self):
        for name, content in {
            "404.html": "Not found",
            "internal/index.html": page("https://presto-dash.com/internal/",
                                         '<meta name="robots" content="noindex, nofollow">'),
            "private.html": '<meta name="googlebot" content="none">',
            "old.html": '<meta http-equiv="refresh" content="0;url=/">',
        }.items():
            self.write(self.website / name, content)
        self.assemble()
        self.assertEqual(check(self.output), (3, 2))
        text = (self.output / "sitemap.xml").read_text()
        for excluded in ("404", "internal", "private", "old.html", "landing", "demo", "docs"):
            self.assertNotIn(excluded, text)

    def test_missing_duplicate_and_noncanonical_marketing_canonicals_fail(self):
        for canonical in ("", page("https://presto-dash.com/") * 2,
                          page("http://presto-dash.com/"),
                          page("https://jake1164.github.io/presto-ai-dash/"),
                          page("https://presto-dash.com/?ref=test"),
                          page("https://presto-dash.com/#features"),
                          page("https://presto-dash.com/index.html"),
                          page("https://presto-dash.com/docs/")):
            with self.subTest(canonical=canonical):
                self.write(self.website / "index.html", canonical)
                with self.assertRaisesRegex(ValueError, "expected one canonical"):
                    self.assemble()

    def test_invalid_or_incomplete_sitemap_fails_artifact_validation(self):
        self.assemble()
        path = self.output / "sitemap.xml"
        valid = path.read_text(encoding="utf-8")
        mutations = (
            "<html>not XML", valid.replace(' xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"', ""),
            valid.replace("https://presto-dash.com/", "http://presto-dash.com/"),
            valid.replace("https://presto-dash.com/", "https://jake1164.github.io/presto-ai-dash/"),
            valid.replace("/about.html", "/about.html?ref=test"),
            valid.replace("/about.html", "/about.html#features"),
            valid.replace("/about.html", "/demo/"),
            valid.replace("/about.html", "/admin/"),
            valid.replace("/about.html", "/features/"),
            valid.replace("<loc>", "<missing> ").replace("</loc>", "</missing>"),
            valid.replace("</urlset>", '<url><loc>https://presto-dash.com/missing/</loc></url></urlset>'),
        )
        for text in mutations:
            with self.subTest(text=text):
                path.write_text(text, encoding="utf-8")
                with self.assertRaises((ValueError, ET.ParseError)):
                    check(self.output)
        path.unlink()
        self.assertEqual(main([str(self.output)]), 1)

    def test_missing_or_obsolete_docs_sitemap_fails_artifact_validation(self):
        self.assemble()
        path = self.output / "docs/sitemap.xml"
        path.write_bytes(self.docs_sitemap.replace(b"https://presto-dash.com/",
                                                  b"https://jake1164.github.io/presto-ai-dash/"))
        with self.assertRaisesRegex(ValueError, "non-canonical"):
            check(self.output)
        path.unlink()
        with self.assertRaises(OSError):
            check(self.output)

    def test_robots_references_both_maps_without_blocking_public_content(self):
        self.assemble()
        path = self.output / "robots.txt"
        valid = path.read_text(encoding="utf-8")
        self.assertEqual(valid, "User-agent: *\nAllow: /\n\n"
                         "Sitemap: https://presto-dash.com/sitemap.xml\n"
                         "Sitemap: https://presto-dash.com/docs/sitemap.xml\n")
        for text in (valid.replace("Allow: /", "Disallow: /"),
                     valid.replace("Allow: /", "Disallow: /docs/"),
                     valid.replace("Allow: /", "Disallow: /sitemap.xml"),
                     valid.replace("Sitemap: https://presto-dash.com/sitemap.xml\n", ""),
                     valid.replace("https://presto-dash.com/", "http://presto-dash.com/"),
                     valid + "Sitemap: https://presto-dash.com/sitemap.xml\n",
                     valid + "\nUser-agent: Googlebot\nDisallow: /\n"):
            with self.subTest(text=text):
                path.write_text(text, encoding="utf-8")
                with self.assertRaises(ValueError):
                    check(self.output)

    def test_deployment_root_serves_both_maps_and_robots_with_correct_types(self):
        self.assemble()
        handler = functools.partial(QuietHandler, directory=str(self.output))
        with http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                base = "http://127.0.0.1:%d" % server.server_address[1]
                for route in ("/sitemap.xml", "/docs/sitemap.xml", "/robots.txt"):
                    with self.subTest(route=route), urlopen(base + route, timeout=5) as response:
                        self.assertEqual(response.status, 200)
                        self.assertEqual(response.geturl(), base + route)
                        content = response.read()
                        if route.endswith(".xml"):
                            self.assertIn(response.headers.get_content_type(),
                                          ("application/xml", "text/xml"))
                            self.assertEqual(ET.fromstring(content).tag, TAG + "urlset")
                        else:
                            self.assertEqual(response.headers.get_content_type(), "text/plain")
                            self.assertIn(b"Sitemap: https://presto-dash.com/docs/sitemap.xml", content)
            finally:
                server.shutdown()
                thread.join(timeout=5)

    def test_current_marketing_inventory_is_the_homepage(self):
        self.assertEqual(indexable_urls(ROOT / "website"), ["https://presto-dash.com/"])


if __name__ == "__main__":
    unittest.main()
