"""The documentation site's clickable demo: its page, its script and its checks.

The page runs the device's own app in MicroPython WebAssembly. What can be
checked without a browser is checked here: the page and the script load nothing
from another host and touch nothing outside the demo, the driver holds no app
logic, the workflows build and check the demo, and the Node smoke test really
fails when the pixels or the files are wrong. What needs a browser (clicking,
swiping, the timers) was looked at by hand and is listed in the release notes
of the change that added the page.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

import presto_path

ROOT = presto_path.REPO_ROOT
DEMO = os.path.join(ROOT, "docs", "user", "demo")
BUILD = os.path.join(ROOT, "tools", "build_demo_site.py")
SMOKE = os.path.join(ROOT, "tools", "check_demo_wasm.mjs")
NODE = shutil.which("node")


def read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as handle:
        return handle.read()


class PageTests(unittest.TestCase):
    def setUp(self):
        self.page = read("docs", "user", "demo", "index.md")

    def test_the_page_has_the_screen_the_poster_and_the_controls(self):
        for marker in ('id="presto-demo"', 'id="presto-canvas"', 'id="presto-start"',
                       'id="presto-poster"', 'id="presto-pause"', 'id="presto-restart"',
                       'id="presto-scene"', 'id="presto-stale"', 'id="presto-error"'):
            self.assertEqual(self.page.count(marker), 1, marker)

    def test_the_start_button_and_the_screen_are_hidden_until_the_script_runs(self):
        """Without JavaScript the reader sees the poster and nothing that cannot work."""
        for tag in ("presto-start", "presto-canvas", "presto-controls"):
            element = re.search(r'<[a-z]+[^>]*id="%s"[^>]*>' % tag, self.page).group(0)
            self.assertIn(" hidden", element, tag)
        poster = re.search(r'<img[^>]*id="presto-poster"[^>]*>', self.page).group(0)
        self.assertNotIn("hidden", poster)
        self.assertIn('alt="', poster)

    def test_every_address_on_the_page_is_relative(self):
        """The site is served below /presto-ai-dash/, so nothing may start with a slash."""
        for target in re.findall(r'(?:src|href)="([^"]*)"', self.page):
            self.assertFalse(target.startswith(("/", "http:", "https:", "//")), target)
        for target in re.findall(r"\]\(([^)]*)\)", self.page):
            self.assertFalse(target.startswith(("/", "http:", "https:", "//")), target)

    def test_the_page_says_it_is_sample_data(self):
        self.assertIn("Sample data, not a live account", self.page)

    def test_the_page_is_in_the_navigation_and_loads_its_two_assets(self):
        config = read("mkdocs.yml")
        self.assertIn("- See It Running: demo/index.md", config)
        # Site-wide branding or consent assets can precede the demo files.
        # Verify membership in each YAML list, without imposing list order.
        for section, asset in (("extra_css", "demo/demo.css"),
                               ("extra_javascript", "demo/demo.js")):
            block = re.search(r"(?m)^" + section + r":\n((?:[ \t]+-[^\n]*\n)+)", config)
            self.assertIsNotNone(block, section)
            self.assertIn(asset, [line.strip()[2:] for line in block[1].splitlines()])


class ScriptTests(unittest.TestCase):
    def setUp(self):
        self.script = read("docs", "user", "demo", "demo.js")

    def test_it_names_no_other_host(self):
        self.assertNotRegex(self.script, r"https?://")
        self.assertNotRegex(self.script, r"""["'`]//[A-Za-z]""")

    def test_it_does_nothing_on_a_page_without_the_demo(self):
        """It loads on every page of the site, so it must leave early."""
        self.assertIn("if (!root || !script) return;", self.script)
        before_exit = self.script.split("if (!root || !script) return;")[0]
        self.assertNotIn("addEventListener", before_exit)
        self.assertNotIn("fetch(", before_exit)
        self.assertNotIn("import(", before_exit)

    def test_it_loads_nothing_until_the_reader_presses_start(self):
        outside_start = self.script.split("function start()")[0]
        self.assertNotIn("import(", outside_start)
        self.assertNotIn("fetch(", outside_start.split("function get(")[0])
        self.assertIn('startButton.addEventListener("click", start);', self.script)

    def test_it_loads_only_files_the_build_puts_next_to_it(self):
        for name in ("vendor/micropython.js", "app/demo.json", "driver.py", '"app/lib/"'):
            self.assertIn(name, self.script)
        self.assertTrue(os.path.isfile(os.path.join(DEMO, "driver.py")))
        self.assertTrue(os.path.isfile(os.path.join(DEMO, "vendor", "micropython.js")))
        self.assertTrue(os.path.isfile(os.path.join(DEMO, "vendor", "micropython.wasm")))

    def test_it_keeps_nothing_in_the_reader_s_browser(self):
        for name in ("localStorage", "sessionStorage", "document.cookie", "indexedDB",
                     "navigator.sendBeacon", "XMLHttpRequest", "eval("):
            self.assertNotIn(name, self.script)

    def test_it_idles_when_the_screen_is_not_in_view(self):
        self.assertIn("IntersectionObserver", self.script)
        self.assertIn("!visible", self.script)


class StyleTests(unittest.TestCase):
    def test_every_rule_is_scoped_to_the_demo(self):
        """The stylesheet loads on every page; it must restyle nothing else."""
        css = re.sub(r"/\*.*?\*/", "", read("docs", "user", "demo", "demo.css"), flags=re.S)
        for selectors in re.findall(r"([^{}@]+)\{", css):
            for selector in selectors.split(","):
                selector = selector.strip()
                if not selector or selector.startswith("media ") or                         re.fullmatch(r"\d+%|from|to", selector):
                    continue
                self.assertTrue(
                    selector.startswith((".presto-demo", "body:has(.presto-demo)")),
                    "unscoped selector: " + selector)

    def test_it_names_no_other_host(self):
        self.assertNotRegex(read("docs", "user", "demo", "demo.css"), r"https?://|url\(")


class DriverTests(unittest.TestCase):
    """The driver is the stand-in for the hardware, not a second copy of the app."""

    ALLOWED_IMPORTS = {"sys", "json", "time", "asyncio", "js", "net", "ai_dash_app",
                       "framebuffer"}

    def setUp(self):
        self.source = read("docs", "user", "demo", "driver.py")

    def test_it_compiles(self):
        compile(self.source, "driver.py", "exec")

    def test_it_imports_nothing_but_the_runtime_and_the_apps_entry_points(self):
        imported = set()
        for plain, named in re.findall(
                r"^(?:import\s+([A-Za-z_]\w*)|from\s+([A-Za-z_]\w*)\s+import)",
                self.source, flags=re.M):
            imported.add(plain or named)
        self.assertLessEqual(imported, self.ALLOWED_IMPORTS)

    def test_the_loop_pass_is_the_apps_own(self):
        """The app's _tick runs every pass; the driver adds no gesture or screen code."""
        self.assertIn("app._tick()", self.source)
        for name in ("_handle_gestures", "_poll_touch", "_poll_popup", "draw_",
                     "_update_leds", "_expire_alerts"):
            self.assertNotIn(name, self.source)
        # Only render_screens, the Node test's helper, draws directly.
        self.assertEqual(self.source.count("app._render()"), 1)

    def test_it_makes_no_request_of_its_own(self):
        for name in ("socket", "urequests", "requests", "urllib", "fetch("):
            self.assertNotIn(name, self.source)


class WorkflowTests(unittest.TestCase):
    def test_the_tests_workflow_builds_then_checks_the_demo(self):
        workflow = read(".github", "workflows", "tests.yml")
        build = workflow.index("python tools/build_demo_site.py")
        check = workflow.index("node tools/check_demo_wasm.mjs")
        self.assertLess(build, check)
        self.assertGreater(build, workflow.index("python -m unittest discover"))
        # Only one job needs it: the Ubuntu run on the Python the image uses.
        self.assertEqual(workflow.count("matrix.os == 'ubuntu-latest' && matrix.python == '3.13'"), 2)

    def test_the_docs_workflow_builds_the_demo_before_mkdocs_and_watches_the_app(self):
        workflow = read(".github", "workflows", "docs.yml")
        self.assertLess(workflow.index("python tools/build_demo_site.py"),
                        workflow.index("mkdocs build --strict"))
        for trigger in ("on:\n  workflow_dispatch:", ):
            self.assertIn(trigger.replace("\n", "\r\n") if "\r\n" in workflow else trigger,
                          workflow)
        lists = re.findall(r"paths:\n((?:\s+- '[^']+'\n?)+)", workflow.replace("\r\n", "\n"))
        self.assertEqual(len(lists), 2)
        for paths in lists:
            for watched in ("presto/**", "daemon/src/**", "tools/build_demo_site.py",
                            "tools/presto_preview/**", "tools/presto_bundle.py"):
                self.assertIn("'%s'" % watched, paths)

    def test_no_new_action_was_added(self):
        for name in ("tests.yml", "docs.yml"):
            for use in re.findall(r"uses:\s*(\S+)", read(".github", "workflows", name)):
                self.assertRegex(use, r"@[0-9a-f]{40}$", use)


@unittest.skipUnless(NODE, "Node is not installed; CI runs this check")
class SmokeTestTests(unittest.TestCase):
    """The Node check must pass on a good build and fail on each kind of bad one."""

    @classmethod
    def setUpClass(cls):
        cls.out = tempfile.mkdtemp(prefix="demo-smoke-")
        result = subprocess.run([sys.executable, BUILD, "--out", cls.out], cwd=ROOT,
                                capture_output=True, text=True, timeout=180)
        if result.returncode != 0:
            raise AssertionError(result.stdout + result.stderr)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.out, ignore_errors=True)

    def smoke(self, folder):
        environment = dict(os.environ, DEMO_APP_DIR=folder)
        return subprocess.run([NODE, SMOKE], cwd=ROOT, env=environment,
                              capture_output=True, text=True, timeout=180)

    def copy(self):
        folder = tempfile.mkdtemp(prefix="demo-smoke-copy-")
        self.addCleanup(shutil.rmtree, folder, ignore_errors=True)
        target = os.path.join(folder, "app")
        shutil.copytree(self.out, target)
        return target

    def test_a_good_build_passes_and_compares_every_screen(self):
        result = self.smoke(self.out)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("compared 56 screens", result.stdout)
        self.assertIn("passed", result.stdout)

    def test_a_changed_pixel_hash_fails(self):
        folder = self.copy()
        path = os.path.join(folder, "hashes.json")
        with open(path, encoding="utf-8") as handle:
            hashes = json.load(handle)
        hashes["scenes"]["critical"]["dash"] = "0" * 64
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(hashes, handle)
        result = self.smoke(folder)
        self.assertEqual(result.returncode, 1)
        self.assertIn("pixels differ for scene critical, card dash", result.stderr)

    def test_an_app_file_that_does_not_import_fails(self):
        folder = self.copy()
        with open(os.path.join(folder, "lib", "ticks.py"), "a", encoding="utf-8") as handle:
            handle.write("\nthis is not python\n")
        result = self.smoke(folder)
        self.assertEqual(result.returncode, 1)
        self.assertIn("cannot import ticks", result.stderr)

    def test_a_changed_app_file_draws_different_pixels_and_fails(self):
        folder = self.copy()
        path = os.path.join(folder, "lib", "ui.py")
        with open(path, encoding="utf-8") as handle:
            source = handle.read()
        self.assertIn("DEMO - SAMPLE DATA", source)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(source.replace("DEMO - SAMPLE DATA", "DEMO - SAMPLE DAT0"))
        result = self.smoke(folder)
        self.assertEqual(result.returncode, 1)
        self.assertIn("pixels differ", result.stderr)

    def test_a_missing_build_is_a_failure_and_not_a_skip(self):
        result = self.smoke(os.path.join(self.out, "nowhere"))
        self.assertEqual(result.returncode, 1)
        self.assertIn("missing", result.stderr)


if __name__ == "__main__":
    unittest.main()
