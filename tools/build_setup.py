"""Write the standalone Presto setup installer for this checkout.

The admin page renders ``presto_setup.py`` for the exact bundle its daemon
serves. This tool writes the same file to disk, so a developer can run it on
the USB computer without a daemon, inspect it, or keep a copy for a repair.

    python tools/build_setup.py                     # writes .preview/presto_setup.py
    python tools/build_setup.py --out setup.py
    python tools/build_setup.py --check             # exit 1 when the file is stale

The rendered file carries only the public app files: no config, no secrets, no
device token and no setup unlock code. The handoff is a separate download.
"""

import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "daemon" / "src"))

from presto_bundle import snapshot  # noqa: E402
from presto_ai_dash.device_setup import render_setup_script  # noqa: E402
from presto_ai_dash.release import release_version  # noqa: E402

DEFAULT_OUT = ROOT / ".preview" / "presto_setup.py"


def render(root=ROOT):
    return render_setup_script(snapshot(root), release_version())


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", default=str(DEFAULT_OUT),
                        help="where to write the script (default: %(default)s)")
    parser.add_argument("--check", action="store_true",
                        help="report whether the file at --out is current; change nothing")
    arguments = parser.parse_args(argv)
    target = Path(arguments.out)
    rendered = render()
    if arguments.check:
        current = target.read_bytes() if target.is_file() else b""
        if current != rendered:
            print("%s is out of date: run tools/build_setup.py" % target)
            return 1
        print("%s is current" % target)
        return 0
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(rendered)
    print("wrote %s (%d bytes, app revision %s)"
          % (target, len(rendered), snapshot(ROOT)["revision"][:12]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
