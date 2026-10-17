# Agent instructions

Presto AI Dash: a Python daemon (`daemon/`) that gathers AI usage and quotas,
and a MicroPython app (`presto/`) that shows them on a Pimoroni Presto's 240x240
touchscreen. Start with `README.md`; `docs/milestones.md` says what is built.

## Rules that apply to every task

- Run the tests before calling work done: `cd daemon && ../.venv/Scripts/python -m unittest discover -s tests -q`
  (Windows; use `../.venv/bin/python` elsewhere). Use the repo's `.venv`, not a global Python.
- `presto/` runs on MicroPython. Only `main.py` and `ui.py` may import `presto`,
  `machine`, `picographics` or `picovector` (`test_presto_structure.py` enforces it);
  `ui.py` currently imports none, so draw through the `display` object it is given.
  Bitmap fonts only. Keep on-screen text ASCII.
- Never read, print, commit or paste the contents of `dash.toml`, `dash.db`,
  `dash.db.key`, `presto/config.py` or `presto/secrets.py`. They hold tokens and keys.
- Do not claim a UI change works because tests pass. Look at it (below).

## Seeing the Presto screen while you work

Do not ask the user for a photo of the device. Run the preview, which executes
the real `presto/main.py` App and `presto/ui.py` against an emulated 240x240
RGB565 display with the firmware's own bitmap fonts, shown at 480x480. It
polls the real daemon, so it shows real data. Saving any `presto/*.py` file
restarts the app on the new code within about a second, so you only edit and look.

### 1. Make sure it is running

```bash
curl -s http://127.0.0.1:8790/state?after=-1      # JSON means it is up
```

If that fails, start it **in the background** (it never exits, so a foreground
call will hang your shell):

```bash
.venv/Scripts/python tools/presto_preview/live.py          # live daemon (.venv/bin/python off Windows)
.venv/Scripts/python tools/presto_preview/live.py --demo   # static demo, no daemon needed
```

The user may already have it running from VS Code ("Daemon + admin page + Presto preview").
If port 8790 is in use, that is the same server: use it, do not start another.
It binds to 127.0.0.1 only. The live view shows real account names and quotas,
so do not expose it, and do not paste screenshots of it anywhere external.

### 2. Look at it: pick the first option your tools support

**A. Playwright browser tool (best: you can also interact).**
Navigate to `http://127.0.0.1:8790/`, then screenshot the element `#screen` (the
display itself, not the page). Page title is "Presto Live". Drive the app:

- `browser_evaluate`: `await presto.gesture("left")`. Names: `left`/`right`
  (previous/next card), `up`/`down` (scroll the Home list), `tap`, `long` (mute).
- Or real mouse events on `#screen`: drag to swipe or scroll, click to tap.
- Read state: `await presto.state()` gives `{frame, led, error, demo, network_fail}`.
- The checkbox `#netfail` simulates daemon loss. After about 10 s the screen shows
  "STALE - retrying", and after 30 s the red offline screen (the real timers).
  Untick it to confirm recovery. `#demo` toggles the static demo.
- If `state.error` is non-null, your code raised. The traceback is also drawn
  over the screen. Fix it; the app restarts on the next save.

Take the screenshot after each edit, wait about 1.5 s for the reload, and compare.
Playwright MCP writes files to `.playwright-mcp/` (gitignored). Do not read
console 404s for `favicon.ico` as a problem.

**B. No browser tool? Use the CLI helper, then open the PNG.**

```bash
.venv/Scripts/python tools/presto_preview/shot.py                           # writes .preview/current.png
.venv/Scripts/python tools/presto_preview/shot.py --gesture left            # swipe, then capture
.venv/Scripts/python tools/presto_preview/shot.py --gesture up --gesture up --out .preview/end.png
```

It prints the PNG path, which you must then view with whatever image-reading
ability you have (attach or open the file). Exit code 1 means the app raised;
the traceback is on stderr. Gestures are the same names as above.

**C. Cannot view images at all.** Say so, make the change, and rely on the tests
in `daemon/tests/test_ui.py` and `test_cards.py`, which check text fits and does
not overlap. Do not claim visual verification.

### 3. Every card, without a server

```bash
.venv/Scripts/python tools/presto_preview/render.py                        # built-in sample payload
.venv/Scripts/python tools/presto_preview/render.py --payload live --out .preview/live
.venv/Scripts/python tools/presto_preview/render.py --payload fixture.json --stale
```

Writes one PNG per card to `.preview/` (plus Home scrolled to its end), with
`--stale`, `--offline` and `--muted` variants. Use it to review a whole change
at once, or for edge-case payloads from a JSON file.

### What the preview does not show

Sound, LED hardware (only a glow hint), memory and CPU limits, Wi-Fi joining,
and non-ASCII text (draws blank; the device remaps some accents). Passing here
does not prove it fits in the Presto's memory. Say so when that matters, and
leave device deployment to the user.

## Debugging the daemon and preview in VS Code

`.vscode/launch.json` has "Daemon + admin page", "Presto preview only", and a
compound "Daemon + admin page + Presto preview" that starts both and opens both tabs
(admin first, then the preview in front). Stopping the compound
stops both. Outside VS Code, run the daemon with
`.venv/Scripts/python -m presto_ai_dash.app --config dash.toml --admin` (admin
page on port 8788) and `live.py` as above, each in the background.
