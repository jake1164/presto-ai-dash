"""Build the static assets for the clickable demo on the documentation site.

    python tools/build_demo_site.py            # writes docs/user/demo/app/
    python tools/build_demo_site.py --out DIR  # a throwaway directory

The output is generated and gitignored:

* ``lib/*.py`` - the real app modules, taken from ``tools/presto_bundle``'s
  single inventory (``main.py`` becomes ``ai_dash_app.py``; the launcher and
  every private file are left out), plus the host drawing code
  ``fonts.py`` and ``framebuffer.py``. PNG encoding stays in ``png.py`` and
  is not copied: the browser writes pixels to a canvas instead.
* ``demo.json`` - for every ``STEP_SECONDS`` of the loop, the payload and the
  lazy card details, all built by the daemon's own demo module for one fixed
  moment, so the page and the device show the same numbers.
* ``poster.png`` - a CPython render of Home at the calm scene, for readers
  without JavaScript and for print.
* ``hashes.json`` - SHA-256 of the pixels of Home and every card at every
  scene, which the Node smoke test compares against.

It needs only the standard library. The checks that run it install nothing:
``demo.py`` and the payload builders must import in a plain Python
environment, and a build with a fattened payload must fail on the size limit.
"""

import argparse
import hashlib
import json
import os
import shutil
import sys
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)
PREVIEW = os.path.join(HERE, "presto_preview")
DAEMON_SRC = os.path.join(REPO_ROOT, "daemon", "src")
DEFAULT_OUT = os.path.join(REPO_ROOT, "docs", "user", "demo", "app")

for _folder in (HERE, PREVIEW, DAEMON_SRC):
    if _folder not in sys.path:
        sys.path.append(_folder)

from presto_bundle import app_files, is_private_name  # noqa: E402
from presto_ai_dash import demo  # noqa: E402

# One moment for every reading, so two builds are byte-for-byte identical. The
# page is sample data; its numbers never age.
DEMO_NOW = datetime(2026, 10, 24, 12, 0, tzinfo=timezone.utc)
# The whole demo file is downloaded before the screen starts; keep it small.
MAX_DEMO_BYTES = 400 * 1024
POSTER_SCENE = "calm"
WIDTH = HEIGHT = 240
CHANNELS = 3


def app_sources():
    """Library files for the page: public app modules and the host drawing code."""
    files = {}
    for source, target in app_files(REPO_ROOT).items():
        if not target.startswith("/ai_dash_files/"):
            continue  # the launcher is the device's, not the page's
        name = os.path.basename(target)
        if is_private_name(name):
            raise SystemExit("refusing to publish a private app file: " + name)
        files[name] = source.read_bytes()
    for name in ("fonts.py", "framebuffer.py"):
        with open(os.path.join(PREVIEW, name), "rb") as handle:
            files[name] = handle.read()
    return files


def demo_data(now=DEMO_NOW):
    """The data file: every step's payload and its lazy card details."""
    steps = []
    for second in range(0, demo.LOOP_SECONDS, demo.STEP_SECONDS):
        payload = demo.demo_payload(second, now=now)
        card_ids = [card["id"] for card in payload.get("cards") or ()]
        steps.append({
            "t": second,
            "payload": payload,
            "details": {card_id: demo.demo_card_detail(card_id, second, now=now)
                        for card_id in card_ids},
        })
    return {
        "loop_seconds": demo.LOOP_SECONDS,
        "step_seconds": demo.STEP_SECONDS,
        "scenes": [{"name": name, "start": start} for name, start in demo.SCENES],
        "steps": steps,
    }


def _fill_details(app, second, now):
    """Give the app every card's detail, as the lazy fetch would have."""
    app.card_details.clear()
    for card in app.cards:
        detail = demo.demo_card_detail(card.id, second, now=now)
        if detail is None:
            continue
        app.card_details[card.id] = {"tiles": list(detail.get("tiles") or ()),
                                     "lines": list(detail.get("lines") or ()),
                                     "at": 0}


def render_screens(now=DEMO_NOW):
    """Hash Home and every card at every scene, and render the poster."""
    import render as preview  # tools/presto_preview/render.py

    main, _net = preview._load_app_modules()
    from framebuffer import Framebuffer
    from png import png

    display = Framebuffer(WIDTH, HEIGHT)
    presto = preview.FakePresto(display)
    app = main.App(presto)
    # There is no event loop here; the harness pre-fills every detail instead.
    app._maybe_load_detail = lambda: None

    scenes = {}
    cards = None
    poster = None
    for name, start in demo.SCENES:
        app._set_payload(demo.demo_payload(start, now=now))
        _fill_details(app, start, now)
        if cards is None:
            cards = [card.id for card in app.cards]
        hashes = {}
        for index, card in enumerate(app.cards):
            app.index = index
            app.home_scroll.offset = 0
            app._render()
            hashes[card.id] = hashlib.sha256(bytes(display.pixels)).hexdigest()
            if name == POSTER_SCENE and card.id == "dash":
                poster = png(display)
        scenes[name] = hashes
    if poster is None:
        raise SystemExit("the poster scene %r has no Home card" % POSTER_SCENE)
    return {"algorithm": "sha256", "width": WIDTH, "height": HEIGHT,
            "channels": CHANNELS, "cards": cards, "scenes": scenes}, poster


def build(out=DEFAULT_OUT, now=DEMO_NOW):
    """Write the whole demo directory; return a small summary for the caller."""
    if os.path.isdir(out):
        # Only clear a directory this script wrote before: a mistyped --out
        # must not delete someone's files.
        entries = set(os.listdir(out))
        if entries and not entries <= {"demo.json", "hashes.json", "poster.png", "lib"}:
            raise SystemExit("%s is not a previous build; refusing to overwrite it" % out)
        shutil.rmtree(out)
    os.makedirs(os.path.join(out, "lib"))

    sources = app_sources()
    for name, data in sources.items():
        with open(os.path.join(out, "lib", name), "wb") as handle:
            handle.write(data)

    contents = demo_data(now)
    # A static host cannot list a directory; the page reads the names here.
    contents["lib"] = sorted(sources)
    data = json.dumps(contents, separators=(",", ":"), sort_keys=True).encode("utf-8")
    if len(data) > MAX_DEMO_BYTES:
        raise SystemExit("demo.json would be %d bytes; the limit is %d"
                         % (len(data), MAX_DEMO_BYTES))
    with open(os.path.join(out, "demo.json"), "wb") as handle:
        handle.write(data)

    hashes, poster = render_screens(now)
    with open(os.path.join(out, "poster.png"), "wb") as handle:
        handle.write(poster)
    with open(os.path.join(out, "hashes.json"), "w", encoding="utf-8") as handle:
        json.dump(hashes, handle, indent=2, sort_keys=True)
        handle.write("\n")

    return {"out": out, "lib": sorted(sources), "demo_bytes": len(data),
            "cards": hashes["cards"], "scenes": sorted(hashes["scenes"]),
            "max_demo_bytes": MAX_DEMO_BYTES}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", default=DEFAULT_OUT,
                        help="output directory (default: docs/user/demo/app)")
    args = parser.parse_args(argv)
    result = build(args.out)
    print("%d app files, demo.json %d bytes, %d scenes x %d cards -> %s"
          % (len(result["lib"]), result["demo_bytes"], len(result["scenes"]),
             len(result["cards"]), result["out"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
