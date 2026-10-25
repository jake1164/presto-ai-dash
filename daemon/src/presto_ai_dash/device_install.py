"""Install or check the Presto app on a USB-connected device.

    presto-ai-dash install-presto --port COM3
    presto-ai-dash check-presto --port COM3

The app files travel inside the release wheel (``presto_app/``), so the device
always gets the version that matches this daemon. Neither command reads or
writes the device's config, secrets, provider data or firmware launcher. The
repository's ``tools/presto_device.py`` is a thin wrapper over this module, so
the checkout and the wheel share one USB engine.
"""

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
from importlib import resources
import importlib.util
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile

# A dated USB observation, outside any checkout. Only the last result is kept.
DEFAULT_RECEIPT = Path.home() / ".presto-ai-dash" / "device-sync.json"
REVISION_TARGET = "/ai_dash_files/revision.txt"
REVISION_CHARS = 12
APP_FOLDER = "presto_app"

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
                            capture_output=True, text=True, timeout=120)
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


def record(status, bundle, receipt=DEFAULT_RECEIPT):
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


def operate(action, port, bundle, remote=run_remote, receipt=DEFAULT_RECEIPT):
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



class NoBundledApp(RuntimeError):
    """This install has no Presto app files (it was not built from a release)."""


def bundled_snapshot(folder=None):
    """The app files shipped in this package, in the shape the USB engine takes.

    The revision is computed exactly as ``tools/presto_bundle.snapshot`` does it,
    so a checkout and a wheel built from it report the same revision. ``folder``
    is for tests; normally the files come from the package.
    """
    root = Path(folder) if folder is not None else Path(
        str(resources.files("presto_ai_dash").joinpath(APP_FOLDER)))
    if not root.is_dir():
        raise NoBundledApp(
            "This install does not include the Presto app files. Install a release "
            "wheel from GitHub, or use tools/presto_device.py from a checkout.")
    files = {}
    for path in sorted(root.rglob("*")):
        # pip byte-compiles installed .py files into __pycache__: not part of the app.
        if "__pycache__" in path.parts or path.suffix not in (".py", ".txt"):
            continue
        if path.is_file() and not path.is_symlink():
            files["/" + path.relative_to(root).as_posix()] = path.read_bytes()
    if REVISION_TARGET not in files or "/ai_dash.py" not in files:
        raise NoBundledApp("The bundled Presto app files are incomplete; reinstall the wheel.")
    hashes = {target: hashlib.sha256(data).hexdigest() for target, data in files.items()}
    for target, data in files.items():
        if target.endswith(".py"):
            compile(data, target, "exec")
    code_hashes = {t: h for t, h in hashes.items() if t != REVISION_TARGET}
    encoded = json.dumps(code_hashes, sort_keys=True, separators=(",", ":")).encode()
    code_revision = hashlib.sha256(encoded).hexdigest()
    if files[REVISION_TARGET] != (code_revision[:REVISION_CHARS] + "\n").encode("ascii"):
        raise NoBundledApp("The bundled Presto app files do not match their revision; reinstall the wheel.")
    return {"revision": code_revision, "hashes": hashes, "files": files}


def run(action, port, snapshot, receipt=DEFAULT_RECEIPT, out=None, err=None):
    """Check or sync; prints the result and returns the exit code."""
    out = out or sys.stdout
    err = err or sys.stderr
    if not port or port in ("auto", "list"):
        print("An explicit --port is required (for example COM3 or /dev/ttyACM0); "
              "automatic board selection is disabled.", file=err)
        return 2
    if importlib.util.find_spec("mpremote") is None:
        print('USB tools are missing. Install them with: pipx install --force '
              '"presto-ai-dash-daemon[device]"  (or: pip install "presto-ai-dash-daemon[device]")',
              file=err)
        return 2
    try:
        print("Presto app revision: " + snapshot["revision"][:REVISION_CHARS], file=out)
        different = operate(action, port, snapshot, receipt=receipt)
        if different:
            print("Device differs from this release:", file=out)
            for path in different:
                print("  " + path, file=out)
            print("Run install-presto to install this revision.", file=out)
            return 1
        print("Verified: the device matches " + snapshot["revision"][:REVISION_CHARS] +
              ". AI Dash relaunched.", file=out)
        return 0
    except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
        print(str(exc) if isinstance(exc, (RuntimeError, ValueError)) else
              "USB operation failed or timed out. Check the port and retry.", file=err)
        return 2


def main(command, argv=None):
    """Entry point for ``presto-ai-dash install-presto`` and ``check-presto``."""
    parser = argparse.ArgumentParser(
        prog="presto-ai-dash " + command,
        description="Copy the bundled Presto app to a USB-connected Presto." if command == "install-presto"
        else "Compare the Presto's app files with this release (writes nothing to the device).")
    parser.add_argument("--port", help="explicit USB port, e.g. COM3 or /dev/ttyACM0")
    args = parser.parse_args(argv)
    try:
        snapshot = bundled_snapshot()
    except NoBundledApp as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except SyntaxError as exc:
        print("Bundled app syntax error: %s:%s" % (exc.filename, exc.lineno), file=sys.stderr)
        return 2
    return run("sync" if command == "install-presto" else "check", args.port, snapshot)
