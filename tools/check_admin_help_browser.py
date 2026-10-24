"""Check the admin page's help links in a real browser.

This starts a **throwaway** daemon: a temporary configuration file, a
temporary database and a temporary key file, all in a temporary directory,
on ports that nothing else uses. The live dash.toml and dash.db are never
read or written.

    .venv-browser/Scripts/python tools/check_admin_help_browser.py

It checks that every help link points at the right page and anchor (the
href is asserted; the live site is never needed), that the links can be
reached with the keyboard, that the page has no horizontal overflow at
phone width, that the sound switch is off by default and that the new
mute length validates and saves.

The browser checks need Playwright, which is NOT needed for the test suite
and is not a dependency of the project. Install it into a throwaway
environment rather than into .venv, and run this script with that
environment's Python (the throwaway daemon still runs with .venv):

    python -m venv .venv-browser
    .venv-browser/Scripts/python -m pip install playwright
    .venv-browser/Scripts/python -m playwright install chromium
    .venv-browser/Scripts/python tools/check_admin_help_browser.py
"""

import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from urllib.request import urlopen

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ADMIN_JS = os.path.join(REPO_ROOT, "daemon", "src", "presto_ai_dash", "web", "admin.js")
DOCS_BASE = "https://jake1164.github.io/presto-ai-dash/"
ADMIN_TOKEN = "throwaway-admin-token-0001"
SHARED_TOKEN = "throwaway-shared-token-0002"


def free_port():
    """A port nothing is listening on."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def help_map():
    """The help map, read out of admin.js."""
    with open(ADMIN_JS, encoding="utf-8") as handle:
        text = handle.read()
    match = re.search(r"const HELP = \{(.*?)\n  \};", text, re.S)
    if not match:
        raise SystemExit("the HELP map is gone from admin.js")
    entries = re.findall(
        r'"([a-z0-9-]+)":\s*\{\s*page:\s*"([^"]+)",\s*anchor:\s*"([^"]+)"\s*\}',
        match.group(1))
    return {key: (page, anchor) for key, page, anchor in entries}


def expected_url(page, anchor):
    return DOCS_BASE + page.replace(".md", "/") + "#" + anchor


def start_daemon(directory, device_port, admin_port):
    """Start the throwaway daemon and wait for its admin page."""
    config = os.path.join(directory, "throwaway.toml")
    with open(config, "w", encoding="utf-8") as handle:
        handle.write(
            "[daemon]\n"
            "host = \"127.0.0.1\"\n"
            "port = %d\n"
            "shared_token = \"%s\"\n"
            "poll_interval = 15\n"
            "database = \"%s\"\n"
            "\n"
            "[admin]\n"
            "enabled = true\n"
            "port = %d\n"
            "token = \"%s\"\n"
            "key_file = \"%s\"\n"
            % (device_port, SHARED_TOKEN,
               os.path.join(directory, "throwaway.db").replace("\\", "/"),
               admin_port, ADMIN_TOKEN,
               os.path.join(directory, "throwaway.key").replace("\\", "/")))
    environment = dict(os.environ)
    environment["PYTHONPATH"] = os.path.join(REPO_ROOT, "daemon", "src")
    # The daemon's output goes to a file in the temporary directory: a pipe
    # nobody reads would fill up and stop the daemon.
    log_path = os.path.join(directory, "daemon.log")
    python = (os.path.join(REPO_ROOT, ".venv", "Scripts", "python.exe") if os.name == "nt"
              else os.path.join(REPO_ROOT, ".venv", "bin", "python"))
    with open(log_path, "wb") as log_file:
        process = subprocess.Popen(
            [python, "-m", "presto_ai_dash.app", "--config", config, "--admin"],
            cwd=REPO_ROOT, env=environment,
            stdout=log_file, stderr=subprocess.STDOUT)
    url = "http://127.0.0.1:%d/" % admin_port
    for _ in range(80):
        if process.poll() is not None:
            with open(log_path, "rb") as log_file:
                raise SystemExit("the throwaway daemon stopped: %s" %
                                 log_file.read().decode("utf-8", "replace"))
        try:
            with urlopen(url, timeout=1):
                return process, url
        except Exception:
            time.sleep(0.25)
    process.kill()
    raise SystemExit("the throwaway daemon did not answer on %s" % url)


def chromium_path():
    """The Chromium build that is on this machine, if there is one.

    Playwright wants a build of its own; when the machine has another one
    (a different Playwright version installed it), that one is used.
    """
    root = os.path.join(os.environ.get("LOCALAPPDATA", ""), "ms-playwright")
    if not os.path.isdir(root):
        return None
    builds = sorted(name for name in os.listdir(root) if name.startswith("chromium-"))
    for name in reversed(builds):
        for relative in (("chrome-win64", "chrome.exe"),
                         ("chrome-win", "chrome.exe")):
            candidate = os.path.join(root, name, *relative)
            if os.path.isfile(candidate):
                return candidate
    return None


def main():
    from playwright.sync_api import sync_playwright

    known = help_map()
    failures = []
    directory = tempfile.mkdtemp(prefix="presto-admin-check-")
    device_port, admin_port = free_port(), free_port()
    process, url = start_daemon(directory, device_port, admin_port)
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=chromium_path())
            page = browser.new_page(viewport={"width": 1280, "height": 900})
            # The live site is never needed: every request to it is stopped
            # before it leaves the browser.
            page.route("**/*", lambda route, request: (
                route.abort() if request.url.startswith(DOCS_BASE) else route.continue_()))
            page.goto(url)
            page.fill("#admin-token", ADMIN_TOKEN)
            page.click("#unlock")
            page.wait_for_selector("#workspace:not([hidden])", timeout=15000)
            page.wait_for_timeout(500)

            # Every provider uses the same card template. These are disabled
            # accounts in the throwaway daemon: no provider is contacted.
            csrf = page.request.get(url + "api/admin/session").json()["csrf_token"]
            catalog = page.request.get(url + "api/admin/sources").json()
            existing = {source["type"] for source in catalog["sources"]}
            for source_type in catalog["types"]:
                if source_type["id"] in existing:
                    continue
                response = page.request.post(url + "api/admin/sources", headers={
                    "X-CSRF-Token": csrf, "Origin": url.rstrip("/")}, data={
                    "type": source_type["id"], "name": source_type["name"],
                    "enabled": False, "config": {}, "revision": 0})
                if not response.ok:
                    raise RuntimeError("could not create disabled test source: " + response.text())
            # Include representative readings as well as empty accounts, so the
            # template is checked with bars, stale data, activity and notes.
            fixtures = page.request.get(url + "api/admin/sources").json()
            now = datetime.now(timezone.utc)
            for source in fixtures["sources"]:
                labels = {"claude": ["5h", "Weekly"], "codex": ["Weekly"],
                          "opencode_go": ["5h", "Weekly", "Monthly"]}.get(source["type"])
                if labels:
                    source["quota"] = {"windows": [{
                        "label": label, "state": "stale" if source["type"] == "codex" else "available",
                        "used_percent": 14 + index * 11, "observed_at": now.isoformat(),
                        "resets_at": (now + timedelta(hours=3)).isoformat(), "source": "test reading"
                    } for index, label in enumerate(labels)], "note": "Sample quota reading."}
                elif source["type"] == "ollama_cloud":
                    source["quota"] = {"windows": [{"label": "Monthly credits", "state": "available",
                        "used_percent": 100 * 8.48 / 60, "used_amount": 8.48, "limit_amount": 60,
                        "currency": "USD", "resets_at": (now + timedelta(days=14)).isoformat()}],
                        "cloud_balance": {"included_remaining_usd": 51.52,
                                          "purchased_remaining_usd": 0, "state": "available"},
                        "activity": {"state": "available", "range": "7d", "request_count": 297,
                                     "usage_usd": 8.480570, "input_tokens": 22000000, "output_tokens": 231608,
                                     "from": (now - timedelta(days=7)).isoformat(), "until": now.isoformat()}}
            page.route("**/api/admin/sources", lambda route: (
                route.fulfill(json=fixtures) if route.request.method == "GET" else route.continue_()))
            page.reload()
            page.wait_for_selector("#workspace:not([hidden])")
            page.wait_for_function("count => document.querySelectorAll('.source').length === count",
                                   arg=len(catalog["types"]))
            for toggle in page.locator(".card-toggle").all():
                toggle.click()
            # Alert settings load asynchronously and redraw the account list.
            page.wait_for_function("() => [...document.querySelectorAll('.card-detail')].every("
                                   "card => card.querySelector('.service-alerts input'))")
            layouts = page.locator(".card-detail").evaluate_all("""cards => cards.map(card => ({
                headings: [...card.querySelectorAll('h4')].map(h => h.textContent),
                outsideLinks: [...card.querySelectorAll('a')].filter(a => !a.closest('.card-links')).length,
                guideCount: card.querySelectorAll('.card-links a.help-link').length,
                footerLast: card.lastElementChild.matches('.card-links')
            }))""")
            for layout in layouts:
                if layout["headings"] != ["Readings", "Setup", "Alerts"]:
                    failures.append("provider card has inconsistent sections: " + str(layout))
                if layout["outsideLinks"] or layout["guideCount"] < 2 or not layout["footerLast"]:
                    failures.append("provider card has links outside its shared footer: " + str(layout))
            if page.locator(".card-readings progress").count() != 7:
                failures.append("quota bars were lost when moving readings into the template")
            cloud = page.locator(".source").filter(has=page.get_by_role("button", name="Show the details of Ollama Cloud", exact=True))
            cloud_text = cloud.inner_text()
            if not all(value in cloud_text for value in ("14.1%", "$51.52", "$8.48", "rolling 7d", "22.23M")):
                failures.append("cloud balance or readable rolling activity was lost: " + cloud_text)
            print("shared template verified for %d provider cards" % len(layouts))
            shots = os.path.join(REPO_ROOT, ".preview", "admin-cards")
            os.makedirs(shots, exist_ok=True)
            for index, card in enumerate(page.locator(".source").all()):
                card.screenshot(path=os.path.join(shots, "card-%d.png" % index))
            page.set_viewport_size({"width": 390, "height": 800})
            for index, card in enumerate(page.locator(".source").all()):
                card.screenshot(path=os.path.join(shots, "phone-card-%d.png" % index))
            if page.evaluate("document.documentElement.scrollWidth > innerWidth"):
                failures.append("expanded provider cards overflow the phone viewport")
            page.set_viewport_size({"width": 1280, "height": 900})
            page.locator(".card-links a[href='#settings']").first.click()
            if page.locator("#remote-setting").get_attribute("open") is None:
                failures.append("the footer remote-report link did not open its settings")
            page.click("#tab-services")

            # 1. Every help link points at the right page and anchor.
            links = page.eval_on_selector_all(
                "a.help-link[target='_blank']",
                "els => els.map(e => ({href: e.getAttribute('href'),"
                " target: e.getAttribute('target'), rel: e.getAttribute('rel'),"
                " referrer: e.getAttribute('referrerpolicy'),"
                " label: e.getAttribute('aria-label'), text: e.textContent}))")
            print("help links found: %d" % len(links))
            if not links:
                failures.append("the page has no help links")
            wanted = {expected_url(page_, anchor) for page_, anchor in known.values()}
            for link in links:
                if link["href"] not in wanted:
                    failures.append("a help link points at %s" % link["href"])
                if link["target"] != "_blank":
                    failures.append("a help link does not open in a new tab: %s" % link["href"])
                rel = (link["rel"] or "").split()
                if "noopener" not in rel or "noreferrer" not in rel:
                    failures.append("a help link has no noopener noreferrer: %s" % link["href"])
                if link["referrer"] != "no-referrer":
                    failures.append("a help link sends a referrer: %s" % link["href"])
                if not (link["label"] or "").startswith("Help: "):
                    failures.append("a help link has no aria-label: %s" % link["href"])
                if not link["text"] or link["text"] == "?":
                    failures.append("a help link needs descriptive text: %s" % link["href"])
                if "?" in (link["href"] or "").split("#")[0]:
                    failures.append("a help link has a query string: %s" % link["href"])

            # 2. The keyboard reaches a link, and Enter opens it.
            # Help follows the tab; Escape and outside clicks dismiss it.
            for tab in ("services", "status", "settings"):
                page.click("#tab-" + tab)
                page.click("#header-help > summary")
                current = page.locator("#current-tab-help a")
                if current.inner_text() != tab.title() + " help":
                    failures.append("help does not follow the " + tab + " tab")
                if not current.get_attribute("href").endswith("#" + tab):
                    failures.append("help points at the wrong tab")
                page.keyboard.press("Escape")
                if page.locator("#header-help").get_attribute("open") is not None:
                    failures.append("Escape did not close Help")
                if not page.locator("#header-help > summary").evaluate("el => el === document.activeElement"):
                    failures.append("Escape did not return focus to Help")
            page.click("#header-help > summary")
            page.click("h1")
            if page.locator("#header-help").get_attribute("open") is not None:
                failures.append("clicking outside did not close Help")
            page.click("#tab-services")
            if page.locator("#panel-services .source-head .help-link").count():
                failures.append("collapsed account rows still have help links")
            page.click("#header-help > summary")
            page.focus("#current-tab-help a")
            page.keyboard.press("Tab")
            focused = page.evaluate("document.activeElement.className")
            if "help-link" not in (focused or ""):
                failures.append("Tab does not move between the help links")
            with page.expect_popup(timeout=10000) as popup_info:
                page.keyboard.press("Enter")
            popup = popup_info.value
            opened = popup.url
            popup.close()
            if opened not in wanted:
                failures.append("Enter opened %s, which is not a documentation page" % opened)

            # 3. Phone width: no horizontal overflow, and the links can be tapped.
            page.set_viewport_size({"width": 390, "height": 800})
            page.locator("#header-help").evaluate("el => { el.open = true; }")
            page.wait_for_timeout(300)
            overflow = page.evaluate(
                "() => ({scroll: document.documentElement.scrollWidth,"
                " inner: window.innerWidth})")
            print("phone width: scroll %s, inner %s" % (overflow["scroll"], overflow["inner"]))
            if overflow["scroll"] > overflow["inner"] + 1:
                failures.append("the page scrolls sideways at 390 pixels")
            sizes = page.eval_on_selector_all(
                "a.help-link",
                "els => els.filter(e => e.offsetParent !== null).map(e => {"
                " const r = e.getBoundingClientRect();"
                " return {w: r.width, h: r.height}; })")
            too_small = [size for size in sizes if size["h"] < 24 or size["w"] < 24]
            if too_small:
                failures.append("a help link is smaller than 24 pixels: %s" % too_small)
            print("the visible help links: %d, the smallest is %s pixels tall" %
                  (len(sizes), min((size["h"] for size in sizes), default=0)))

            # 4. The sound is off by default, and the mute length saves.
            page.set_viewport_size({"width": 1280, "height": 900})
            page.click("#tab-settings")
            page.wait_for_timeout(500)
            # The Alerts section is folded away by default.
            page.eval_on_selector("#alerts-setting", "el => { el.open = true; }")
            page.wait_for_timeout(200)
            if page.is_checked("#alert-sound"):
                failures.append("the sound switch is on by default")
            mute = page.input_value("#alert-mute-hours")
            print("the mute length shows: %s" % mute)
            if mute != "4":
                failures.append("the mute length does not show the default of 4 hours")
            page.fill("#alert-mute-hours", "99")
            page.dispatch_event("#alert-mute-hours", "change")
            page.wait_for_timeout(800)
            text = page.inner_text("#message")
            print("the refusal says: %s" % text)
            if "between 0 and 48" not in text:
                failures.append("an unusable mute length is not refused in words")
            page.fill("#alert-mute-hours", "8")
            page.dispatch_event("#alert-mute-hours", "change")
            page.wait_for_timeout(1200)
            if page.input_value("#alert-mute-hours") != "8":
                failures.append("the mute length was not saved")
            print("the mute length after saving: %s" % page.input_value("#alert-mute-hours"))
            browser.close()
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except Exception:
            process.kill()
        shutil.rmtree(directory, ignore_errors=True)

    if failures:
        print("")
        for line in failures:
            print("FAILED: " + line)
        return 1
    print("")
    print("every help link is correct, the keyboard reaches them, the page fits a phone,")
    print("the sound is off by default and the mute length validates and saves.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
