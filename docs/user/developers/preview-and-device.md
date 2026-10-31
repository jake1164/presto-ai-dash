# The preview and the device sync

There is one app implementation, in `presto/`. It runs in two places: the
host-side preview (PSL), and the physical Presto. The preview fakes only the
hardware. The device runs copies of exactly the same bytes.

That is why there is no simulator-specific copy of the app logic, and why a
change you make is visible in the preview within a second.

## The preview

```bash
.venv/Scripts/python tools/presto_preview/live.py          # live daemon
.venv/Scripts/python tools/presto_preview/live.py --demo   # the scripted demo
.venv/Scripts/python tools/presto_preview/live.py --config dash.toml   # follow that daemon
```

`live.py` runs the real `presto/main.py` App against an emulated 240x240
RGB565 framebuffer, using the firmware's own bitmap fonts, shown at 480x480.
It polls the real daemon, so it shows real data.

By default it polls the daemon named in `presto/config.py`, the same settings
file the device uses. When you are debugging a daemon on this computer, pass
`--config` with the daemon's own configuration file instead: the preview then
polls that daemon's port and token, and a bind-everything address such as
`0.0.0.0` becomes `127.0.0.1`. The VS Code launch entries do this for you, so
the preview always follows the daemon you started beside it. If the daemon has
no `shared_token` set it invents a new one at every start, which the preview
cannot know, so it says so and keeps the token from `presto/config.py`.

The line under the screen says where it is polling, for example
`live daemon · polling 127.0.0.1:8787 (dash.toml)`: the address and which file
named it (`dash.toml` when you pass `--config`, `presto/config.py` otherwise),
never the token. Demo mode says it has no daemon.

It binds to `127.0.0.1` only. The live view shows real account names and
quotas, so do not expose it and do not paste a screenshot of it anywhere
external.

Drive it with the browser: drag to swipe or scroll, click to tap, hold to
mute. The arrow keys, Enter and M do the same. "Simulate network failure"
exercises the real stale, offline and recovery timers.

The **Demo** checkbox runs the app on the scripted demo instead of the daemon:
the same six sample accounts the daemon's demo mode and the published demo page
show, built by the daemon's own `demo` module, with the device's demo chip. It
contacts nothing and writes nothing. The **Scene** menu jumps the loop to a
named scene (`calm`, `climbing`, `warning`, `near_limit`, `critical`,
`rate_limited`, `offline`, `recovery`), and the loop keeps playing from there,
so you can watch a scene progress instead of waiting for it. "Simulate network
failure" works in demo mode too.

If you have no browser tool, the command-line helper does the same job:

```bash
.venv/Scripts/python tools/presto_preview/shot.py
.venv/Scripts/python tools/presto_preview/shot.py --gesture left
.venv/Scripts/python tools/presto_preview/shot.py --gesture up --gesture up --out .preview/end.png
```

It prints the path of the PNG it wrote.

## Rendering every card, without a server

```bash
.venv/Scripts/python tools/presto_preview/render.py                        # the demo payload
.venv/Scripts/python tools/presto_preview/render.py --scene rate_limited --out .preview/busy
.venv/Scripts/python tools/presto_preview/render.py --payload live --out .preview/live
.venv/Scripts/python tools/presto_preview/render.py --payload fixture.json --stale
```

It writes one PNG per card, plus the Home list scrolled to its end, with
`--stale`, `--offline` and `--muted` variants. `--scene` picks the demo scene
to render; the default is the first, calm one. A demo card is rendered with its
tiles and its full set of lines, as if it had been opened. Use it to review a
whole change at once, or for the edge-case payloads you do not have a live
account for.

**Never render from the live preview for a public screenshot.** The live
payload carries your real account names and quotas. Use a fixture.

## The revision and the USB check

`tools/presto_bundle.py` is the shared inventory. It decides which files in
`presto/` are public app files, maps `main.py` to `/ai_dash_files/ai_dash_app.py`,
includes the carousel entry, and computes a revision hash over the bytes.

The preview runs a frozen snapshot of those bytes and bypasses the Python
bytecode caches, so the revision it reports identifies the bytes it actually
loaded. The footer and `/state` also show the **dated last USB
verification**: a check of the installed files, not live hardware telemetry.

After you edit an app file, the preview shows "App changed since USB
verification". A different installed hash shows "Device code differs".

## The sync

After changing any deployable file under `presto/`, run the tests, look at
the preview, and then sync the device:

```bash
.venv/Scripts/python tools/presto_device.py sync --port COM3
.venv/Scripts/python tools/presto_device.py check --port COM3
```

On macOS or Linux use `.venv/bin/python` and the device's explicit USB port
or path. To find the port:

```bash
.venv/Scripts/python -m mpremote connect list
```

The tool refuses to pick a board automatically. You name the port, and it
checks that the board identifies as a Pimoroni Presto.

In VS Code the same two actions are **Terminal -> Run Task -> Sync Presto**
and **Check Presto sync**.

### What the sync does

It freezes a source snapshot, uploads the staged files, verifies every
SHA-256, replaces the installed files with a rollback on a write or rename
failure, and verifies the installed bytes afterwards.

It leaves alone:

- the firmware `/main.py` launcher,
- your private `config.py` and `secrets.py`,
- the daemon's data.

A helper that is no longer in the source is retired on the device as a
non-importable `.presto-prev` backup, so an old helper cannot silently be
used.

Re-running the sync repairs an interrupted transfer. After a USB or power
loss, always verify or sync again.

## What the preview does not show

Sound, the LED hardware beyond a glow hint, the memory and CPU limits, the
Wi-Fi joining, and non-ASCII text (which draws blank; the device remaps some
accents). Passing in the preview does not prove it fits in the Presto's
memory. Say so when it matters.

## The rule

Never claim the device is current without a successful USB verification for
the current revision. A dated USB check is not continuous device telemetry.

## See also

- [Running the tests](running-the-tests.md)
- [Architecture overview](architecture.md)
