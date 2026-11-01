/* The clickable demo: the real Presto app, in MicroPython WebAssembly.
 *
 * This script is loaded on every page of the site and does nothing unless the
 * page has the demo's elements. It loads nothing until the reader presses
 * Start, and it contacts no other host: the runtime, the app files and the
 * sample data are all served from this site (demo/vendor and demo/app).
 *
 * The app is the device's own code. docs/user/demo/driver.py supplies the
 * screen buffer, the touch queue and a client that reads demo.json; this file
 * supplies the canvas, the pointer and keyboard input, and the timer.
 */
(function () {
  "use strict";

  var script = document.currentScript;
  var root = document.getElementById("presto-demo");
  if (!root || !script) return;

  var base = new URL(".", script.src).href;
  var $ = function (id) { return document.getElementById(id); };
  var poster = $("presto-poster"), canvas = $("presto-canvas"), startButton = $("presto-start");
  var controls = $("presto-controls"), pauseButton = $("presto-pause");
  var restartButton = $("presto-restart"), sceneSelect = $("presto-scene");
  var staleBox = $("presto-stale"), errorBox = $("presto-error"), statusLine = $("presto-status");
  var context = canvas.getContext("2d");
  var image = context.createImageData(240, 240);
  var pixels32 = new Uint32Array(image.data.buffer);

  var TICK_MS = 20;        // the device's main loop period
  var MAX_STEP_MS = 250;   // a long gap (a hidden tab) must not skip the scenario
  var HOLD_MS = 900;       // the M key's long press

  var mp = null, demo = null;
  var running = false, paused = false, timer = null, last = 0, stopped = false;
  var visible = true;  // the screen is on screen; the loop idles when it is not
  var touches = [];
  var interacted = false;

  // The page's analytics (javascripts/analytics.js), which sends nothing unless
  // the reader accepted cookies. Only the events below are reported: never a
  // frame, a simulated touch or anything the app is showing.
  function report(name, params) {
    if (window.prestoAnalytics) window.prestoAnalytics.track(name, params);
  }

  // "Did they use it at all", asked once per page load, not once per touch.
  function firstInteraction(kind) {
    if (interacted) return;
    interacted = true;
    report("demo_interaction", { interaction: kind });
  }

  if (!window.WebAssembly) {
    show("This browser cannot run the demo. The picture above is the Home screen.");
    return;
  }
  startButton.hidden = false;
  startButton.addEventListener("click", start);

  function show(message) {
    errorBox.textContent = message;
    errorBox.hidden = !message;
  }
  function say(message) { statusLine.textContent = message; }

  function get(path, kind) {
    return fetch(base + path).then(function (response) {
      if (!response.ok) throw new Error(path + ": " + response.status);
      return kind === "json" ? response.json() : response.arrayBuffer();
    });
  }

  // -- the page's side of the driver ----------------------------------------

  function expose() {
    window.stepJson = function (index) { return JSON.stringify(demo.steps[index]); };
    window.takeTouch = function () { return touches.length ? touches.shift() : ""; };
    window.pushFrame = function (proxy) {
      var rgb = mp.PyProxy.toJs(proxy);
      for (var i = 0, n = 0; i < 57600; i++, n += 3) {
        pixels32[i] = 0xff000000 | (rgb[n + 2] << 16) | (rgb[n + 1] << 8) | rgb[n];
      }
      context.putImageData(image, 0, 0);
    };
    window.setLed = function (r, g, b) {
      root.style.setProperty("--presto-led", "rgb(" + r + "," + g + "," + b + ")");
    };
  }

  function py(code) { return mp.runPython(code); }

  // -- loading ----------------------------------------------------------------

  function start() {
    startButton.disabled = true;
    startButton.textContent = "Loading...";
    show("");
    report("demo_start");
    Promise.all([
      import(base + "vendor/micropython.js"),
      get("app/demo.json", "json"),
      fetch(base + "driver.py").then(function (r) {
        if (!r.ok) throw new Error("driver.py: " + r.status);
        return r.arrayBuffer();
      })
    ]).then(function (parts) {
      demo = parts[1];
      return parts[0].loadMicroPython({
        stdout: function (line) { if (window.console) console.log(line); },
        stderr: function (line) { if (window.console) console.warn(line); }
      }).then(function (runtime) {
        mp = runtime;
        mp.FS.mkdir("/lib");
        mp.FS.writeFile("/lib/driver.py", new Uint8Array(parts[2]));
        return Promise.all(demo.lib.map(function (name) {
          return get("app/lib/" + name).then(function (data) {
            mp.FS.writeFile("/lib/" + name, new Uint8Array(data));
          });
        }));
      });
    }).then(function () {
      expose();
      py("import sys\nsys.path.insert(0, '/lib')\nimport driver");
      fillScenes();
      begin();
      report("demo_ready");
    }).catch(function (error) {
      startButton.disabled = false;
      startButton.textContent = "Start demo";
      // A coarse category only: the message can name a file or a host, and
      // nothing about this page needs to travel with it.
      report("demo_error", { error_kind: "start_failed" });
      show("The demo could not start: " + (error && error.message ? error.message : error) +
        ". Reload the page to try again.");
    });
  }

  function fillScenes() {
    sceneSelect.replaceChildren();
    var first = document.createElement("option");
    first.value = ""; first.textContent = "Jump to...";
    sceneSelect.appendChild(first);
    demo.scenes.forEach(function (scene) {
      var option = document.createElement("option");
      option.value = String(scene.start);
      option.textContent = scene.name.replace(/_/g, " ");
      sceneSelect.appendChild(option);
    });
  }

  function begin() {
    touches.length = 0;
    py("driver.start(" + demo.loop_seconds + ", " + demo.step_seconds + ", " + demo.steps.length + ")");
    poster.hidden = true;
    startButton.hidden = true;
    canvas.hidden = false;
    controls.hidden = false;
    paused = false;
    pauseButton.textContent = "Pause";
    staleBox.checked = false;
    sceneSelect.value = "";
    running = true;
    stopped = false;
    say("Demo running.");
    last = performance.now();
    schedule(0);
    canvas.focus({ preventScroll: true });
  }

  // -- the loop ----------------------------------------------------------------

  function schedule(delay) {
    clearTimeout(timer);
    timer = setTimeout(frame, delay);
  }

  function frame() {
    if (!running || paused || stopped || !visible) return;
    var began = performance.now();
    var elapsed = Math.min(began - last, MAX_STEP_MS);
    last = began;
    try {
      py("driver.tick(" + Math.round(elapsed) + ")");
    } catch (error) {
      stopped = true;
      report("demo_error", { error_kind: "runtime_stopped" });
      show("The demo stopped: " + (error && error.message ? error.message : error) +
        ". Press Restart.");
      return;
    }
    schedule(Math.max(0, TICK_MS - (performance.now() - began)));
  }

  function setPaused(value) {
    paused = value;
    pauseButton.textContent = value ? "Resume" : "Pause";
    say(value ? "Demo paused." : "Demo running.");
    if (!value && running && !stopped) { last = performance.now(); schedule(0); }
  }

  // Nothing needs drawing while the screen is scrolled out of view.
  if (window.IntersectionObserver) {
    new IntersectionObserver(function (entries) {
      visible = entries[entries.length - 1].isIntersecting;
      if (visible && running && !paused && !stopped && !document.hidden) {
        last = performance.now();
        schedule(0);
      }
    }).observe(canvas.parentNode);
  }

  // A hidden tab runs timers rarely; stop instead of lurching on return.
  document.addEventListener("visibilitychange", function () {
    if (!running || stopped) return;
    if (document.hidden) { clearTimeout(timer); }
    else if (!paused) { last = performance.now(); schedule(0); }
  });

  pauseButton.addEventListener("click", function () { setPaused(!paused); });
  restartButton.addEventListener("click", function () {
    report("demo_restart");
    show("");
    begin();
  });
  sceneSelect.addEventListener("change", function () {
    if (sceneSelect.value === "" || !running) return;
    // The scene's own name, from the demo's script — a scenario label such as
    // "near limit", never anything read from the simulated accounts.
    var chosen = sceneSelect.options[sceneSelect.selectedIndex].textContent;
    py("driver.jump(" + Number(sceneSelect.value) + ")");
    sceneSelect.value = "";
    report("demo_scene", { scene: chosen.trim().replace(/[^A-Za-z0-9]+/g, "_") });
  });
  staleBox.addEventListener("change", function () {
    if (!running) return;
    py("driver.set_failure(" + (staleBox.checked ? "True" : "False") + ")");
  });

  // -- input: the pointer becomes the touch panel -----------------------------

  function queue(kind, x, y) {
    if (kind === "move" && touches.length && touches[touches.length - 1].lastIndexOf("move", 0) === 0) {
      touches.pop();  // the app reads one sample per pass; a drag must not lag behind
    }
    touches.push(kind + "," + x + "," + y);
  }
  function point(event) {
    var box = canvas.getBoundingClientRect();
    var clamp = function (v) { return Math.max(0, Math.min(239, Math.round(v))); };
    return [clamp((event.clientX - box.left) * 240 / box.width),
            clamp((event.clientY - box.top) * 240 / box.height)];
  }
  var down = false;
  canvas.addEventListener("pointerdown", function (event) {
    if (!running || paused) return;
    firstInteraction("pointer");
    canvas.setPointerCapture(event.pointerId);
    down = true;
    var p = point(event);
    queue("down", p[0], p[1]);
    event.preventDefault();
  });
  canvas.addEventListener("pointermove", function (event) {
    if (!down) return;
    var p = point(event);
    queue("move", p[0], p[1]);
  });
  function lift(event) {
    if (!down) return;
    down = false;
    var p = point(event);
    queue("up", p[0], p[1]);
  }
  canvas.addEventListener("pointerup", lift);
  canvas.addEventListener("pointercancel", lift);
  canvas.addEventListener("contextmenu", function (event) { event.preventDefault(); });

  // The same strokes the preview uses, for the keyboard.
  function stroke(x0, y0, x1, y1) {
    queue("down", x0, y0);
    for (var i = 1; i <= 6; i++) {
      touches.push("move," + Math.round(x0 + (x1 - x0) * i / 6) + "," + Math.round(y0 + (y1 - y0) * i / 6));
    }
    queue("up", x1, y1);
  }
  canvas.addEventListener("keydown", function (event) {
    if (!running || paused) return;
    var handled = true;
    switch (event.key) {
      case "ArrowLeft": stroke(190, 130, 50, 130); break;
      case "ArrowRight": stroke(50, 130, 190, 130); break;
      case "ArrowUp": stroke(120, 80, 120, 180); break;
      case "ArrowDown": stroke(120, 180, 120, 80); break;
      case "Enter": queue("down", 120, 130); queue("up", 120, 130); break;
      case "m": case "M":
        queue("down", 120, 130);
        setTimeout(function () { queue("up", 120, 130); }, HOLD_MS);
        break;
      default: handled = false;
    }
    if (handled) { firstInteraction("keyboard"); event.preventDefault(); }
  });

  // For tests: read-only state and the same gestures the keys make.
  function read(expression) {
    py("__r = " + expression);
    return mp.globals.get("__r");
  }
  window.prestoDemo = {
    running: function () { return running && !paused && !stopped; },
    frames: function () { return running ? Number(read("driver.driver.presto.frames")) : 0; },
    card: function () { return running ? String(read("driver.driver.app._current_card().id")) : ""; },
    homeStatus: function () { return running ? String(read("driver.driver.app.cards[0].status")) : ""; },
    stale: function () { return running ? read("driver.driver.app.connection_error") !== null : false; },
    // Times one tick per touch sample ("kind,x,y" strings), for the drag
    // benchmark. Pause the demo first so the timer does not interleave.
    measure: function (events) {
      var times = [];
      events.forEach(function (event) { touches.push(event); });
      var remaining = events.length + 5;
      // The app times a drag with the real clock, so ticks keep the page's pace.
      return new Promise(function (resolve) {
        (function next() {
          var began = performance.now();
          py("driver.tick(20)");
          times.push(performance.now() - began);
          if (--remaining > 0) setTimeout(next, TICK_MS); else resolve(times);
        })();
      });
    }
  };
})();
