"""Check a built documentation site for requests to any other host.

The site promises that, in a current browser, it loads nothing from a third
party except Google Analytics, which the privacy page and the footer disclose.
That promise was untrue once without anyone noticing: the theme's repository
widget asked api.github.com for stars and releases on every page view. This
reads the *built* site, so it also catches a theme upgrade that brings
something back.

    python tools/check_docs_site.py build/docs/site

It fails (exit 1) when it finds:

* the repository widget, ``data-md-component="source"``;
* a script, stylesheet, image, frame, media file, font or other resource that
  an HTML page loads from another host (a link the reader clicks is fine);
* ``url(...)`` or ``@import`` of another host in a stylesheet;
* a host named in the theme's script that is not on the list below, so a new
  endpoint in an upgraded theme has to be looked at by a person;
* a host other than Google Analytics' own named in a page's inline script (the
  theme loads the analytics script from there, so a link in the page is not
  where to look);
* a page that loads Google Analytics without the footer notice that says so
  and links the privacy page, or without the cookie banner that asks first.

It needs only the standard library.
"""

import re
import sys
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit

WIDGET = 'data-md-component="source"'

# <link rel=...> values that make the browser fetch the target. canonical,
# alternate, next and prev do not.
LOADING_RELS = {"stylesheet", "preload", "prefetch", "preconnect", "dns-prefetch",
                "modulepreload", "icon", "shortcut", "apple-touch-icon", "manifest",
                "mask-icon", "prerender"}

# Hosts the theme's script names, and why each is acceptable. Anything else in
# the script is reported.
# The theme's main script, the one every page loads and runs.
THEME_BUNDLE = re.compile(r"bundle\.[0-9a-f]+\.min\.js")
# A call that fetches a literal external address.
REQUEST_CALL = re.compile(
    r"""(?:(?:fetch|importScripts|sendBeacon|EventSource|WebSocket)\(\s*"""
    r"""|\.open\(\s*["'][A-Za-z]+["']\s*,\s*)["'`]((?:https?|wss?)://[^"'`\s)]+)""")

# The only third party the site may ask for anything: Google Analytics 4
# (extra.analytics in mkdocs.yml), allowed to load its script and report page
# views. A page that uses it must also carry DISCLOSURE.
ANALYTICS_HOSTS = {"www.googletagmanager.com", "www.google-analytics.com",
                   "analytics.google.com"}
CONSENT_BANNER = 'data-md-component="consent"'
DISCLOSURE = re.compile(r"""md-copyright__highlight">[^<]*Google Analytics[^<]*"""
                        r"""<a href="[^"]*">Privacy</a>""")

KNOWN_SCRIPT_HOSTS = {
    "unpkg.com": "the polyfill a browser without ResizeObserver (before about 2020) "
                 "fetches, and Mermaid, which this site does not enable",
    "api.github.com": "the repository widget's code; it only runs for the element "
                      "this check forbids",
    "clipboardjs.com": "a link in a comment, never requested",
}


def other_host(url, own=""):
    """The host a URL would be fetched from, or None when it is local."""
    url = (url or "").strip()
    if not url or url.startswith(("#", "data:", "about:", "javascript:", "mailto:")):
        return None
    if url.startswith("//"):
        url = "https:" + url
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None
    return parts.hostname.lower() if parts.hostname.lower() != own else None


class Loads(HTMLParser):
    """Collects what a page makes the browser fetch."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.found = []
        self.inline_scripts = []
        self._in_script = False

    def handle_endtag(self, tag):
        if tag == "script":
            self._in_script = False

    def handle_data(self, data):
        if self._in_script:
            self.inline_scripts.append(data)

    def handle_starttag(self, tag, attrs):
        attrs = {name.lower(): value for name, value in attrs}
        targets = []
        if tag == "script" and not attrs.get("src"):
            self._in_script = True
        if tag in ("script", "img", "iframe", "frame", "embed", "source", "audio",
                   "video", "track", "input") and attrs.get("src"):
            targets.append(attrs["src"])
        if tag == "object" and attrs.get("data"):
            targets.append(attrs["data"])
        if tag in ("img", "source") and attrs.get("srcset"):
            targets += [part.split()[0] for part in attrs["srcset"].split(",") if part.strip()]
        if tag == "link":
            rels = set((attrs.get("rel") or "").lower().split())
            if rels & LOADING_RELS and attrs.get("href"):
                targets.append(attrs["href"])
        if tag == "video" and attrs.get("poster"):
            targets.append(attrs["poster"])
        for target in targets:
            host = other_host(target)
            if host:
                self.found.append((tag, target, host))


def check(site):
    """The problems found in a built site, as strings."""
    root = Path(site)
    if not root.is_dir():
        return ["%s is not a directory: build the site first" % site]
    problems = []
    pages = sorted(root.rglob("*.html"))
    if not pages:
        return ["%s holds no HTML pages" % site]
    for page in pages:
        text = page.read_text(encoding="utf-8", errors="replace")
        name = page.relative_to(root).as_posix()
        if WIDGET in text:
            problems.append("%s: the repository widget is back (%s); it asks api.github.com "
                            "for the repository's facts on every page view" % (name, WIDGET))
        parser = Loads()
        parser.feed(text)
        uses_analytics = False
        for tag, target, host in parser.found:
            if host in ANALYTICS_HOSTS:
                uses_analytics = True
            else:
                problems.append("%s: <%s> loads %s from %s" % (name, tag, target, host))
        for host in sorted(set(re.findall(r"https?://([A-Za-z0-9.-]+)", " ".join(parser.inline_scripts)))):
            if host.lower() in ANALYTICS_HOSTS:
                uses_analytics = True
            elif host.strip("."):
                problems.append("%s: an inline script names %s, a host that is not Google "
                                "Analytics" % (name, host))
        if uses_analytics and CONSENT_BANNER not in text:
            problems.append("%s: loads Google Analytics without the cookie banner that asks "
                            "first (extra.consent in mkdocs.yml)" % name)
        if uses_analytics and not DISCLOSURE.search(text):
            problems.append("%s: loads Google Analytics without the footer notice that links "
                            "the privacy page (docs/overrides/partials/copyright.html)" % name)
    for sheet in sorted(root.rglob("*.css")):
        text = sheet.read_text(encoding="utf-8", errors="replace")
        name = sheet.relative_to(root).as_posix()
        for match in re.finditer(r"""(?:url\(\s*|@import\s+(?:url\(\s*)?)["']?([^"')\s]+)""", text):
            host = other_host(match.group(1))
            if host:
                problems.append("%s: stylesheet fetches %s from %s" % (name, match.group(1), host))
    for script in sorted(root.rglob("*.js")):
        text = script.read_text(encoding="utf-8", errors="replace")
        name = script.relative_to(root).as_posix()
        if THEME_BUNDLE.fullmatch(script.name):
            # The script every page runs. Every host it names is accounted for.
            for host in sorted(set(re.findall(r"https?://([A-Za-z0-9.-]+)", text))):
                if host.lower() not in KNOWN_SCRIPT_HOSTS and host.strip("."):
                    problems.append("%s names a host this check has not seen: %s. If it is a "
                                    "request the page can make, the site's privacy claim needs "
                                    "changing; if not, add it to KNOWN_SCRIPT_HOSTS with a "
                                    "reason" % (name, host))
        else:
            # The search plugin's language files and the like are full of URLs in
            # comments. What matters is a call that would fetch one.
            for match in REQUEST_CALL.finditer(text):
                problems.append("%s makes a request to %s" % (name, match.group(1)))
    return problems


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        print(__doc__.strip().splitlines()[0])
        print("usage: check_docs_site.py <built site directory>")
        return 2
    problems = check(argv[0])
    if problems:
        print("The built site is not clean:")
        for problem in problems:
            print("  - " + problem)
        return 1
    print("The built site asks nothing of any other host but Google Analytics (checked %d pages)."
          % len(list(Path(argv[0]).rglob("*.html"))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
