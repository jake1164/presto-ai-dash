"""Check or sync the exact app files used by PSL, through an explicit USB command.

    python tools/presto_device.py plan
    python tools/presto_device.py check --port COM3
    python tools/presto_device.py sync --port COM3

check/sync interrupt the device, then relaunch AI Dash. Neither command reads
or writes device config, secrets, provider data, or the firmware launcher.
"""

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile

from presto_bundle import ROOT, RECEIPT, snapshot

MARKER = "PRESTO_SYNC:"
LAUNCH = ("import sys, os; sys.path.insert(0, '/ai_dash_files'); "
          "os.chdir('/ai_dash_files'); from ai_dash_app import main; main()")

HELPERS = '''import os, sys, json, hashlib, binascii
if sys.implementation.name != 'micropython' or 'Presto' not in os.uname().machine:
    raise RuntimeError('Selected USB device is not a Pimoroni Presto')
def exists(path):
    try:
        os.stat(path)
        return True
    except OSError:
        return False
def digest(path):
    try:
        stream = open(path, 'rb')
    except OSError:
        return None
    h = hashlib.sha256()
    with stream:
        while True:
            chunk = stream.read(1024)
            if not chunk:
                break
            h.update(chunk)
    return binascii.hexlify(h.digest()).decode()
def app_paths(expected):
    paths = list(expected)
    try:
        names = os.listdir('/ai_dash_files')
    except OSError:
        names = []
    for name in names:
        lower = name.lower()
        if (lower.endswith(('.py', '.mpy')) and
                not lower.startswith(('config.', 'secrets.'))):
            path = '/ai_dash_files/' + name
            if path not in paths:
                paths.append(path)
    return paths
'''


def inspection_script(hashes):
    return HELPERS + "\npaths = " + repr(list(hashes)) + "\n" + (
        "print('PRESTO_SYNC:' + json.dumps({p: digest(p) if p in paths else None for p in app_paths(paths)}))\n")


def commit_script(hashes):
    # Upload and verify every staged file before replacing any installed file.
    # Backups restore the previous files if a normal write/rename/verify fails.
    return HELPERS + "\nexpected = " + repr(hashes) + '''
for path, wanted in expected.items():
    if digest(path + '.presto-new') != wanted:
        raise RuntimeError('Staged app verification failed')
installed = []
extras = [p for p in app_paths(expected) if p not in expected]
try:
    for path in list(expected) + extras:
        backup = path + '.presto-prev'
        if exists(backup):
            os.remove(backup)
        had_old = exists(path)
        if had_old:
            os.rename(path, backup)
        installed.append((path, had_old))
        if path in expected:
            os.rename(path + '.presto-new', path)
    actual = {p: digest(p) for p in app_paths(expected)}
    if actual != expected:
        raise RuntimeError('Installed app verification failed')
except Exception:
    for path, had_old in reversed(installed):
        if exists(path):
            os.remove(path)
        if had_old:
            os.rename(path + '.presto-prev', path)
    raise
for path in expected:
    backup = path + '.presto-prev'
    if exists(backup):
        os.remove(backup)
print('PRESTO_SYNC:' + json.dumps(actual))
'''


def run_remote(port, arguments):
    result = subprocess.run([sys.executable, "-m", "mpremote", "connect", port, *arguments],
                            capture_output=True, text=True, timeout=120, cwd=ROOT)
    if result.returncode:
        # Boot output and remote tracebacks are not safe diagnostics to echo.
        raise RuntimeError("USB command failed. Check the selected port, close other serial tools, "
                           "and confirm this is a configured Pimoroni Presto. Rerun sync if interrupted.")
    return result.stdout


def parse_hashes(output, expected):
    lines = [line[len(MARKER):] for line in output.splitlines() if line.startswith(MARKER)]
    if len(lines) != 1:
        raise RuntimeError("USB verification did not return an unambiguous file inventory")
    actual = json.loads(lines[0])
    if not isinstance(actual, dict) or not set(expected).issubset(actual):
        raise RuntimeError("USB verification returned an unexpected file inventory")
    for extra in set(actual) - set(expected):
        if (not re.fullmatch(r"/ai_dash_files/[A-Za-z_][A-Za-z0-9_]*\.(py|mpy)", extra) or
                extra.rsplit("/", 1)[1].lower().startswith(("config.", "secrets."))):
            raise RuntimeError("USB verification returned an unsafe extra app path")
    if any(value is not None and (not isinstance(value, str) or len(value) != 64 or
           any(char not in '0123456789abcdef' for char in value)) for value in actual.values()):
        raise RuntimeError("USB verification returned an invalid file hash")
    return actual


def record(status, bundle, receipt=RECEIPT):
    path = Path(receipt)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps({"status": status, "revision": bundle["revision"],
        "checked_at": datetime.now(timezone.utc).isoformat()}, indent=2), encoding="utf-8")
    temporary.replace(path)


@contextmanager
def device_lock(path):
    """Only one USB operation; OS releases this lock if the process crashes."""
    import os
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as lock:
        lock.seek(0, 2)
        if not lock.tell():
            lock.write(b"0"); lock.flush()
        lock.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise RuntimeError("Another Presto check/sync is running; wait for it to finish") from None
        try:
            yield
        finally:
            lock.seek(0)
            if os.name == "nt":
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def operate(action, port, bundle, remote=run_remote, receipt=RECEIPT):
    with device_lock(Path(receipt).with_suffix(".lock")):
        return _operate(action, port, bundle, remote, receipt)


def _operate(action, port, bundle, remote, receipt):
    record("unverified", bundle, receipt)
    attempted = False
    try:
        with tempfile.TemporaryDirectory(prefix="presto-sync-") as temporary:
            folder = Path(temporary)
            script = folder / "verify.py"
            script.write_text(inspection_script(bundle["hashes"]) if action == "check" else
                              commit_script(bundle["hashes"]), encoding="utf-8")
            arguments = ["soft-reset"]
            if action == "sync":
                # Check configuration exists without opening it; never provision secrets here.
                preflight = HELPERS + "\nos.stat('/ai_dash_files/config.py')\n"
                arguments += ["exec", preflight]
                for index, (target, data) in enumerate(bundle["files"].items()):
                    local = folder / (str(index) + ".py")
                    local.write_bytes(data)
                    arguments += ["fs", "cp", str(local), ":" + target + ".presto-new", "+"]
            arguments += ["run", str(script)]
            attempted = True
            actual = parse_hashes(remote(port, arguments), bundle["hashes"])
            different = [path for path, wanted in bundle["hashes"].items() if actual[path] != wanted]
            different += sorted(set(actual) - set(bundle["hashes"]))
            record("different" if different else "matched", bundle, receipt)
            return different
    finally:
        if attempted:
            pending_failure = sys.exc_info()[0] is not None
            # Release USB with the real app running, even following a failed check/upload.
            try:
                remote(port, ["soft-reset", "exec", "--no-follow", LAUNCH])
            except (RuntimeError, OSError, subprocess.TimeoutExpired):
                if not pending_failure:
                    raise RuntimeError("Could not relaunch AI Dash. Reconnect USB and use Sync Presto again.") from None
                print("Could not relaunch AI Dash. Reconnect USB and use Sync Presto again.", file=sys.stderr)


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
