"""Capture the running Presto preview to a PNG, optionally after a gesture.

    python tools/presto_preview/shot.py                       # -> .preview/current.png
    python tools/presto_preview/shot.py --gesture left        # swipe to the next card first
    python tools/presto_preview/shot.py --gesture up --gesture up --out .preview/end.png

For agents with no browser tool: needs only the server from live.py. Prints the
PNG path (480x480) and a one-line state summary. Gestures: left, right, up
(scrolls the list down), down, tap, long.
"""

import argparse
import json
import os
import sys
import time
from urllib.request import Request, urlopen

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
STROKES = {
    "left": [(190, 130), (50, 130)], "right": [(50, 130), (190, 130)],
    "up": [(120, 180), (120, 80)], "down": [(120, 80), (120, 180)],
}


def call(base, path, body=None, timeout=25):
    request = Request(base + path, data=None if body is None else json.dumps(body).encode(),
                      headers={"Content-Type": "application/json"})
    with urlopen(request, timeout=timeout) as reply:
        return reply.read()


def touch(base, events):
    call(base, "/touch", events)


def gesture(base, name):
    if name in STROKES:
        (x0, y0), (x1, y1) = STROKES[name]
        steps = 6
        events = [["down", x0, y0]] + [
            ["move", x0 + (x1 - x0) * i // steps, y0 + (y1 - y0) * i // steps]
            for i in range(1, steps + 1)] + [["up", x1, y1]]
        touch(base, events)
    elif name == "tap":
        touch(base, [["down", 120, 130], ["up", 120, 130]])
    elif name == "long":
        touch(base, [["down", 120, 130]])
        time.sleep(0.9)
        touch(base, [["up", 120, 130]])
    else:
        raise SystemExit("unknown gesture %r" % name)
    time.sleep(0.4)  # let the 20 ms app loop and render finish


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--gesture", action="append", default=[])
    parser.add_argument("--port", type=int, default=8790)
    parser.add_argument("--out", default=os.path.join(REPO_ROOT, ".preview", "current.png"))
    args = parser.parse_args(argv)
    base = "http://127.0.0.1:%d" % args.port
    try:
        call(base, "/state?after=-1", timeout=3)
    except OSError:
        raise SystemExit("preview not running on %s; start it: python tools/presto_preview/live.py" % base)
    for name in args.gesture:
        gesture(base, name)
    state = json.loads(call(base, "/state?after=-1"))
    if state["error"]:
        print("APP ERROR:\n" + state["error"], file=sys.stderr)
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "wb") as handle:
        handle.write(call(base, "/frame.png?scale=2"))
    print(args.out)
    print("frame=%d led=%s mode=%s%s" % (
        state["frame"], state["led"], "demo" if state["demo"] else "live daemon",
        " network-failure-simulated" if state["network_fail"] else ""))
    return 1 if state["error"] else 0


if __name__ == "__main__":
    sys.exit(main())
