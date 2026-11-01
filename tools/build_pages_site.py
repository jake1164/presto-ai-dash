"""Assemble one GitHub Pages artifact from the product site and built MkDocs.

Run after ``mkdocs build --strict``. The product owns the root; MkDocs owns
``docs/``. Old documentation URLs redirect to the corresponding new pages,
including /demo/. Redirects preserve query strings and heading fragments.
"""

import html
from html.parser import HTMLParser
import json
from pathlib import Path
import shutil
from urllib.parse import unquote, urljoin, urlsplit


ROOT = Path(__file__).resolve().parents[1]


class LocalLinks(HTMLParser):
    def __init__(self):
        super().__init__()
        self.targets = []
        self.ids = set()

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if attributes.get("id"):
            self.ids.add(attributes["id"])
        self.targets.extend(value for name, value in attrs
                            if name in ("href", "src", "poster") and value)


def check_local_links(output, base="https://presto-dash.com/"):
    """Check local targets at the deployed domain (or a supplied project URL)."""
    origin = urlsplit(base)
    prefix = origin.path
    problems = []
    for page in sorted(output.rglob("*.html")):
        parser = LocalLinks()
        parser.feed(page.read_text(encoding="utf-8"))
        source = page.relative_to(output).as_posix()
        for target in parser.targets:
            resolved = urlsplit(urljoin(base + source, target))
            if resolved.scheme != origin.scheme or resolved.netloc != origin.netloc:
                continue
            path = unquote(resolved.path)
            if not path.startswith(prefix):
                problems.append("%s: link escapes the project prefix: %s" % (source, target))
                continue
            destination = output / path[len(prefix):]
            if destination.is_dir():
                destination = destination / "index.html"
            if not destination.is_file():
                problems.append("%s: missing local target: %s" % (source, target))
    if problems:
        raise ValueError("broken Pages links:\n" + "\n".join(problems))


def redirect_page(target):
    escaped = html.escape(target, quote=True)
    return """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Page moved — Presto AI Dash</title>
<link rel="canonical" href="%s">
<script id="legacy-docs-redirect">location.replace(%s + location.search + location.hash);</script>
<meta http-equiv="refresh" content="0; url=%s">
</head><body><p>This page has moved. <a href="%s">Continue to Presto AI Dash</a>.</p></body></html>
""" % (escaped, json.dumps(target), escaped, escaped)


def check_legacy_targets(docs, legacy):
    """Published paths and headings remain a compatibility contract."""
    problems = []
    for old, entry in legacy.items():
        page = docs / entry["page"]
        if not page.is_file():
            problems.append("%s: missing replacement %s" % (old, entry["page"]))
            continue
        parser = LocalLinks()
        parser.feed(page.read_text(encoding="utf-8"))
        for anchor in entry["anchors"]:
            if anchor not in parser.ids:
                problems.append("%s#%s: missing heading in %s" % (old, anchor, entry["page"]))
    if problems:
        raise ValueError("existing documentation links would break:\n" + "\n".join(problems))


def home_redirect(anchors):
    """Old documentation-home fragments now live below /docs/."""
    return """<script id="legacy-docs-redirect">
(function () {
  var anchors = %s;
  function redirect() {
    var fragment;
    try { fragment = decodeURIComponent(location.hash.slice(1)); }
    catch (error) { return; }
    if (anchors.indexOf(fragment) !== -1) {
      location.replace("docs/" + location.search + location.hash);
    }
  }
  redirect();
  window.addEventListener("hashchange", redirect);
}());
</script>""" % json.dumps(anchors).replace("<", "\\u003c")


def build(docs, website, output, legacy=None):
    docs, website, output = Path(docs), Path(website), Path(output)
    legacy = legacy or {}
    if not (docs / "index.html").is_file():
        raise ValueError("build MkDocs with --strict before assembling Pages")
    if not (website / "index.html").is_file():
        raise ValueError("the product site needs index.html")
    check_legacy_targets(docs, legacy)
    # These namespaces belong to the assembler, never to product assets.
    if any((website / name).exists() for name in ("docs", "demo", "landing")):
        raise ValueError("product files must not overwrite docs, demo or landing")
    if output.exists():
        shutil.rmtree(output)
    shutil.copytree(website, output)
    shutil.copytree(docs, output / "docs")
    routes = {page.relative_to(docs).as_posix(): page.relative_to(docs).as_posix()
              for page in docs.rglob("*.html")}
    routes.update({old: entry["page"] for old, entry in legacy.items()})
    for old, replacement in sorted(routes.items()):
        relative = Path(old)
        if relative == Path("index.html") or relative.parts[0] == "docs":
            continue
        if relative == Path("404.html"):
            # Pages serves this at arbitrary missing URLs. MkDocs' 404 uses
            # project-absolute links, which work at every requested depth.
            if not (output / "404.html").exists():
                shutil.copyfile(docs / replacement, output / "404.html")
            continue
        destination = output / relative
        if destination.exists():
            raise ValueError("product file conflicts with a legacy docs URL: %s" % relative)
        destination.parent.mkdir(parents=True, exist_ok=True)
        # Each old URL points into docs/ at the same depth, so all page-relative
        # links and runtime assets remain MkDocs' responsibility.
        target = "../" * (len(relative.parts) - 1) + "docs/" + replacement
        if target.endswith("index.html"):
            target = target[:-len("index.html")]
        destination.write_text(redirect_page(target), encoding="utf-8")
    if "index.html" in legacy:
        home = output / "index.html"
        text = home.read_text(encoding="utf-8")
        if "</head>" not in text:
            raise ValueError("the product homepage needs </head> for legacy heading redirects")
        home.write_text(text.replace("</head>", home_redirect(legacy["index.html"]["anchors"])
                                     + "\n</head>", 1), encoding="utf-8")
    # Keep links to the original placeholder working too.
    (output / "landing").mkdir(exist_ok=True)
    (output / "landing" / "index.html").write_text(redirect_page("../"), encoding="utf-8")
    (output / ".nojekyll").touch()
    check_local_links(output)
    if legacy:
        print("Verified %d existing docs pages and %d anchor targets."
              % (len(legacy), sum(len(entry["anchors"]) for entry in legacy.values())))
    print("Pages assembled: product at /, documentation at /docs/, /demo/ redirects to /docs/demo/")


if __name__ == "__main__":
    legacy = json.loads((ROOT / "tools/pages_legacy_urls.json").read_text(encoding="utf-8"))["routes"]
    build(ROOT / "build/docs/site", ROOT / "website", ROOT / "build/pages", legacy)
