// Smoke test for the documentation site's clickable demo.
//
//   python tools/build_demo_site.py && node tools/check_demo_wasm.mjs
//
// It loads the vendored MicroPython WebAssembly runtime, writes the generated
// app files (the device's own modules) into it, and checks three things:
//   1. every app module imports under MicroPython, which is also a
//      device-compatibility check;
//   2. Home and every card, at every scene, draw exactly the pixels CPython
//      drew when the build recorded hashes.json;
//   3. the page's driver runs the app: a swipe changes the card, a tap opens
//      one, the "Stale" switch turns the screen stale and then recovers.
// It exits 1 on any difference or failure. It never skips: if the runtime
// cannot load, that is a failure too.
//
// Node's own crypto hashes the pixels. Nothing is installed or downloaded.

import { createHash } from "node:crypto";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const demoDir = path.join(here, "..", "docs", "user", "demo");
// DEMO_APP_DIR points the check at another build, which the tests use.
const appDir = process.env.DEMO_APP_DIR || path.join(demoDir, "app");
const vendorDir = path.join(demoDir, "vendor");

const failures = [];
const fail = (message) => { failures.push(message); console.error("FAIL: " + message); };
const check = (condition, message) => { if (!condition) fail(message); };

function need(file) {
  if (!fs.existsSync(file)) {
    console.error("missing " + file + " (run python tools/build_demo_site.py first)");
    process.exit(1);
  }
  return file;
}

const demo = JSON.parse(fs.readFileSync(need(path.join(appDir, "demo.json")), "utf8"));
const expected = JSON.parse(fs.readFileSync(need(path.join(appDir, "hashes.json")), "utf8"));
const libDir = need(path.join(appDir, "lib"));
const driverSource = fs.readFileSync(need(path.join(demoDir, "driver.py")));

// Node loads ES modules only from .mjs (or a package marked as one), and the
// vendored wrapper is published as .js for web servers. Copy it aside.
const scratch = fs.mkdtempSync(path.join(os.tmpdir(), "demo-wasm-"));
fs.copyFileSync(need(path.join(vendorDir, "micropython.js")), path.join(scratch, "micropython.mjs"));
fs.copyFileSync(need(path.join(vendorDir, "micropython.wasm")), path.join(scratch, "micropython.wasm"));
process.on("exit", () => fs.rmSync(scratch, { recursive: true, force: true }));

const { loadMicroPython } = await import(pathToFileURL(path.join(scratch, "micropython.mjs")).href);

const output = [];
const mp = await loadMicroPython({
  stdout: (line) => output.push(line),
  stderr: (line) => output.push("ERR " + line),
});

mp.FS.mkdir("/lib");
const modules = [];
for (const name of fs.readdirSync(libDir).sort()) {
  mp.FS.writeFile("/lib/" + name, fs.readFileSync(path.join(libDir, name)));
  if (name.endsWith(".py")) modules.push(name.slice(0, -3));
}
mp.FS.writeFile("/lib/driver.py", driverSource);

// What the page provides to the driver.
const frames = [];
const touches = [];
let led = [0, 0, 0];
const toBytes = (proxy) => Uint8Array.from(mp.PyProxy.toJs(proxy));
const sha = (bytes) => createHash("sha256").update(bytes).digest("hex");
const got = {};
globalThis.stepJson = (index) => JSON.stringify(demo.steps[index]);
globalThis.takeTouch = () => (touches.length ? touches.shift() : "");
globalThis.pushFrame = (proxy) => frames.push(sha(toBytes(proxy)));
globalThis.setLed = (r, g, b) => { led = [r, g, b]; };
globalThis.checkFrame = (scene, card, proxy) => {
  (got[scene] ||= {})[card] = sha(toBytes(proxy));
};

const run = (code) => mp.runPython(code);
const read = (code) => { run("__r = " + code); return mp.globals.get("__r"); };

// 1. Every app module imports under MicroPython.
run("import sys\nsys.path.insert(0, '/lib')");
for (const name of modules) {
  try { run("import " + name); } catch (error) { fail("cannot import " + name + ": " + error); }
}

// 2. The pixels match the ones CPython drew.
try {
  const scenes = demo.scenes.map((scene) => "(" + JSON.stringify(scene.name) + ", " + scene.start + ")");
  run("import driver\ndriver.render_screens([" + scenes.join(", ") + "], " + demo.step_seconds + ")");
  let compared = 0;
  for (const [scene, cards] of Object.entries(expected.scenes)) {
    for (const [card, hash] of Object.entries(cards)) {
      compared += 1;
      check(got[scene] && got[scene][card] === hash,
        "pixels differ for scene " + scene + ", card " + card);
    }
  }
  check(compared === demo.scenes.length * expected.cards.length,
    "compared " + compared + " screens, expected " + demo.scenes.length * expected.cards.length);
  console.log("compared " + compared + " screens against hashes.json");
} catch (error) {
  fail("rendering failed: " + error);
}

// 3. The driver runs the app.
// The app times gestures and polls with the real clock, so the ticks are spaced
// like the page's: about 20 ms apart.
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
async function drive(ticks) {
  for (let i = 0; i < ticks; i++) {
    run("driver.tick(20)");
    await sleep(20);
  }
}
function swipe(x0, x1) {
  touches.push("down," + x0 + ",130");
  for (let i = 1; i <= 6; i++) touches.push("move," + Math.round(x0 + (x1 - x0) * i / 6) + ",130");
  touches.push("up," + x1 + ",130");
}
try {
  run("driver.start(" + demo.loop_seconds + ", " + demo.step_seconds + ", " + demo.steps.length + ")");
  await drive(3);
  check(frames.length > 0, "the first ticks drew nothing");
  check(read("driver.driver.app._current_card().id") === "dash", "the demo does not start on Home");
  check(read("driver.driver.app.demo") === true, "the app did not take the payload's demo flag");
  check(read("driver.driver.app.connection_error") === null, "the first poll did not succeed");

  swipe(190, 50);
  await drive(20);
  check(read("driver.driver.app._current_card().id") !== "dash", "a swipe left did not leave Home");

  swipe(50, 190);
  await drive(20);
  check(read("driver.driver.app._current_card().id") === "dash", "a swipe right did not return to Home");

  run("driver.set_failure(True)");
  await drive(3);
  check(read("driver.driver.app.connection_error") !== null, "the Stale switch did not make the poll fail");
  run("driver.set_failure(False)");
  await drive(3);
  check(read("driver.driver.app.connection_error") === null, "the screen did not recover after Stale");

  run("driver.jump(" + demo.scenes[demo.scenes.length - 1].start + ")");
  await drive(3);
  check(frames.length >= 5, "ticks after a scene jump drew nothing");
  console.log("drove " + frames.length + " frames, led " + led.join(","));
} catch (error) {
  fail("driving the app failed: " + error);
}

if (failures.length) {
  console.error(output.join("\n"));
  console.error(failures.length + " check(s) failed");
  process.exit(1);
}
console.log("demo wasm smoke test passed");
