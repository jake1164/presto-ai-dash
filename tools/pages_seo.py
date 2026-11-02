"""Generate and validate crawl files in the assembled GitHub Pages artifact.

    python tools/pages_seo.py build/pages

Only self-canonical, indexable HTML pages belong in a sitemap. Marketing pages
come from website/; MkDocs continues to own its sitemap under docs/.
"""

from html.parser import HTMLParser
from pathlib import Path
import sys
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser
import xml.etree.ElementTree as ET


ORIGIN = "https://presto-dash.com/"
NAMESPACE = "http://www.sitemaps.org/schemas/sitemap/0.9"
TAG = "{%s}" % NAMESPACE
SITEMAPS = (ORIGIN + "sitemap.xml", ORIGIN + "docs/sitemap.xml")


class PageMetadata(HTMLParser):
    def __init__(self):
        super().__init__()
        self.canonicals = []
        self.noindex = False
        self.redirect = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "link" and "canonical" in (attrs.get("rel") or "").lower().split():
            self.canonicals.append(attrs.get("href"))
        if tag == "meta":
            if (attrs.get("http-equiv") or "").lower() == "refresh":
                self.redirect = True
            if (attrs.get("name") or "").lower() in ("robots", "googlebot"):
                directives = (attrs.get("content") or "").lower().replace(",", " ").split()
                self.noindex |= bool({"noindex", "none"} & set(directives))


def indexable_urls(root, prefix="", exclude=()):
    """Require a production self-canonical for each public content page."""
    urls = []
    for page in sorted(Path(root).rglob("*.html")):
        relative = page.relative_to(root)
        if relative.parts[0] in exclude or page.name == "404.html":
            continue
        metadata = PageMetadata()
        metadata.feed(page.read_text(encoding="utf-8"))
        if metadata.noindex or metadata.redirect:
            continue
        path = prefix + relative.as_posix()
        if path.endswith("index.html"):
            path = path[:-len("index.html")]
        expected = ORIGIN + path
        if metadata.canonicals != [expected]:
            raise ValueError("%s: expected one canonical %s, found %r"
                             % (page, expected, metadata.canonicals))
        urls.append(expected)
    if not urls:
        raise ValueError("%s: no indexable pages found" % root)
    return sorted(urls)


def write_crawl_files(website, output):
    """Generate root files without touching MkDocs' sitemap or its timestamps."""
    root = ET.Element("urlset", xmlns=NAMESPACE)
    for url in indexable_urls(website):
        ET.SubElement(ET.SubElement(root, "url"), "loc").text = url
    # No content modification metadata is maintained for website/. Checkout or
    # build times would be misleading lastmod values, so leave them out.
    ET.indent(root, space="  ")
    ET.ElementTree(root).write(Path(output) / "sitemap.xml", encoding="utf-8",
                               xml_declaration=True)
    (Path(output) / "robots.txt").write_text(
        "User-agent: *\nAllow: /\n\n"
        + "".join("Sitemap: %s\n" % url for url in SITEMAPS), encoding="utf-8")


def sitemap_urls(path):
    """Read a protocol URL set, rejecting duplicates and non-production URLs."""
    root = ET.parse(path).getroot()
    if root.tag != TAG + "urlset" or not len(root):
        raise ValueError("%s: expected a nonempty sitemap urlset" % path)
    urls = []
    for entry in root:
        locations = entry.findall(TAG + "loc")
        if entry.tag != TAG + "url" or len(locations) != 1:
            raise ValueError("%s: each sitemap url needs exactly one loc" % path)
        url = locations[0].text or ""
        parts = urlsplit(url)
        if (not url.startswith(ORIGIN) or parts.query or parts.fragment
                or "?" in url or "#" in url):
            raise ValueError("%s: non-canonical sitemap URL %r" % (path, url))
        urls.append(url)
    if len(set(urls)) != len(urls):
        raise ValueError("%s: duplicate sitemap URLs" % path)
    return sorted(urls)


def check(output):
    """Validate the exact deployment output, including the preserved docs map."""
    output = Path(output)
    marketing = indexable_urls(output, exclude=("docs",))
    docs = indexable_urls(output / "docs", prefix="docs/")
    for path, expected in ((output / "sitemap.xml", marketing),
                           (output / "docs/sitemap.xml", docs)):
        if sitemap_urls(path) != expected:
            raise ValueError("%s: sitemap URLs do not match indexable canonical pages" % path)
    robots = RobotFileParser()
    robots.parse((output / "robots.txt").read_text(encoding="utf-8").splitlines())
    if sorted(robots.site_maps() or []) != sorted(SITEMAPS):
        raise ValueError("robots.txt must reference both production sitemaps exactly once")
    for agent in ("*", "Googlebot", "Bingbot"):
        for url in marketing + docs + list(SITEMAPS):
            if not robots.can_fetch(agent, url):
                raise ValueError("robots.txt blocks %s from %s" % (agent, url))
    return len(marketing), len(docs)


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("usage: pages_seo.py <built Pages directory>")
        return 2
    try:
        marketing, docs = check(args[0])
    except (OSError, ValueError, ET.ParseError) as error:
        print("Pages crawl validation failed: %s" % error)
        return 1
    print("Validated root robots.txt and sitemaps: %d marketing, %d documentation URLs."
          % (marketing, docs))
    return 0


if __name__ == "__main__":
    sys.exit(main())
