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
globalThis.queueTestTouch = (event) => touches.push(String(event));
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

  touches.push("down,100,20", "up,100,20");
  await drive(12);
  check(read("driver.driver.app._current_card().id") === "demo-opencode", "a Home row tap did not open its account");
  swipe(50, 190);
  await drive(20);
  check(read("driver.driver.app._current_card().id") === "dash", "could not return Home after a row tap");

  touches.push("down,120,180");
  for (let i = 1; i <= 6; i++) touches.push("move,120," + Math.round(180 - 100 * i / 6));
  touches.push("up,120,80");
  await drive(20);
  check(read("driver.driver.app.home_scroll.offset") > 0, "a vertical drag did not scroll Home");

  run("driver.set_failure(True)");
  await drive(3);
  check(read("driver.driver.app.connection_error") !== null, "the Stale switch did not make the poll fail");
  run("driver.set_failure(False)");
  await drive(3);
  check(read("driver.driver.app.connection_error") === null, "the screen did not recover after Stale");

  run("driver.jump(54)");
  await drive(3);
  check(read("driver.driver.app.cards[0].status") === "critical", "critical scene did not reach Home");
  run("driver.run_now(driver.driver.client.post_alert('demo-claude', False))\ndriver.driver.poll_soon()");
  await drive(3);
  check(read("driver.driver.app.cards[0].status") === "critical", "muting Claude hid Cloud's separate alert");
  run("driver.run_now(driver.driver.client.post_alert('demo-ollama-cloud', False))\ndriver.driver.poll_soon()");
  await drive(3);
  check(read("driver.driver.app.cards[0].status") === "ok", "the demo bell did not update Home");
  check(read("driver.driver.app.overall_accent") === "green", "the demo bell did not update the LED");
  check(read("next(card.status for card in driver.driver.app.cards if card.id == 'demo-claude')") === "critical",
    "the demo bell hid the service's real status");

  // Drive the real hold and disconnect timers with a monotonic test clock.
  // Each new app captures this clock in Uptime, so no real 30-second wait or
  // replacement app logic is needed.
  run(`
demo_ticks = driver.time.ticks_ms
demo_clock = [demo_ticks()]
driver.time.ticks_ms = lambda: demo_clock[0]
try:
    driver.start(${demo.loop_seconds}, ${demo.step_seconds}, ${demo.steps.length})
    driver.tick(0)
    demo_app = driver.driver.app
    driver.js.queueTestTouch('down,120,130')
    driver.tick(20)
    demo_clock[0] += demo_app.long_press_ms + 20
    driver.tick(demo_app.long_press_ms + 20)
    demo_muted = demo_app.alerts.muted
    driver.js.queueTestTouch('up,120,130')
    demo_clock[0] += 20
    driver.tick(20)
    demo_clock[0] += 200
    driver.js.queueTestTouch('down,120,130')
    driver.tick(20)
    demo_clock[0] += demo_app.long_press_ms + 20
    driver.tick(demo_app.long_press_ms + 20)
    demo_unmuted = not demo_app.alerts.muted
    driver.js.queueTestTouch('up,120,130')
    demo_clock[0] += 20
    driver.tick(20)
    driver.set_failure(True)
    driver.tick(0)
    demo_retained = demo_app.connection_error is not None and demo_app._connection_screen() is None
    demo_clock[0] += 27000
    driver.tick(27000)
    demo_still_stale = demo_app._connection_screen() is None
    demo_clock[0] += 4000
    driver.tick(4000)
    demo_offline = demo_app._connection_screen() is not None
    demo_offline_red = driver.driver.display.pixels[0] > driver.driver.display.pixels[1]
    import ui
    demo_chip_x, demo_chip_y, _, _ = ui.demo_chip_rect(driver.driver.display)
    demo_chip_at = (demo_chip_y * driver.driver.display.width + demo_chip_x) * 3
    demo_chip_rgb = driver.driver.display.pixels[demo_chip_at:demo_chip_at + 3]
    demo_offline_chip = demo_chip_rgb[0] > demo_chip_rgb[1] and demo_chip_rgb[2] > demo_chip_rgb[1]
    driver.set_failure(False)
    driver.jump(${demo.scenes[demo.scenes.length - 1].start})
    driver.tick(0)
    demo_recovered = (demo_app.connection_error is None and demo_app._connection_screen() is None
                      and demo_app.cards[0].status == 'ok' and demo_app.demo)
finally:
    driver.time.ticks_ms = demo_ticks
`);
  for (const [name, message] of [
    ["demo_muted", "a hold did not mute"], ["demo_unmuted", "a second hold did not unmute"],
    ["demo_retained", "daemon loss discarded the last good screen"],
    ["demo_still_stale", "daemon loss showed offline before 30 seconds"],
    ["demo_offline", "daemon loss did not show offline after 30 seconds"],
    ["demo_offline_red", "the offline screen was not red"],
    ["demo_offline_chip", "the offline screen lost its sample-data label"],
    ["demo_recovered", "the screen did not recover after a prolonged daemon loss"],
  ]) check(read(name) === true, message);

  // Exercise the driver's poll scheduler across a full loop without sleeping.
  run(`
review_ticks = driver.time.ticks_ms
review_clock = [0]
driver.time.ticks_ms = lambda: review_clock[0]
try:
    driver.start(${demo.loop_seconds}, ${demo.step_seconds}, ${demo.steps.length})
    review_seen = set()
    for second in range(${demo.loop_seconds}):
        review_clock[0] = second * 1000
        driver.tick(1000)
        review_seen.add(driver.driver.app.cards[0].status)
finally:
    driver.time.ticks_ms = review_ticks
`);
  for (const status of ["ok", "active", "warning", "near_limit", "critical", "rate_limited", "offline"]) {
    check(read(JSON.stringify(status) + " in review_seen") === true, "automatic playback missed " + status);
  }

  run("driver.start(" + demo.loop_seconds + ", " + demo.step_seconds + ", " + demo.steps.length + ")");
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
