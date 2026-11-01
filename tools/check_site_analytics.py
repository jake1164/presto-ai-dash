"""Check that the public site measures what it says it measures, and only after consent.

This drives a real browser against the assembled site, because consent and
navigation are behaviour, not markup: a page can carry a banner and still
start Google Analytics, and a click handler can fire twice without any test of
the source noticing. It serves build/pages, so run it after
`python tools/build_pages_site.py`.

    .venv-browser/bin/python tools/check_site_analytics.py
    .venv-browser/Scripts/python tools/check_site_analytics.py   (Windows)

Two things are watched. The requests the page makes are counted, so a page that
reaches Google before the reader answers is caught. The commands the page hands
to Google Analytics are read from a stand-in for gtag.js, so what would be
reported is exact and repeatable -- and nothing at all reaches the property.

It fails when:

* a first-time visitor causes any request to Google, or gets a GA cookie;
* accepting consent starts analytics twice, or reports a page view twice;
* the same choice is not honoured on the other surface;
* rejecting, or revoking an earlier acceptance, still lets analytics run;
* a click reports an event twice, or reports one it should not;
* the demo reports anything without consent, or reports animation rather
  than a reader's action;
* an event carries anything beyond the documented parameters.
"""

import functools
import http.server
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SITE = ROOT / "build" / "pages"

# Every custom event this site may report, with the parameters it may carry.
# Kept in step with docs/analytics.md by daemon/tests/tooling/test_site_analytics.py.
EVENTS = {
    "install_click": {"install_method", "link_path", "link_location"},
    "docs_link_click": {"link_path", "link_location"},
    "outbound_click": {"destination", "link_domain", "link_path", "link_location"},
    "demo_start": set(),
    "demo_ready": set(),
    "demo_error": {"error_kind"},
    "demo_interaction": {"interaction"},
    "demo_scene": {"scene"},
    "demo_restart": set(),
}

# Stands in for gtag.js. It records every command the page queues, which is
# exactly what the real library would forward to Google.
STUB = """
window.__hits = [];
(function () {
  function record(entry) {
    try {
      var parts = Array.prototype.slice.call(entry).map(function (value) {
        return (value && typeof value === 'object')
          ? JSON.parse(JSON.stringify(value)) : String(value);
      });
      window.__hits.push(parts);
    } catch (error) { /* not a command */ }
  }
  var queue = window.dataLayer = window.dataLayer || [];
  for (var i = 0; i < queue.length; i++) record(queue[i]);
  queue.length = 0;
  queue.push = function () {
    for (var i = 0; i < arguments.length; i++) record(arguments[i]);
    return 0;
  };
}());
"""

failures = []


def check(condition, message):
    if not condition:
        failures.append(message)
        print("  FAIL  " + message)
    return condition


def hits(page):
    return page.evaluate("() => window.__hits || []")


def views(page):
    return sum(1 for hit in hits(page) if hit and hit[0] == "config")


def events(page):
    found = []
    for hit in hits(page):
        if hit and hit[0] == "event":
            params = hit[2] if len(hit) > 2 and isinstance(hit[2], dict) else {}
            found.append({"name": hit[1], "params": params})
    return found


def reset(page):
    page.evaluate("() => { if (window.__hits) window.__hits.length = 0; }")


def new_page(browser, consent=None):
    context = browser.new_context()
    # gtag.js never runs: the stand-in answers instead, so a run reports
    # nothing to the property while still showing exactly what would be sent.
    context.route("**/gtag/js*",
                  lambda route: route.fulfill(status=200, body=STUB,
                                              content_type="application/javascript"))
    if consent is not None:
        context.add_init_script(
            "try{localStorage.setItem('/docs/.__consent',%r)}catch(e){}" % consent)
    page = context.new_page()
    page.google = []
    page.on("request", lambda request: page.google.append(request.url)
            if "google" in request.url else None)
    return context, page


def stored(page):
    return page.evaluate("() => localStorage.getItem('/docs/.__consent')")


def shown(page, selector):
    return page.evaluate(
        "(sel) => { var box = document.querySelector(sel); return box ? !box.hidden : null; }",
        selector)


def first_visit(browser, base):
    print("a first-time visitor, before answering")
    context, page = new_page(browser)
    page.goto(base + "/", wait_until="networkidle")
    page.wait_for_timeout(1000)
    check(page.google == [], "the product page asked Google for %r" % (page.google,))
    check(hits(page) == [], "the product page queued %r before consent" % (hits(page),))
    names = [cookie["name"] for cookie in context.cookies()]
    check(names == [], "cookies were set before consent: %r" % (names,))
    check(shown(page, "[data-landing-consent]") is True,
          "the product page did not offer the cookie choice")
    context.close()


def accept(browser, base):
    print("accepting on the product page")
    context, page = new_page(browser)
    page.goto(base + "/", wait_until="networkidle")
    page.click("[data-consent-accept]")
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(1200)
    check(stored(page) == '{"analytics":true}',
          "accepting stored %r, not the shape Material reads" % (stored(page),))
    check(shown(page, "[data-landing-consent]") is False,
          "the banner stayed up after the reader answered")
    check(views(page) == 1, "the product page reported %d page views, not 1" % views(page))
    check(page.evaluate("() => typeof window.gtag === 'function'"),
          "accepting did not start analytics")

    # The same visitor, now on the documentation: one consent, no second prompt,
    # and page views that are neither lost nor doubled.
    page.goto(base + "/docs/", wait_until="networkidle")
    page.wait_for_timeout(1200)
    check(shown(page, "[data-md-component=consent]") is False,
          "the documentation asked again after the product page was answered")
    check(views(page) == 1,
          "a documentation page reported %d page views, not 1" % views(page))
    surface = page.evaluate("() => window.__md_scope && window.__md_scope.pathname")
    check(surface == "/docs/",
          "the documentation scopes its choice to %r, not the path the product "
          "page reads and writes" % (surface,))
    context.close()


def reject(browser, base):
    print("rejecting on the product page")
    context, page = new_page(browser)
    page.goto(base + "/", wait_until="networkidle")
    page.click("[data-consent-reject]")
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(1000)
    check(stored(page) == "{}",
          "rejecting stored %r, not Material's empty answer" % (stored(page),))
    check(page.google == [], "rejecting still asked Google for %r" % (page.google,))
    check(hits(page) == [], "rejecting still queued %r" % (hits(page),))
    names = [cookie["name"] for cookie in context.cookies()]
    check(names == [], "rejecting set cookies: %r" % (names,))

    page.goto(base + "/docs/", wait_until="networkidle")
    page.wait_for_timeout(1000)
    check(page.google == [], "the documentation ignored the rejection and asked "
          "Google for %r" % (page.google,))
    check(hits(page) == [], "the documentation ignored the rejection and queued %r"
          % (hits(page),))
    context.close()


def revoke(browser, base):
    print("changing an earlier answer")
    context, page = new_page(browser)
    page.goto(base + "/", wait_until="networkidle")
    page.click("[data-consent-accept]")
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(1000)
    check(views(page) == 1, "accepting did not start analytics")
    page.click("[data-consent-open]")
    page.wait_for_timeout(200)
    check(shown(page, "[data-landing-consent]") is True,
          "the footer link did not reopen the choice")
    page.click("[data-consent-reject]")
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(1000)
    check(stored(page) == "{}", "revoking stored %r" % (stored(page),))
    page.google.clear()
    page.reload(wait_until="networkidle")
    page.wait_for_timeout(1000)
    check(page.google == [], "revoking did not stop analytics: %r" % (page.google,))
    check(hits(page) == [], "revoking still queued %r" % (hits(page),))
    context.close()


def clicks(browser, base):
    print("clicks report one named event each")
    context, page = new_page(browser, consent='{"analytics":true}')
    page.goto(base + "/", wait_until="networkidle")
    page.wait_for_timeout(1000)

    for label, selector, expected in (
        ("the setup guide", 'a[href="docs/getting-started/install-the-daemon/"]', "install_click"),
        ("the demo", 'a[href="docs/demo/"]', "docs_link_click"),
        ("the Pimoroni shop", 'a[href*="shop.pimoroni.com"]', "outbound_click"),
        ("the repository", 'header a[href="https://github.com/jake1164/presto-ai-dash"]',
         "outbound_click"),
    ):
        reset(page)
        # Follow the link's own handler but not the navigation, so one click can
        # be told apart from the page it opens. Our listener runs first, in the
        # capture phase; this one then cancels the default.
        page.evaluate("""(sel) => {
            var anchor = document.querySelector(sel);
            if (!anchor) throw new Error("no link matching " + sel);
            anchor.addEventListener("click", function (event) { event.preventDefault(); },
                                    { once: true });
            anchor.click();
        }""", selector)
        page.wait_for_timeout(300)
        found = events(page)
        check([hit["name"] for hit in found] == [expected],
              "clicking %s reported %r, not one %s"
              % (label, [hit["name"] for hit in found], expected))
        if found:
            extra = set(found[0]["params"]) - EVENTS[expected]
            check(not extra, "%s carried %r, which is not documented"
                  % (expected, sorted(extra)))
    context.close()


def demo(browser, base):
    print("the demo reports actions, not frames")
    context, page = new_page(browser, consent='{"analytics":true}')
    page.goto(base + "/docs/demo/", wait_until="networkidle")
    page.wait_for_timeout(800)
    page.click("#presto-start")
    page.wait_for_function("() => window.prestoDemo && window.prestoDemo.running()",
                           timeout=90000)
    page.wait_for_timeout(1000)
    names = [hit["name"] for hit in events(page)]
    check("demo_start" in names, "pressing Start did not report demo_start (%r)" % (names,))
    check("demo_ready" in names, "the demo running did not report demo_ready (%r)" % (names,))

    # A tap, then several more: "did they use it" once, not once per touch, and
    # nothing at all from the animation loop that runs every 20 ms.
    reset(page)
    for _ in range(4):
        page.click("#presto-canvas", position={"x": 120, "y": 130})
        page.wait_for_timeout(250)
    page.wait_for_timeout(800)
    found = events(page)
    interactions = [hit for hit in found if hit["name"] == "demo_interaction"]
    check(len(interactions) == 1,
          "four taps reported %d demo_interaction events, not 1" % len(interactions))
    check(len(found) == len(interactions),
          "idle frames reported %r" % ([hit["name"] for hit in found
                                        if hit["name"] != "demo_interaction"],))

    reset(page)
    page.select_option("#presto-scene", index=1)
    page.wait_for_timeout(500)
    scenes = [hit for hit in events(page) if hit["name"] == "demo_scene"]
    check(len(scenes) == 1, "choosing a scene reported %d demo_scene events" % len(scenes))
    if scenes:
        check(set(scenes[0]["params"]) == EVENTS["demo_scene"],
              "demo_scene carried %r" % (scenes[0]["params"],))

    reset(page)
    page.click("#presto-restart")
    page.wait_for_timeout(500)
    names = [hit["name"] for hit in events(page)]
    check(names.count("demo_restart") == 1, "restarting reported %r" % (names,))
    context.close()


def demo_without_consent(browser, base):
    print("the demo says nothing without consent")
    context, page = new_page(browser, consent="{}")
    page.goto(base + "/docs/demo/", wait_until="networkidle")
    page.wait_for_timeout(800)
    page.click("#presto-start")
    page.wait_for_function("() => window.prestoDemo && window.prestoDemo.running()",
                           timeout=90000)
    page.click("#presto-canvas", position={"x": 120, "y": 130})
    page.wait_for_timeout(800)
    check(page.google == [], "the demo asked Google for %r without consent" % (page.google,))
    check(hits(page) == [], "the demo queued %r without consent" % (hits(page),))
    context.close()


def main():
    if not SITE.is_dir():
        print("%s is missing: run python tools/build_pages_site.py first" % SITE)
        return 2
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print(__doc__.strip().splitlines()[0])
        print("This needs the browser environment: .venv-browser/bin/python.")
        return 2

    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(SITE))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = "http://127.0.0.1:%d" % server.server_address[1]
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                for scenario in (first_visit, accept, reject, revoke, clicks, demo,
                                 demo_without_consent):
                    scenario(browser, base)
            finally:
                browser.close()
    finally:
        server.shutdown()

    if failures:
        print("\n%d problem(s) with the site's analytics." % len(failures))
        return 1
    print("\nConsent, clicks and demo events all behave (checked in a real browser).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
