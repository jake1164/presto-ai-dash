"""Check that every admin help link's anchor exists in the built site.

The map in ``daemon/src/presto_ai_dash/web/admin.js`` is the only place a
documentation URL is built. This script reads it and looks for the anchor in
the HTML that mkdocs built, so a link that points at a heading that was
renamed is caught before it reaches the page.

    .venv-docs/Scripts/mkdocs build --strict
    .venv/Scripts/python tools/check_doc_anchors.py

It reads ``build/docs/site``, so build the site first. It exits non-zero
when an anchor is missing.
"""

import os
import re
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ADMIN_JS = os.path.join(REPO_ROOT, "daemon", "src", "presto_ai_dash", "web", "admin.js")
SITE_DIR = os.path.join(REPO_ROOT, "build", "docs", "site")


def help_entries(text):
    """The help map: (key, page, anchor) for every entry."""
    match = re.search(r"const HELP = \{(.*?)\n  \};", text, re.S)
    if not match:
        raise SystemExit("the HELP map is gone from admin.js")
    return re.findall(r'"([a-z0-9-]+)":\s*\{\s*page:\s*"([^"]+)",\s*anchor:\s*"([^"]+)"\s*\}',
                      match.group(1))


def built_page(page):
    """The built HTML file of a docs page."""
    path = os.path.join(SITE_DIR, page.replace(".md", ""), "index.html")
    return path


def main():
    if not os.path.isdir(SITE_DIR):
        raise SystemExit("the site is not built; run mkdocs build --strict first")
    with open(ADMIN_JS, encoding="utf-8") as handle:
        entries = help_entries(handle.read())
    if not entries:
        raise SystemExit("the HELP map is empty")
    missing = []
    for key, page, anchor in entries:
        path = built_page(page)
        if not os.path.isfile(path):
            missing.append("%s: the page %s was not built" % (key, page))
            continue
        with open(path, encoding="utf-8") as handle:
            html = handle.read()
        if ('id="%s"' % anchor) not in html:
            missing.append("%s: the anchor #%s is not in %s" % (key, anchor, page))
    for key, page, anchor in entries:
        print("%-22s %s#%s" % (key, page, anchor))
    if missing:
        print("")
        for line in missing:
            print("MISSING: " + line)
        return 1
    print("")
    print("%d help links checked, every anchor is in the built site." % len(entries))
    return 0


if __name__ == "__main__":
    sys.exit(main())
