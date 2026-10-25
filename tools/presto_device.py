"""Check or sync the exact app files used by PSL, through an explicit USB command.

    python tools/presto_device.py plan
    python tools/presto_device.py check --port COM3
    python tools/presto_device.py sync --port COM3

check/sync interrupt the device, then relaunch AI Dash. Neither command reads
or writes device config, secrets, provider data, or the firmware launcher.

The USB engine lives in the package (``presto_ai_dash.device_install``) so the
release wheel can use it too; this script feeds it the checkout's own files.
"""

import argparse
import importlib.util
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "daemon" / "src"))

from presto_bundle import ROOT, RECEIPT, snapshot
from presto_ai_dash import device_install as _engine
from presto_ai_dash.device_install import (  # noqa: F401  (the names tests and callers use)
    MARKER, LAUNCH, HELPERS, inspection_script, commit_script, run_remote,
    parse_hashes, record, device_lock, _operate)


def operate(action, port, bundle, remote=run_remote, receipt=RECEIPT):
    return _engine.operate(action, port, bundle, remote=remote, receipt=receipt)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("action", choices=("plan", "check", "sync"))
    parser.add_argument("--port", help="explicit USB port, e.g. COM3 or /dev/ttyACM0")
    args = parser.parse_args(argv)
    if args.action != "plan" and (not args.port or args.port in ("auto", "list")):
        parser.error("check/sync require an explicit --port; automatic board selection is disabled")
    try:
        bundle = snapshot()
        print("Shared Presto app revision: " + bundle["revision"][:12])
        if args.action == "plan":
            for path in bundle["files"]:
                print("  " + path)
            print("Private configuration, secrets and firmware main.py are excluded.")
            return 0
        if importlib.util.find_spec("mpremote") is None:
            print('Install USB tools: python -m pip install -e "daemon[device]"', file=sys.stderr)
            return 2
        different = operate(args.action, args.port, bundle)
        if different:
            print("Device differs from PSL:")
            for path in different:
                print("  " + path)
            print("Run Sync Presto to install this revision.")
            return 1
        print("Verified: device and PSL app files match " + bundle["revision"][:12] + ". AI Dash relaunched.")
        return 0
    except SyntaxError as exc:
        print("App syntax error: %s:%s" % (exc.filename, exc.lineno), file=sys.stderr)
        return 2
    except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
        print(str(exc) if isinstance(exc, (RuntimeError, ValueError)) else
              "USB operation failed or timed out. Check the port and retry.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
