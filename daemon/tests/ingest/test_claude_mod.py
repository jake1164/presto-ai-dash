"""The Presto Claude mod: its files, and what its hooks module does when run.

The structural checks always run. The behavioural ones run the generated
``register.js`` under Node with a stand-in for Claude Code's mod API, and are
skipped on a machine without Node.
"""

import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from presto_ai_dash.collectors import claude_mod

NODE = shutil.which("node")
WINDOWS_FOLDER = "C:\\Users\\Jo \"Q\" Doe\\.presto-ai-dash\\claude-quota"
POSIX_FOLDER = "/home/jo/.presto-ai-dash/claude-quota"


class FilesTests(unittest.TestCase):
    def test_the_mod_is_three_files(self):
        self.assertEqual(sorted(claude_mod.mod_files(POSIX_FOLDER)),
                         [".claude-plugin/plugin.json", "hooks/hooks.json", "hooks/register.js"])

    def test_the_manifest_name_is_one_claude_code_accepts(self):
        name = claude_mod.PLUGIN_JSON["name"]
        self.assertRegex(name, r"^[a-z0-9]+(-[a-z0-9]+)*$")
        for reserved in ("claude-", "anthropic-", "anthropics-", "cc-plugin-"):
            self.assertFalse(name.startswith(reserved))

    def test_the_json_files_parse(self):
        files = claude_mod.mod_files(POSIX_FOLDER)
        self.assertEqual(json.loads(files[".claude-plugin/plugin.json"])["name"], "presto-quota")
        self.assertEqual(json.loads(files["hooks/hooks.json"])["modules"], ["./register.js"])

    def test_no_placeholder_is_left_in_the_hooks_module(self):
        for folder in (POSIX_FOLDER, WINDOWS_FOLDER):
            self.assertNotIn("__", claude_mod.register_js(folder))

    def test_the_folder_is_a_json_string_with_a_trailing_separator(self):
        posix = claude_mod.register_js(POSIX_FOLDER)
        self.assertIn(json.dumps(POSIX_FOLDER + "/"), posix)
        windows = claude_mod.register_js(WINDOWS_FOLDER)
        self.assertIn(json.dumps(WINDOWS_FOLDER + "\\"), windows)

    def test_an_empty_folder_is_refused(self):
        for bad in ("", None):
            with self.assertRaises(ValueError):
                claude_mod.register_js(bad)

    def test_the_mod_uses_only_the_file_write_and_the_clock(self):
        text = claude_mod.register_js(POSIX_FOLDER)
        self.assertEqual(set(re.findall(r"\$\.[a-z]+\.[a-z]+", text)),
                         {"$.fs.write", "$.clock.now"})

    def test_the_mod_cannot_reach_the_network_a_process_or_credentials(self):
        text = claude_mod.register_js(POSIX_FOLDER)
        text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)       # comments may say anything
        text = re.sub(r"(?m)^\s*//.*$", "", text)
        for forbidden in ("fetch(", "XMLHttpRequest", "require(", "import(", "child_process",
                          "process.", "$.http", "$.env", "authorize", "$.session.usage",
                          "readFile", "$.fs.read"):
            self.assertNotIn(forbidden, text, forbidden)

    def test_the_mod_never_exports_session_start_figures(self):
        text = claude_mod.register_js(POSIX_FOLDER)
        start = text.split('on("session.start"')[1].split('on("session.measure"')[0]
        self.assertIn("save($, null, [])", start)
        self.assertNotIn("rateLimits", start)


@unittest.skipUnless(NODE, "Node is not installed")
class BehaviourTests(unittest.TestCase):
    DRIVER = r"""
import { pathToFileURL } from "node:url";
const scenario = JSON.parse(process.argv[3]);
const writes = [];
const fake = (options) => ({
  fs: { write: async (path, text) => {
    if (options.failWrite) throw new Error("disk full");
    writes.push({ path, text }); } },
  clock: { now: async () => {
    if (options.failClock) throw new Error("no clock");
    return options.clock; } },
});
async function load(tag) {
  const handlers = {};
  const module = await import(pathToFileURL(process.argv[2]).href + "?" + tag);
  module.register((name, handler) => { handlers[name] = handler; });
  return handlers;
}
const results = [];
for (const step of scenario.steps) {
  const handlers = await load(step.instance || "a");
  const passed = [];
  for (const [event, payload] of step.events) {
    const next = (e) => { passed.push(e); return "passed:" + event; };
    let outcome;
    try { outcome = await handlers[event](fake(step.options || {}), payload, next); }
    catch (error) { outcome = "threw:" + error.message; }
    results.push({ event, outcome, writes: writes.splice(0) });
  }
}
console.log(JSON.stringify(results));
"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        (root / "package.json").write_text('{"type": "module"}')
        (root / "register.js").write_text(claude_mod.register_js(POSIX_FOLDER))
        (root / "driver.mjs").write_text(self.DRIVER)

    def run_steps(self, steps):
        root = Path(self.temp.name)
        done = subprocess.run([NODE, str(root / "driver.mjs"), str(root / "register.js"),
                               json.dumps({"steps": steps})],
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        return json.loads(done.stdout)

    @staticmethod
    def body(result, index=0):
        return json.loads(result["writes"][index]["text"])

    def measure(self, limits, **options):
        return {"events": [["session.measure", {"rateLimits": limits, "changed": []}]],
                "options": options}

    def test_session_start_writes_a_marker_with_no_reading(self):
        [result] = self.run_steps([{"events": [["session.start", {"cwd": "/work/secret"}]],
                                    "options": {"clock": 0}}])
        body = self.body(result)
        self.assertEqual(body["schema"], claude_mod.MOD_SCHEMA)
        self.assertIsNone(body["observed_at"])
        self.assertEqual(body["rate_limits"], [])
        self.assertEqual(body["seq"], 1)
        self.assertNotIn("secret", result["writes"][0]["text"])

    def test_a_measure_writes_the_two_windows_with_the_event_time(self):
        [result] = self.run_steps([self.measure([
            {"kind": "five_hour", "percentUsed": 9.5, "resetsAt": "2026-11-10T15:30:00.000Z"},
            {"kind": "seven_day", "percentUsed": 19, "resetsAt": "2026-11-17T04:00:00Z"}],
            clock=1789000000000)])
        body = self.body(result)
        self.assertEqual(body["observed_at"], "2026-09-10T00:26:40.000Z")
        self.assertEqual(body["rate_limits"], [
            {"kind": "five_hour", "percent_used": 9.5, "resets_at": "2026-11-10T15:30:00.000Z"},
            {"kind": "seven_day", "percent_used": 19, "resets_at": "2026-11-17T04:00:00.000Z"}])
        self.assertTrue(result["writes"][0]["path"].startswith(POSIX_FOLDER + "/session-"))
        self.assertTrue(result["writes"][0]["path"].endswith(".json"))

    def test_the_spend_limit_and_unknown_kinds_are_not_exported(self):
        [result] = self.run_steps([self.measure([
            {"kind": "spend_limit", "percentUsed": 5, "resetsAt": "2026-11-10T15:30:00Z"},
            {"kind": "seven_day", "percentUsed": 19, "resetsAt": "2026-11-17T04:00:00Z"}],
            clock=1789000000000)])
        self.assertEqual([item["kind"] for item in self.body(result)["rate_limits"]], ["seven_day"])

    def test_a_measure_with_nothing_usable_writes_nothing(self):
        for limits in ([], None, "x", [{"kind": "five_hour", "percentUsed": "NaN",
                                        "resetsAt": "2026-11-10T15:30:00Z"}],
                       [{"kind": "five_hour", "percentUsed": 5, "resetsAt": "not a time"}],
                       [{"kind": "five_hour", "percentUsed": -1,
                         "resetsAt": "2026-11-10T15:30:00Z"}]):
            [result] = self.run_steps([self.measure(limits, clock=1789000000000)])
            self.assertEqual(result["writes"], [], limits)

    def test_the_sequence_counts_up_within_one_session(self):
        good = [{"kind": "five_hour", "percentUsed": 5, "resetsAt": "2026-11-10T15:30:00Z"}]
        step = {"events": [["session.start", {}], ["session.measure", {"rateLimits": good}],
                           ["session.measure", {"rateLimits": good}]],
                "options": {"clock": 1789000000000}}
        results = self.run_steps([step])
        self.assertEqual([self.body(result)["seq"] for result in results], [1, 2, 3])
        self.assertEqual(len({result["writes"][0]["path"] for result in results}), 1)

    def test_two_sessions_write_two_different_files(self):
        good = [{"kind": "five_hour", "percentUsed": 5, "resetsAt": "2026-11-10T15:30:00Z"}]
        first = self.measure(good, clock=1789000000000)
        second = dict(self.measure(good, clock=1789000000000), instance="b")
        one, two = self.run_steps([first, second])
        self.assertNotEqual(one["writes"][0]["path"], two["writes"][0]["path"])

    def test_a_clock_that_fails_falls_back_to_the_system_clock(self):
        [result] = self.run_steps([self.measure([
            {"kind": "five_hour", "percentUsed": 5, "resetsAt": "2026-11-10T15:30:00Z"}],
            failClock=True)])
        self.assertRegex(self.body(result)["observed_at"], r"^20\d\d-\d\d-\d\dT[\d:.]+Z$")

    def test_a_failed_write_never_reaches_claude_code(self):
        good = [{"kind": "five_hour", "percentUsed": 5, "resetsAt": "2026-11-10T15:30:00Z"}]
        step = {"events": [["session.start", {}], ["session.measure", {"rateLimits": good}]],
                "options": {"clock": 1789000000000, "failWrite": True}}
        for result in self.run_steps([step]):
            self.assertTrue(result["outcome"].startswith("passed:"), result["outcome"])

    def test_every_event_is_passed_on(self):
        good = [{"kind": "five_hour", "percentUsed": 5, "resetsAt": "2026-11-10T15:30:00Z"}]
        step = {"events": [["session.start", {}], ["session.measure", {"rateLimits": good}],
                           ["session.measure", {}]], "options": {"clock": 1789000000000}}
        for result in self.run_steps([step]):
            self.assertTrue(result["outcome"].startswith("passed:"), result["outcome"])


if __name__ == "__main__":
    unittest.main()
