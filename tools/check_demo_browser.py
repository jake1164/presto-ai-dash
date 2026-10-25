"""Exercise demo controls in Chromium without reading live configuration.

Run with .venv-browser/Scripts/python tools/check_demo_browser.py. Uses the
existing Playwright environment, a throwaway daemon, and the real preview.
Screenshots go to the gitignored .preview/demo-browser directory.
"""

import functools
import http.server
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from urllib.request import urlopen

from check_admin_help_browser import (ADMIN_TOKEN, REPO_ROOT, chromium_path,
                                      free_port, start_daemon)


def main():
    from playwright.sync_api import expect, sync_playwright

    root = Path(REPO_ROOT)
    python = root / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    screenshots = root / ".preview" / "demo-browser"
    screenshots.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="presto-demo-browser-") as directory:
        scratch = Path(directory)
        site = scratch / "site"
        demo = site / "demo"
        demo.mkdir(parents=True)
        # The build's renderer imports the app: give it public defaults rather
        # than allowing the normal device config import to read a private file.
        subprocess.run([str(python), "-c",
            "import sys,types;sys.modules['config']=types.SimpleNamespace();"
            "sys.path.insert(0,'tools');import build_demo_site;"
            "build_demo_site.build(sys.argv[1])", str(demo / "app")],
            cwd=root, check=True)
        source = root / "docs" / "user" / "demo"
        for name in ("driver.py", "demo.js", "demo.css"):
            shutil.copyfile(source / name, demo / name)
        shutil.copytree(source / "vendor", demo / "vendor")
        markup = (source / "index.md").read_text(encoding="utf-8")
        body = markup[markup.index('<div class="presto-demo"'):markup.index("## What you are looking at")]
        (demo / "index.html").write_text(
            '<!doctype html><html><head><title>Presto demo check</title>'
            '<link rel="stylesheet" href="demo.css"></head><body>' + body +
            '<script src="demo.js"></script></body></html>', encoding="utf-8")
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0),
            functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(site)))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        processes = []
        try:
            daemon, admin_url = start_daemon(directory, free_port(), free_port())
            processes.append(daemon)
            preview_port = free_port()
            preview_code = """
import sys, types
sys.path.insert(0, 'tools/presto_preview')
import live
original = live.render._load_app_modules
def load(*args, **kwargs):
    sys.modules['config'] = types.SimpleNamespace()
    return original(*args, **kwargs)
live.render._load_app_modules = load
live.main_cli(['--demo', '--port', sys.argv[1]])
"""
            with open(scratch / "preview.log", "wb") as log:
                preview = subprocess.Popen([str(python), "-c", preview_code, str(preview_port)],
                    cwd=root, stdout=log, stderr=subprocess.STDOUT)
            processes.append(preview)
            preview_url = "http://127.0.0.1:%d/" % preview_port
            deadline = time.monotonic() + 15
            while True:
                try:
                    with urlopen(preview_url, timeout=1):
                        break
                except OSError:
                    if time.monotonic() > deadline:
                        raise RuntimeError("isolated preview did not start")
                    time.sleep(0.1)

            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True, executable_path=chromium_path())
                context = browser.new_context(viewport={"width": 1100, "height": 1000})
                page = context.new_page()
                page.goto(admin_url)
                page.fill("#admin-token", ADMIN_TOKEN)
                page.click("#unlock")
                expect(page.locator("#workspace")).to_be_visible()
                second = context.new_page()
                second.goto(admin_url)
                expect(second.locator("#workspace")).to_be_visible()
                for tab in (page, second):
                    tab.click("#tab-settings")
                    tab.click("#demo-setting > summary")
                page.check("#demo-enabled")
                expect(page.locator("#demo-banner")).to_be_visible()
                expect(second.locator("#demo-banner")).to_be_visible(timeout=15000)
                expect(second.locator("#demo-enabled")).to_be_checked()
                second.click("#demo-banner-off")
                expect(page.locator("#demo-banner")).to_be_hidden(timeout=15000)
                expect(page.locator("#demo-enabled")).not_to_be_checked()
                page.screenshot(path=str(screenshots / "admin.png"))

                # An old background response must not undo a newer checkbox write.
                held = []
                page.route("**/api/admin/demo", lambda route:
                    held.append(route) if route.request.method == "GET" else route.continue_())
                page.bring_to_front()
                page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
                deadline = time.monotonic() + 12
                while not held and time.monotonic() < deadline:
                    page.wait_for_timeout(50)
                assert held, "no background demo read was issued"
                page.check("#demo-enabled")
                expect(page.locator("#demo-banner")).to_be_visible()
                for route in held:
                    route.fulfill(json={"enabled": False, "locked_by_environment": False})
                page.unroute("**/api/admin/demo")
                page.wait_for_timeout(200)
                expect(page.locator("#demo-enabled")).to_be_checked()
                expect(page.locator("#demo-banner")).to_be_visible()
                print("admin tabs synchronize and an old read cannot undo a write")

                demo_page = context.new_page()
                errors = []
                demo_page.on("pageerror", lambda error: errors.append(str(error)))
                demo_page.goto("http://127.0.0.1:%d/demo/" % server.server_port)
                demo_page.click("#presto-start")
                expect(demo_page.locator("#presto-controls")).to_be_visible(timeout=20000)
                demo_page.wait_for_function("window.prestoDemo && prestoDemo.frames() > 0")
                # Jump straight to a critical sample, then tap its real bell.
                demo_page.select_option("#presto-scene", "51")
                demo_page.wait_for_function("prestoDemo.homeStatus() === 'critical'")
                demo_page.locator("#presto-canvas").press("ArrowLeft")
                demo_page.wait_for_function("prestoDemo.card() === 'demo-opencode'")
                demo_page.locator("#presto-canvas").press("ArrowLeft")
                demo_page.wait_for_function("prestoDemo.card() === 'demo-codex'")
                demo_page.locator("#presto-canvas").press("ArrowLeft")
                demo_page.wait_for_function("prestoDemo.card() === 'demo-claude'")
                demo_page.locator("#presto-canvas").screenshot(path=str(screenshots / "browser-critical.png"))
                canvas = demo_page.locator("#presto-canvas")
                box = canvas.bounding_box()
                canvas.click(position={"x": box["width"] * 226 / 240,
                                       "y": box["height"] * 14 / 240})
                demo_page.wait_for_function("prestoDemo.homeStatus() === 'ok'", timeout=5000)
                canvas.screenshot(path=str(screenshots / "browser-alerts-off.png"))
                for card_id in ("demo-codex", "demo-opencode", "dash"):
                    canvas.press("ArrowRight")
                    demo_page.wait_for_function("prestoDemo.card() === " + repr(card_id))
                canvas.screenshot(path=str(screenshots / "browser-home-alerts-off.png"))
                led = demo_page.locator("#presto-demo").evaluate(
                    "el => el.style.getPropertyValue('--presto-led')")
                channels = [int(value) for value in led.removeprefix("rgb(").removesuffix(")").split(",")]
                assert channels[1] > channels[0], "healthy Home must have a green LED"
                demo_page.check("#presto-stale")
                demo_page.wait_for_function("prestoDemo.stale()")
                demo_page.uncheck("#presto-stale")
                demo_page.wait_for_function("!prestoDemo.stale()")
                expect(demo_page.locator("#presto-error")).to_be_hidden()
                assert not errors, errors
                print("browser WebAssembly loads, navigates, applies its bell, and recovers from loss")

                psl = context.new_page()
                psl.goto(preview_url)
                psl.wait_for_function("window.presto && document.querySelector('#scene').options.length > 0")
                psl.select_option("#scene", "critical")
                psl.wait_for_timeout(1500)
                state = psl.evaluate("presto.state()")
                assert state["error"] is None, state["error"]
                psl.locator("#screen").screenshot(path=str(screenshots / "preview-critical.png"))
                psl.evaluate("presto.gesture('left')")
                psl.wait_for_timeout(300)
                assert psl.evaluate("presto.state()")["error"] is None
                print("real Presto preview renders and responds to a swipe")
                browser.close()
        finally:
            for process in reversed(processes):
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
    print("demo browser checks passed; screenshots: " + str(screenshots))
    return 0


if __name__ == "__main__":
    sys.exit(main())
