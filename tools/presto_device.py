"""Check, sync or migrate the exact app files used by PSL, through explicit USB.

    python tools/presto_device.py plan
    python tools/presto_device.py check --port COM3
    python tools/presto_device.py sync --port COM3
    python tools/presto_device.py migrate --port COM3

``check`` and ``sync`` keep working on both installation layouts:

* a **legacy** device (``/ai_dash.py`` plus ``/ai_dash_files``) is checked and
  synced exactly as before;
* a **slot** device (``/ai_dash_boot.py`` plus two slots) has its bootstrap and
  its active slot verified, and a sync installs the build into the inactive
  slot and activates it, keeping the previous one as the fallback.

``migrate`` is the one deliberate USB migration from the legacy layout to the
bootstrap and slots. It is never implied by a sync: a routine update must not
silently rewrite how the device boots, because there is no over-the-air way
back. Private settings, secrets and firmware are not touched by any action.

check/sync/migrate interrupt the device, then relaunch AI Dash.
"""

import argparse
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "daemon" / "src"))

from presto_bundle import ROOT, RECEIPT, snapshot
from presto_ai_dash import device_install as _engine
from presto_ai_dash.device_install import (  # noqa: F401  (the names tests and callers use)
    MARKER, LAUNCH, HELPERS, inspection_script, commit_script, run_remote,
    parse_hashes, record, device_lock, _operate)

# The activation journal is written by the device's own module, so a USB
# activation and an over-the-air one agree byte for byte.
sys.path.insert(0, str(ROOT / "presto"))
import activation  # noqa: E402


def operate(action, port, bundle, remote=run_remote, receipt=RECEIPT):
    return _engine.operate(action, port, bundle, remote=remote, receipt=receipt)


def layout(port, remote=run_remote):
    """Which installation layout this device has, without writing anything."""
    probe = (_engine.SLOT_LAYOUT_HELPERS +
             "\nprint('PRESTO_SYNC:' + json.dumps({"
             "'config': exists('/ai_dash_files/config.py'), "
             "'legacy': exists('/ai_dash.py'), "
             "'boot': exists('/ai_dash_boot.py')}))\n")
    with _engine.device_lock(Path(RECEIPT).with_suffix(".lock")):
        return _engine.parse_state(remote(port, ["soft-reset", "exec", probe]))


def slot_check(port, bundle):
    return _engine.slot_check_operation(port, bundle)


def slot_sync(port, bundle, maintain_bootstrap=False):
    """Install into the inactive slot and activate it, keeping the other one."""
    _different, active, journal = slot_check(port, bundle)
    stored = activation.parse_journal(json.dumps(journal)) if journal else None
    if stored is None or active not in activation.SLOT_NAMES:
        raise RuntimeError("Activation state unavailable; repair the installation over USB first")
    if not maintain_bootstrap and any(path == _engine.BOOT_ENTRY or path.startswith(_engine.BOOT_FOLDER + '/')
                                      for path in _different):
        raise RuntimeError("Bootstrap maintenance is required: sync over USB with --maintain-bootstrap")
    target = "b" if active == "a" else "a"
    next_state = dict(stored, active=target, confirmed=target, pending=None,
                      failure=None, success=None, transfer=None,
                      boots=int(stored["boots"]))
    _engine.slot_sync_operation(port, bundle, activation.encode_journal(next_state),
                                target, maintain_bootstrap=maintain_bootstrap)
    return target


def main(argv=None):
    arguments = sys.argv[1:] if argv is None else list(argv)
    if arguments[:1] in (["set-daemon"], ["set-wifi"]):
        return _engine.main(arguments[0], arguments[1:], receipt=RECEIPT)
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("action", choices=("plan", "check", "sync", "migrate"))
    parser.add_argument("--port", help="explicit USB port, e.g. COM3 or /dev/ttyACM0")
    parser.add_argument("--maintain-bootstrap", action="store_true",
                        help="explicitly maintain the stable bootstrap during a slot USB sync")
    args = parser.parse_args(argv)
    if args.maintain_bootstrap and args.action != "sync":
        parser.error("--maintain-bootstrap is only valid for sync")
    if args.action != "plan" and (not args.port or args.port in ("auto", "list")):
        parser.error("check/sync/migrate require an explicit --port; "
                     "automatic board selection is disabled")
    try:
        bundle = snapshot()
        print("Shared Presto app revision: " + bundle["revision"][:12])
        if args.action == "plan":
            for path in bundle["files"]:
                print("  " + path)
            slot = bundle.get("slot")
            boot = bundle.get("bootstrap")
            if slot is not None:
                print("Slot bundle (slot-v1, bootstrap %d): %s"
                      % (slot["min_bootstrap"], slot["revision"][:12]))
                for name in sorted(slot["files"]):
                    print("  /ai_dash_slots/{a,b}/" + name.lstrip("/"))
            if boot is not None:
                print("Bootstrap bundle (version %d):" % boot["version"])
                for name in sorted(boot["files"]):
                    print("  " + name)
            print("Private configuration, secrets and firmware main.py are excluded.")
            return 0
        if importlib.util.find_spec("mpremote") is None:
            print('Install USB tools: python -m pip install -e "daemon[device]"',
                  file=sys.stderr)
            return 2
        state = layout(args.port)
        if args.action == "migrate":
            if bundle.get("slot") is None or bundle.get("bootstrap") is None:
                print("This build has no bootstrap or slot bundle, so it cannot "
                      "migrate a device.", file=sys.stderr)
                return 2
            _engine.migrate_operation(
                args.port, bundle,
                activation.encode_journal(activation.blank_journal()))
            print("Migrated to the bootstrap and A/B slots; both slots verified. "
                  "AI Dash relaunched.")
            return 0
        if state.get("boot") and not state.get("legacy"):
            if args.action == "check":
                different, active, _journal = slot_check(args.port, bundle)
                if different:
                    print("Device differs from PSL:")
                    for path in different:
                        print("  " + path)
                    print("Run Sync Presto to install this revision.")
                    return 1
                slot = bundle.get("slot") or {}
                print("Verified: bootstrap and slot %s match %s. AI Dash relaunched."
                      % (active, str(slot.get("revision", bundle["revision"]))[:12]))
                return 0
            target = slot_sync(args.port, bundle, maintain_bootstrap=args.maintain_bootstrap)
            print("Installed into slot %s and activated it. AI Dash relaunched." % target)
            return 0
        if state.get("legacy"):
            different = operate(args.action, args.port, bundle)
            if different:
                print("Device differs from PSL:")
                for path in different:
                    print("  " + path)
                print("Run Sync Presto to install this revision.")
                return 1
            print("Verified: device and PSL app files match " + bundle["revision"][:12]
                  + ". AI Dash relaunched.")
            if bundle.get("slot") is not None:
                print("This device is still on the legacy layout. Use "
                      "'python tools/presto_device.py migrate --port <port>' to move "
                      "it to the bootstrap and A/B slots.", file=sys.stderr)
            return 0
        print("This device has neither the legacy app nor the bootstrap, so there is "
              "nothing to check or sync. Provision it with 'Set up a Presto' first.",
              file=sys.stderr)
        return 2
    except SyntaxError as exc:
        print("App syntax error: %s:%s" % (exc.filename, exc.lineno), file=sys.stderr)
        return 2
    except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
        print(str(exc) if isinstance(exc, (RuntimeError, ValueError)) else
              "USB operation failed or timed out. Check the port and retry.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
