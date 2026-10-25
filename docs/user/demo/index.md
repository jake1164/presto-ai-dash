# See it running

This is the real Presto AI Dash app, the same code that runs on the device,
drawing a scripted set of sample accounts. Press **Start**, then use it like the
Presto: click a row to open it, swipe to move between cards, hold to mute.

<div class="presto-demo" id="presto-demo">
  <div class="presto-bezel" id="presto-bezel">
    <div class="presto-screen">
      <img class="presto-poster" id="presto-poster" src="app/poster.png" width="240" height="240" alt="The Presto Home screen showing sample accounts and the purple DEMO - SAMPLE DATA chip">
      <canvas id="presto-canvas" width="240" height="240" tabindex="0" role="img" aria-label="The Presto screen, running the demo" hidden></canvas>
      <button class="presto-start" id="presto-start" type="button" hidden>Start demo</button>
    </div>
  </div>
  <p class="presto-error" id="presto-error" role="alert" hidden></p>
  <div class="presto-controls" id="presto-controls" hidden>
    <button id="presto-pause" type="button">Pause</button>
    <button id="presto-restart" type="button">Restart</button>
    <label>Scene
      <select id="presto-scene" aria-label="Jump to a scene"></select></label>
    <label title="Pretend the daemon has gone away. The screen shows STALE after about 10 seconds and the offline screen after about 30, as on the device."><input type="checkbox" id="presto-stale"> Stale</label>
  </div>
  <p class="presto-note">Sample data, not a live account. Click or tap to press, drag to swipe or scroll, hold to mute.
  With the screen focused: arrow keys, Enter, and M.</p>
  <p class="presto-note" id="presto-status" role="status"></p>
</div>

## What you are looking at

- **The code is the device's.** The page runs the app's own Python modules in
  your browser, using MicroPython compiled to WebAssembly. Nothing is sent
  anywhere, and no account is involved.
- **The numbers are made up.** Six accounts (OpenCode Go, Codex, Claude, Ollama
  Cloud, Ollama and LiteLLM) play through a 90-second scenario: calm, climbing,
  a warning, a near-limit, critical, rate-limited, offline and recovery. Use the
  **Scene** menu to jump to one.
- **The purple chip is always there.** It says the readings are sample data. A
  daemon in [demo mode](../guides/admin-page.md#demo-mode) shows the same chip on
  a real Presto.
- **Stale** pretends the daemon has gone away, so you can watch the screen go
  stale and then offline, and recover when you untick it.

To run it on your own Presto, start with [what Presto AI Dash does](../getting-started/what-it-does.md).
Without JavaScript this page shows a picture of the Home screen instead.
