"""Render every Presto screen to PNG with the device's own drawing code.

    python tools/presto_preview/render.py                    # demo payload
    python tools/presto_preview/render.py --payload live     # daemon in presto/config.py
    python tools/presto_preview/render.py --payload fixture.json --stale

The real presto/main.py App picks each screen and presto/ui.py draws it into
framebuffer.py, which uses the firmware's bitmap fonts at 240x240 and doubles
to the 480x480 panel. Output: one PNG per screen, manifest.json and index.html
(open it as a file). For the running, interactive dash use live.py instead.
"""

import argparse
import html
import importlib.abc
import importlib.machinery
import importlib.util
import json
import os
import re
import sys
import tempfile
import time
from urllib.request import Request, urlopen

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(HERE))
if os.path.dirname(HERE) not in sys.path:
    sys.path.append(os.path.dirname(HERE))
from presto_bundle import app_files, snapshot  # noqa: E402

if HERE not in sys.path:
    sys.path.insert(0, HERE)
import demo_client  # noqa: E402
import lifecycle_fixtures  # noqa: E402

PRESTO_DIR = os.path.join(REPO_ROOT, "presto")
DEFAULT_OUT = os.path.join(REPO_ROOT, ".preview")
PAYLOADS = ("demo", "live")


class _AppSourceLoader(importlib.machinery.SourceFileLoader):
    """Execute frozen source bytes, never timestamp-based .pyc cache entries."""
    def __init__(self, name, path, data):
        super().__init__(name, str(path))
        self.data = data

    def get_code(self, fullname):
        return self.source_to_code(self.data, self.path)


class _AppImports(importlib.abc.MetaPathFinder):
    def __init__(self):
        self.sources = {}

    def find_spec(self, fullname, path=None, target=None):
        source = self.sources.get(fullname)
        if source:
            filename, data = source
            return importlib.util.spec_from_file_location(fullname, filename,
                loader=_AppSourceLoader(fullname, filename, data))
        return None


_APP_IMPORTS = _AppImports()


def _load_app_modules(real_sleep=False):
    # MicroPython's time helpers, before main.py is imported.
    time.ticks_ms = lambda: int(time.monotonic() * 1000)
    time.ticks_diff = lambda a, b: a - b
    time.sleep_ms = (lambda ms: time.sleep(ms / 1000)) if real_sleep else (lambda ms: None)
    # Appended, as in the tests, so presto/secrets.py never shadows stdlib.
    if PRESTO_DIR not in sys.path:
        sys.path.append(PRESTO_DIR)
    # Resolve the entry from the deployment inventory, not an unrelated `main`
    # cached elsewhere on sys.path. All helper code still imports from presto/.
    inventory = app_files()
    bundle = snapshot()
    entry = next(path for path, target in inventory.items() if target == "/ai_dash_files/ai_dash_app.py")
    sources = {path.stem: (path, bundle["files"][target]) for path, target in inventory.items()
               if path != entry and target.startswith("/ai_dash_files/")}
    for name, (path, _) in sources.items():
        loaded = sys.modules.get(name)
        if loaded is not None:
            if os.path.realpath(getattr(loaded, "__file__", "")) != os.path.realpath(path):
                raise RuntimeError("app module name conflicts with another module: " + name)
            sys.modules.pop(name)
    _APP_IMPORTS.sources = sources
    if _APP_IMPORTS not in sys.meta_path:
        sys.meta_path.insert(0, _APP_IMPORTS)
    spec = importlib.util.spec_from_file_location("_presto_preview_app", entry,
        loader=_AppSourceLoader("_presto_preview_app", entry, bundle["files"][inventory[entry]]))
    main = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(main)
    main.__presto_revision__ = bundle["revision"]
    # The preview is not a device: keep its boot counter in a temporary file
    # rather than the filesystem root, which it cannot write anyway.
    try:
        main.config.BOOT_COUNTER_FILE = os.path.join(
            tempfile.gettempdir(), "presto-preview-boot.txt")
    except Exception:
        pass
    import net
    # The preview is not a device: it says so, and it reports the revision of
    # the shared app code it runs.
    net.CLIENT_NAME_OVERRIDE = "psl"
    net.REVISION_OVERRIDE = bundle["revision"][:12]
    net.DEVICE_ID_OVERRIDE = ""
    return main, net


class FakePresto(object):
    NUM_LEDS = 7

    def __init__(self, display):
        self.display = display
        self.leds = [(0, 0, 0)] * self.NUM_LEDS
        self.frames = 0

    def set_led_rgb(self, index, r, g, b):
        self.leds[index] = (r, g, b)

    def update(self):
        self.frames += 1


def _fetch_live(net, config):
    host = getattr(config, "DAEMON_HOST", None)
    if not host:
        raise SystemExit("presto/config.py has no DAEMON_HOST; use --payload demo or a file")
    url = net.build_url(host, int(getattr(config, "DAEMON_PORT", 8787)))
    request = Request(url, headers=net.build_headers(getattr(config, "SHARED_TOKEN", "")))
    try:
        with urlopen(request, timeout=int(getattr(config, "HTTP_TIMEOUT", 8))) as reply:
            return json.loads(reply.read())
    except Exception as exc:
        # Never echo the request: it carries the device token.
        raise SystemExit("could not fetch %s: %s" % (url, type(exc).__name__))


def _mute_left_ms(text):
    """How long the mute chip has left: "3h", "45m", or None for no end."""
    if text is None:
        return None
    text = str(text).strip().lower()
    if text in ("", "0", "none", "forever"):
        return None
    if text.endswith("h"):
        return int(text[:-1]) * 60 * 60 * 1000
    if text.endswith("m"):
        return int(text[:-1]) * 60 * 1000
    return int(text) * 60 * 60 * 1000


def _fill_card_details(app, t):
    """Give the app every card's demo detail, as a poll would have fetched it.

    The render harness has no event loop, so it cannot let the app fetch a
    detail lazily. Filling the map here is what makes a rendered card show its
    tiles and its full set of lines, exactly as a card opened in PSL does.
    """
    for card in app.cards:
        detail = demo_client.card_detail_at(card.id, t)
        if detail is None:
            continue
        app.card_details[card.id] = {"tiles": list(detail.get("tiles") or ()),
                                     "lines": list(detail.get("lines") or ()),
                                     "at": 0}


def render(payload="demo", out=DEFAULT_OUT, stale=False, offline=False, muted=False,
           mute_left=None, scene=None, lifecycle="none"):
    main, net = _load_app_modules()
    if HERE not in sys.path:
        sys.path.insert(0, HERE)
    from framebuffer import Framebuffer
    from png import png
    scene = scene or demo_client.DEFAULT_SCENE
    if scene not in demo_client.SCENES:
        raise SystemExit("--scene must be one of: %s" % ", ".join(demo_client.SCENES))
    second = demo_client.scene_offset(scene)

    display = Framebuffer()
    presto = FakePresto(display)
    app = main.App(presto)
    # No event loop here: the lazy detail would have to be a task. Every
    # screen is rendered with whatever the payload and the demo carry.
    app._maybe_load_detail = lambda: None
    if payload == "demo":
        # The chosen scene's payload. It carries the demo flag, so the app
        # draws the chip, and its card details are filled below.
        data = demo_client.payload_at(second)
    elif payload == "live":
        data = _fetch_live(net, main.config)
    else:
        with open(payload) as handle:
            data = json.load(handle)
    if lifecycle != "none":
        # A display fixture: the agreed indicator states, fed through the real
        # app. Nothing here is a device record or USB evidence.
        data = lifecycle_fixtures.apply(data, lifecycle)
    app._set_payload(data)
    if payload == "demo":
        _fill_card_details(app, second)
    if app.error:
        print("payload rejected by the app: %s" % app.error, file=sys.stderr)
    app.alerts.muted = muted
    if muted:
        # The time left on the mute chip: "3h", "45m", or "0" for a mute
        # that lasts until the user presses again.
        left = _mute_left_ms(mute_left)
        if left is None and mute_left not in (None, ""):
            app.alerts.set_mute_hours(0)
        app.alerts.set_mute(True, left)
    if offline:
        app.connection_error = ("Daemon offline", "TimeoutError @head", True)
    elif stale:
        app.connection_error = ("Daemon offline", "Check daemon and LAN address", False)

    os.makedirs(out, exist_ok=True)
    for old in os.listdir(out):
        if old.endswith(".png"):
            os.remove(os.path.join(out, old))
    screens = []

    def capture(name, label):
        app._update_leds()
        app._render()
        filename = "%02d-%s.png" % (len(screens), re.sub(r"[^\w-]+", "-", name).strip("-") or "card")
        with open(os.path.join(out, filename), "wb") as handle:
            handle.write(png(display))
        screens.append({"file": filename, "label": label, "led": list(presto.leds[0])})

    for index, card in enumerate(app.cards):
        app.index = index
        app.home_scroll.offset = 0
        capture(card.title, card.title)
        if card.id == "dash" and app.home_scroll.limit and not offline:
            app.home_scroll.offset = app.home_scroll.limit
            capture(card.title + "-scrolled", card.title + " (scrolled to end)")
            app.home_scroll.offset = 0
        if offline:
            break  # Every card shows the same connection screen.

    state = {"payload": payload if payload in PAYLOADS else os.path.basename(payload),
             "scene": scene if payload == "demo" else None,
             "stale": stale, "offline": offline, "muted": muted,
             "lifecycle": lifecycle,
             "code_revision": main.__presto_revision__}
    with open(os.path.join(out, "manifest.json"), "w") as handle:
        json.dump({"state": state, "screens": screens}, handle, indent=2)
    with open(os.path.join(out, "index.html"), "w", encoding="utf-8") as handle:
        handle.write(_viewer(state, screens))
    return screens


def _viewer(state, screens):
    links = html.escape("payload: %(payload)s, stale: %(stale)s, offline: %(offline)s, muted: %(muted)s" % state)
    tiles = "\n".join(
        '<figure id="screen-%02d" data-file="%s"><div class="bezel" style="--led: rgb(%d,%d,%d)">'
        '<img src="%s" width="480" height="480" alt="%s"></div><figcaption>%02d &middot; %s</figcaption></figure>'
        % (i, s["file"], s["led"][0], s["led"][1], s["led"][2], s["file"],
           html.escape(s["label"]), i, html.escape(s["label"]))
        for i, s in enumerate(screens))
    return """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Presto Preview</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
body { margin: 0; padding: 16px; background: #0b0d10; color: #c9d1d9; font: 14px system-ui, sans-serif; }
nav { margin-bottom: 16px; } nav { color: #8b949e; }
main { display: flex; flex-wrap: wrap; gap: 28px; }
figure { margin: 0; }
.bezel { padding: 18px; background: #1b1d21; border-radius: 22px;
  box-shadow: 0 0 26px 2px var(--led); }
img { display: block; image-rendering: pixelated; max-width: 100%%; height: auto; }
figcaption { margin-top: 8px; color: #8b949e; }
</style></head><body>
<nav>%s</nav>
<main>%s</main>
</body></html>
""" % (links, tiles)



def main_cli(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--payload", default="demo",
                        help="demo (the scripted sample data), live (daemon in "
                             "presto/config.py), or a JSON file path")
    parser.add_argument("--out", default=DEFAULT_OUT)
    parser.add_argument("--stale", action="store_true", help="a failed refresh after good data")
    parser.add_argument("--offline", action="store_true", help="the full-screen connection error")
    parser.add_argument("--muted", action="store_true")
    parser.add_argument("--mute-left", default=None,
                        help="how long the mute chip has left: 3h, 45m, or 0 "
                             "for a mute that lasts until pressed again")
    parser.add_argument("--scene", default=None,
                        help="with --payload demo, the scene to render: %s "
                             "(default %s)" % (", ".join(demo_client.SCENES),
                                                demo_client.DEFAULT_SCENE))
    parser.add_argument("--lifecycle", default="none",
                        choices=lifecycle_fixtures.STATES,
                        help="a display fixture for the shared bottom-right "
                             "indicator: %s (default none)" % ", ".join(lifecycle_fixtures.STATES))
    args = parser.parse_args(argv)
    screens = render(args.payload, args.out, args.stale, args.offline, args.muted,
                      args.mute_left, args.scene, args.lifecycle)
    for screen in screens:
        print(os.path.join(args.out, screen["file"]))


if __name__ == "__main__":
    main_cli()
