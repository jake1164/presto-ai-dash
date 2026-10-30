"""Install or check the Presto app on a USB-connected device.

    presto-ai-dash install-presto --port COM3
    presto-ai-dash check-presto --port COM3

The app files travel inside the release wheel (``presto_app/``), so the device
always gets the version that matches this daemon. A daemon running from a
source checkout has no staged copy, so it reads the same inventory from
``tools/presto_bundle.py`` instead; there is still one list of public files.
Neither command reads or writes the device's config, secrets, provider data or
firmware launcher. The repository's ``tools/presto_device.py`` is a thin
wrapper over this module, so the checkout and the wheel share one USB engine.

First-time provisioning needs settings as well as app files, so it is a
different command: the standalone ``presto_setup.py`` the admin page serves
(see ``presto_ai_dash.device_setup``) collects Wi-Fi locally, keeps or writes
the private files and then uses the same staged, verified, rolled-back copy.
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
# The slot layout travels beside the legacy app inside the same wheel, from
# the same ``presto/`` sources. A wheel built before Part 2 has neither
# folder, and then no slot identity exists and no over-the-air update can be
# offered: that is reported honestly rather than inferred.
SLOT_APP_FOLDER = "presto_app_slot"
BOOT_APP_FOLDER = "presto_app_boot"
# The scheme of the revision this install layout produces: the aggregate of the
# current fixed-path files, launcher included. Stated explicitly so a report is
# only ever compared with an expectation of the same layout.
REVISION_SCHEME = "legacy-v1"
# The current layout has no boot manager, so it requires no bootstrap version.
MIN_BOOTSTRAP = None
# The slot layout's own identity rules (see tools/presto_bundle.py).
REVISION_SCHEME_SLOT = "slot-v1"
SLOT_MIN_BOOTSTRAP = 1
SLOT_NAMES = ("a", "b")
SLOT_ROOT = "/ai_dash_slots"
BOOT_ENTRY = "/ai_dash_boot.py"
BOOT_FOLDER = "/ai_dash_boot_files"
STATE_FOLDER = "/ai_dash_state"
SETTINGS_FOLDER = "/ai_dash_files"
SLOT_REVISION_LABEL = "revision.txt"
SLOT_REVISION_FULL = "revision.sha256"
SLOT_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_]+\.py$")

MARKER = "PRESTO_SYNC:"
LAUNCH = ("import sys, os; sys.path.insert(0, '/ai_dash_files'); "
          "os.chdir('/ai_dash_files'); from ai_dash_app import main; main()")
CONFIG_PATH = "/ai_dash_files/config.py"
CONFIG_MISSING = (
    "This Presto has no " + CONFIG_PATH + " yet. Use 'Set up a Presto' in the "
    "daemon's admin page to install and configure it the first time; that "
    "download also collects the Wi-Fi settings and the device identifier.")

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
                # Check the settings exist without opening them: installing the
                # app alone leaves a device that cannot reach the daemon, so a
                # first-time device is sent to the setup utility instead.
                preflight = HELPERS + (
                    "\nprint('PRESTO_SYNC:' + json.dumps({'config': "
                    "exists('/ai_dash_files/config.py')}))\n")
                # The preflight's soft reset stops AI Dash, so relaunch it
                # afterwards whatever the preflight finds.
                attempted = True
                state = parse_state(remote(port, ["soft-reset", "exec", preflight]))
                if not state.get("config"):
                    raise RuntimeError(CONFIG_MISSING)
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


def parse_state(output):
    """The single marker object a preflight script printed."""
    lines = [line[len(MARKER):] for line in output.splitlines() if line.startswith(MARKER)]
    if len(lines) != 1:
        raise RuntimeError("USB check did not return an unambiguous device state")
    state = json.loads(lines[0])
    if not isinstance(state, dict):
        raise RuntimeError("USB check returned an unexpected device state")
    return state


# -- the slot layout -------------------------------------------------------
#
# A device moves from the legacy single-directory install to the bootstrap and
# two slots through one deliberate USB operation. It is never part of routine
# sync: a sync that silently rewrote the boot layout would be a migration the
# user did not ask for, with no way back.

SLOT_LAYOUT_HELPERS = '''import os, sys, json, hashlib, binascii
if sys.implementation.name != 'micropython' or 'Presto' not in os.uname().machine:
    raise RuntimeError('Selected USB device is not a Pimoroni Presto')
DIRS = ['/ai_dash_slots', '/ai_dash_slots/a', '/ai_dash_slots/b',
        '/ai_dash_boot_files', '/ai_dash_state']
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
def mkdirs():
    for path in DIRS:
        try:
            os.mkdir(path)
        except OSError:
            pass
def stale(prefix, expected):
    paths = []
    try:
        names = os.listdir(prefix)
    except OSError:
        names = []
    for name in names:
        path = prefix + '/' + name
        if path not in expected and name.endswith(('.py', '.mpy')):
            paths.append(path)
    return paths
'''

# The public modules a legacy install owns. Private settings are not here:
# config.py and secrets.py stay exactly where the setup utility put them.


def layout_script(boot_hashes, slots, journal_text, retire_legacy=False, legacy_paths=()):
    """Stage, verify and activate a slot layout on the device.

    ``boot_hashes`` and each ``slots[name]`` map installed path -> SHA-256.
    Every file is uploaded beside its target as ``.presto-new`` first, so a
    broken transfer never replaces a working file. The journal is written last
    and through a temporary file, so an interrupted run leaves either the old
    install or a complete new one.
    """
    owned = [p for p in legacy_paths if p.startswith('/ai_dash_files/')
             and not any(word in p.rsplit('/', 1)[-1].lower() for word in ('config', 'secret'))]
    head = SLOT_LAYOUT_HELPERS + "\nboot = %r\nslots = %r\njournal = %r\nretire = %r\nlegacy_paths = %r\n" % (
        boot_hashes, slots, journal_text, bool(retire_legacy), owned)
    body = '''
mkdirs()
def check(expected):
    for path, wanted in expected.items():
        if digest(path + '.presto-new') != wanted:
            raise RuntimeError('Staged file verification failed: ' + path)
check(boot)
for name in slots:
    check(slots[name])
changed = []
extras = []
try:
    for group in list(slots.values()) + [boot]:
        for path in group:
            backup = path + '.presto-prev'
            if exists(backup):
                os.remove(backup)
            had_old = exists(path)
            if had_old:
                os.rename(path, backup)
            changed.append((path, had_old))
            os.rename(path + '.presto-new', path)
    for group in list(slots.values()) + [boot]:
        for path, wanted in group.items():
            if digest(path) != wanted:
                raise RuntimeError('Installed file verification failed: ' + path)
    for name in slots:
        extras.extend(stale('/ai_dash_slots/' + name, slots[name]))
except Exception:
    for path, had_old in reversed(changed):
        try:
            if exists(path):
                os.remove(path)
            if had_old:
                os.rename(path + '.presto-prev', path)
        except Exception:
            pass
    raise
for path, had_old in changed:
    backup = path + '.presto-prev'
    if exists(backup):
        os.remove(backup)
for path in extras:
    try:
        os.remove(path)
    except OSError:
        pass
try:
    # A prior check may have relaunched the app before this sync. Never move
    # the public boot epoch backwards when committing the USB activation.
    next_journal = json.loads(journal)
    try:
        with open('/ai_dash_state/journal.json', 'r') as handle:
            current_journal = json.loads(handle.read(1025))
        current_boots = current_journal.get('boots')
        if type(current_boots) is int and current_boots > next_journal['boots']:
            next_journal['boots'] = current_boots
    except OSError:
        pass
    journal = json.dumps(next_journal)
    with open('/ai_dash_state/journal.tmp', 'w') as handle:
        handle.write(journal)
    try:
        os.remove('/ai_dash_state/journal.bak')
    except OSError:
        pass
    try:
        os.rename('/ai_dash_state/journal.json', '/ai_dash_state/journal.bak')
    except OSError:
        pass
    os.rename('/ai_dash_state/journal.tmp', '/ai_dash_state/journal.json')
    if hasattr(os, 'sync'):
        os.sync()
except Exception:
    raise RuntimeError('Activation journal could not be written')
retired = []
if retire:
    # Only now, with a verified bootstrap and slots, is the old single-folder
    # app removed. Private settings and unrelated files are never touched.
    for path in (%(legacy)r, %(target)r):
        try:
            os.remove(path)
            retired.append(path)
        except OSError:
            pass
    for path in legacy_paths:
        try:
            os.remove(path)
            retired.append(path)
        except OSError:
            pass
print('PRESTO_SYNC:' + json.dumps({'installed': sorted(boot),
                                   'slots': sorted(slots), 'retired': retired}))
''' % {"legacy": "/ai_dash.py", "target": REVISION_TARGET}
    return head + body


def layout_inspection_script(boot_hashes, slot_hashes):
    """Every bootstrap and slot hash, plus the stored journal, for the host.

    It writes nothing. The host decides whether the active slot matches this
    release, because only the host knows which revision it expects.
    """
    return SLOT_LAYOUT_HELPERS + "\nboot = " + repr(boot_hashes) + \
        "\nslots = " + repr(slot_hashes) + '''
result = {'boot': {path: digest(path) for path in boot}, 'slots': {}, 'journal': None}
for name, group in slots.items():
    result['slots'][name] = {path: digest(path) for path in group}
try:
    with open('/ai_dash_state/journal.json') as handle:
        result['journal'] = handle.read(1024)
except OSError:
    pass
print('PRESTO_SYNC:' + json.dumps(result))
'''


def _slot_targets(bundle):
    """Installed path -> bytes for the bootstrap and for each slot."""
    boot = dict(bundle.get("bootstrap") or {}).get("files") or {}
    slot = dict(bundle.get("slot") or {}).get("files") or {}
    if not boot or not slot:
        raise RuntimeError(
            "This build has no Presto bootstrap or slot bundle, so it cannot "
            "migrate a device. Build a release that includes them first.")
    slots = {}
    for name in SLOT_NAMES:
        slots[name] = {SLOT_ROOT + "/" + name + "/" + target.lstrip("/"): data
                       for target, data in slot.items()}
    return boot, slots


def slot_hashes(bundle):
    """(bootstrap hashes, {slot: hashes}) for the layout this build ships."""
    boot, slots = _slot_targets(bundle)
    return ({path: hashlib.sha256(data).hexdigest() for path, data in boot.items()},
            {name: {path: hashlib.sha256(data).hexdigest() for path, data in group.items()}
             for name, group in slots.items()})


def migrate_operation(port, bundle, journal_text, remote=run_remote,
                      receipt=DEFAULT_RECEIPT):
    """One deliberate USB migration from the legacy install to A/B slots.

    Both slots receive the same verified dashboard, so the device is never
    left with one unproven slot. The private settings stay where they are, and
    the legacy app files are retired only after everything else verified.
    """
    boot, slots = _slot_targets(bundle)
    boot_hashes = {path: hashlib.sha256(data).hexdigest() for path, data in boot.items()}
    slot_hashes_map = {name: {path: hashlib.sha256(data).hexdigest()
                              for path, data in group.items()}
                       for name, group in slots.items()}
    with device_lock(Path(receipt).with_suffix(".lock")):
        record("unverified", bundle, receipt)
        attempted = False
        try:
            with tempfile.TemporaryDirectory(prefix="presto-migrate-") as temporary:
                folder = Path(temporary)
                script = folder / "migrate.py"
                script.write_text(
                    layout_script(boot_hashes, slot_hashes_map, journal_text,
                                  retire_legacy=True,
                                  legacy_paths=bundle["files"]), encoding="utf-8")
                state = parse_state(remote(port, ["soft-reset", "exec", (
                    SLOT_LAYOUT_HELPERS +
                    "\nprint('PRESTO_SYNC:' + json.dumps({'config': "
                    "exists('/ai_dash_files/config.py'), "
                    "'legacy': exists('/ai_dash.py')}))\n")]))
                if not state.get("config"):
                    raise RuntimeError(CONFIG_MISSING)
                if not state.get("legacy"):
                    raise RuntimeError(
                        "This device is not on the legacy install layout, so "
                        "there is nothing to migrate. Use the slot check instead.")
                # Prepare directories before fs cp, and continue the public
                # boot counter without opening private settings.
                preparation = SLOT_LAYOUT_HELPERS + '''
mkdirs()
try:
    counters = []
    for counter_path in ('/ai_dash_boot.txt', '/ai_dash_boot.txt.tmp'):
        if exists(counter_path):
            with open(counter_path, 'r') as handle:
                value = handle.read(12).strip()
            if not value.isdigit() or not 1 <= int(value) < 2147483647:
                raise ValueError()
            counters.append(int(value))
    if not counters:
        raise ValueError()
    boots = max(counters)
except OSError:
    raise RuntimeError('Boot counter unavailable; identity recovery requires USB setup')
except ValueError:
    raise RuntimeError('Invalid boot counter; identity recovery requires USB setup')
print('PRESTO_SYNC:' + json.dumps({'boots': boots}))
'''
                prepared = parse_state(remote(port, ["soft-reset", "exec", preparation]))
                if type(prepared.get("boots")) is not int:
                    raise RuntimeError("Migration could not preserve the boot counter")
                migration_journal = json.loads(journal_text)
                migration_journal["boots"] = prepared["boots"]
                script.write_text(layout_script(boot_hashes, slot_hashes_map,
                    json.dumps(migration_journal), retire_legacy=True,
                    legacy_paths=bundle["files"]), encoding="utf-8")
                arguments = ["soft-reset"]
                index = 0
                for target, data in list(boot.items()) + list(slots["a"].items()) + \
                        list(slots["b"].items()):
                    local = folder / ("f%d.py" % index)
                    local.write_bytes(data)
                    index += 1
                    arguments += ["fs", "cp", str(local), ":" + target + ".presto-new", "+"]
                arguments += ["run", str(script)]
                attempted = True
                remote(port, arguments)
            record("matched", bundle, receipt)
        finally:
            if attempted:
                pending_failure = sys.exc_info()[0] is not None
                try:
                    # A failed migration can still be on the legacy layout.
                    relaunch = ("import os; exec(" + repr(LAUNCH) +
                        ") if 'ai_dash.py' in os.listdir('/') else exec(" + repr(BOOT_LAUNCH) + ")")
                    remote(port, ["soft-reset", "exec", "--no-follow", relaunch])
                except (RuntimeError, OSError, subprocess.TimeoutExpired):
                    if not pending_failure:
                        raise RuntimeError(
                            "Could not relaunch AI Dash. Reconnect USB and run "
                            "Check Presto sync again.") from None
                    print("Could not relaunch AI Dash. Reconnect USB and run "
                          "Check Presto sync again.", file=sys.stderr)


# The bootstrap is the carousel entry, so relaunching it is relaunching AI Dash.
BOOT_LAUNCH = ("import sys, os; sys.path.insert(0, '/ai_dash_boot_files'); "
               "os.chdir('/ai_dash_boot_files'); import boot; boot.main()")


# A running slot app has the bootstrap's 8-second hardware watchdog armed, and
# an app interrupted over USB no longer feeds it, so a long USB session could be
# cut short by a reset (observed on COM3 during a slot sync). RP2 cannot disarm
# a watchdog; a hard reset clears it, and the stock launcher arms none.
RESET_TO_LAUNCHER = "import machine; machine.reset()"
READY_PROBE = "print('PRESTO_READY')"


def reset_to_launcher(port, remote=run_remote, attempts=20, pause=1.0, sleep=None):
    """Hard-reset the board to the stock launcher and wait for USB to return."""
    import time
    sleep = sleep or time.sleep
    try:
        remote(port, ["exec", "--no-follow", RESET_TO_LAUNCHER])
    except (RuntimeError, OSError, subprocess.TimeoutExpired):
        pass  # The port can vanish before mpremote has finished.
    for _ in range(attempts):
        try:
            remote(port, ["exec", READY_PROBE])
            return
        except (RuntimeError, OSError, subprocess.TimeoutExpired):
            sleep(pause)
    raise RuntimeError("The Presto did not return after a reset. Reconnect USB and "
                       "run Check Presto sync again.")


def slot_sync_operation(port, bundle, journal_text, slot, remote=run_remote,
                        receipt=DEFAULT_RECEIPT, maintain_bootstrap=False):
    """Install this build into one slot over USB and activate it, confirmed.

    A USB install is verified by hash over the cable, so it does not need the
    daemon's two-poll confirmation; the journal records it as confirmed. The
    other slot keeps the app it had, which is the fallback if this one ever
    fails to start.
    """
    boot, slots = _slot_targets(bundle)
    # Bootstrap replacement is an explicit USB maintenance action, never OTA
    # and never implied by a routine app sync.
    if not maintain_bootstrap:
        boot = {}
    boot_hashes = {path: hashlib.sha256(data).hexdigest() for path, data in boot.items()}
    group = slots[slot]
    hashes = {path: hashlib.sha256(data).hexdigest() for path, data in group.items()}
    with device_lock(Path(receipt).with_suffix(".lock")):
        record("unverified", bundle, receipt)
        attempted = False
        try:
            with tempfile.TemporaryDirectory(prefix="presto-slot-") as temporary:
                folder = Path(temporary)
                script = folder / "activate.py"
                script.write_text(layout_script(boot_hashes, {slot: hashes}, journal_text),
                                  encoding="utf-8")
                arguments = ["soft-reset"]
                for index, (target, data) in enumerate(list(boot.items()) + list(group.items())):
                    local = folder / (str(index) + ".py")
                    local.write_bytes(data)
                    arguments += ["fs", "cp", str(local), ":" + target + ".presto-new", "+"]
                arguments += ["run", str(script)]
                attempted = True
                reset_to_launcher(port, remote)
                remote(port, arguments)
            record("matched", bundle, receipt)
        finally:
            if attempted:
                pending_failure = sys.exc_info()[0] is not None
                try:
                    remote(port, ["soft-reset", "exec", "--no-follow", BOOT_LAUNCH])
                except (RuntimeError, OSError, subprocess.TimeoutExpired):
                    if not pending_failure:
                        raise RuntimeError(
                            "Could not relaunch AI Dash. Reconnect USB and run "
                            "Check Presto sync again.") from None
                    print("Could not relaunch AI Dash. Reconnect USB and run "
                          "Check Presto sync again.", file=sys.stderr)


def slot_check_operation(port, bundle, remote=run_remote, receipt=DEFAULT_RECEIPT):
    """Verify the bootstrap and the active slot by hash; write nothing.

    Returns ``(different, active, journal)``: the paths that do not match this
    release, the active slot the journal names, and the parsed journal (or
    None when it could not be read).
    """
    boot_hashes, slot_hashes_map = slot_hashes(bundle)
    with device_lock(Path(receipt).with_suffix(".lock")):
        record("unverified", bundle, receipt)
        attempted = True
        try:
            with tempfile.TemporaryDirectory(prefix="presto-slot-check-") as temporary:
                script = Path(temporary) / "inspect.py"
                script.write_text(layout_inspection_script(boot_hashes, slot_hashes_map),
                                  encoding="utf-8")
                reset_to_launcher(port, remote)
                output = remote(port, ["soft-reset", "run", str(script)])
            actual = parse_state(output)
            different = []
            for path, wanted in boot_hashes.items():
                if actual.get("boot", {}).get(path) != wanted:
                    different.append(path)
            journal = None
            raw = actual.get("journal")
            if isinstance(raw, str):
                try:
                    candidate = json.loads(raw)
                except (ValueError, TypeError):
                    candidate = None
                if isinstance(candidate, dict):
                    journal = candidate
            active = journal.get("active") if journal else None
            if active not in SLOT_NAMES:
                # Without a readable journal nothing can be called verified.
                active = None
                different.append("/ai_dash_state/journal.json")
            for name, hashes in slot_hashes_map.items():
                if name != active:
                    continue
                for path, wanted in hashes.items():
                    if actual.get("slots", {}).get(name, {}).get(path) != wanted:
                        different.append(path)
            record("different" if different else "matched", bundle, receipt)
            return different, active, journal
        finally:
            if attempted:
                pending_failure = sys.exc_info()[0] is not None
                try:
                    remote(port, ["soft-reset", "exec", "--no-follow", BOOT_LAUNCH])
                except (RuntimeError, OSError, subprocess.TimeoutExpired):
                    if not pending_failure:
                        print("Could not relaunch AI Dash. Reconnect USB and run "
                              "Check Presto sync again.", file=sys.stderr)



class NoBundledApp(RuntimeError):
    """This install has no Presto app files (it was not built from a release)."""


def _package_folder(name):
    return Path(str(resources.files("presto_ai_dash").joinpath(name)))


def _read_folder(root, suffix_filter=(".py", ".txt", ".sha256")):
    """Every plain file under a staged folder, by relative path."""
    files = {}
    for path in sorted(Path(root).rglob("*")):
        if "__pycache__" in path.parts or path.suffix not in suffix_filter:
            continue
        if path.is_file() and not path.is_symlink():
            files["/" + path.relative_to(root).as_posix()] = path.read_bytes()
    return files


def slot_revision(hashes):
    """The ``slot-v1`` revision of a {slot-relative name: sha256} mapping."""
    encoded = json.dumps(hashes, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def bundled_slot(folder=None):
    """The ``slot-v1`` dashboard the wheel carries, or None.

    Validation is strict: every name is a canonical module name, the two
    generated revision files agree with the recomputed hash, and no private
    file is present. A bundle that cannot prove itself is treated as absent,
    because an unverifiable target must never become an update command.
    """
    root = Path(folder) if folder is not None else _package_folder(SLOT_APP_FOLDER)
    if not root.is_dir():
        return None
    files = _read_folder(root)
    names = {target.lstrip("/") for target in files}
    if len(names) != len(files):
        return None
    for name in names:
        if name in (SLOT_REVISION_LABEL, SLOT_REVISION_FULL):
            continue
        if not SLOT_NAME_PATTERN.match(name):
            return None
        if "secret" in name.lower() or "config" in name.lower():
            return None
    for name, data in files.items():
        if name.endswith(".py"):
            compile(data, name, "exec")
    code = {name: hashlib.sha256(data).hexdigest() for name, data in files.items()
            if name not in ("/" + SLOT_REVISION_LABEL, "/" + SLOT_REVISION_FULL)}
    revision = slot_revision({name.lstrip("/"): sha for name, sha in code.items()})
    if files.get("/" + SLOT_REVISION_FULL) != (revision + "\n").encode("ascii"):
        return None
    if files.get("/" + SLOT_REVISION_LABEL) != (revision[:REVISION_CHARS] + "\n").encode("ascii"):
        return None
    return {"scheme": REVISION_SCHEME_SLOT, "revision": revision,
            "min_bootstrap": SLOT_MIN_BOOTSTRAP, "files": files,
            "hashes": {name: hashlib.sha256(data).hexdigest()
                       for name, data in files.items()}}


def bundled_bootstrap(folder=None):
    """The stable bootstrap files the wheel carries, or None."""
    root = Path(folder) if folder is not None else _package_folder(BOOT_APP_FOLDER)
    if not root.is_dir():
        return None
    files = _read_folder(root)
    if BOOT_ENTRY not in files or BOOT_FOLDER + "/boot.py" not in files:
        return None
    if BOOT_FOLDER + "/activation.py" not in files:
        return None
    for name, data in files.items():
        if name.endswith(".py"):
            compile(data, name, "exec")
    return {"version": 1, "files": files,
            "hashes": {name: hashlib.sha256(data).hexdigest()
                       for name, data in files.items()}}


def bundled_snapshot(folder=None):
    """The app files shipped in this package, in the shape the USB engine takes.

    The revision is computed exactly as ``tools/presto_bundle.snapshot`` does it,
    so a checkout and a wheel built from it report the same revision. ``folder``
    is for tests; normally the files come from the package.
    """
    root = Path(folder) if folder is not None else _package_folder(APP_FOLDER)
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
    # The same wheel may also carry the slot layout. A wheel that does not is
    # still a valid legacy release; it simply cannot offer an over-the-air
    # update, which the lifecycle coordinator then reports honestly.
    slot_root = root.parent / SLOT_APP_FOLDER
    boot_root = root.parent / BOOT_APP_FOLDER
    return {"revision": code_revision, "scheme": REVISION_SCHEME,
            "min_bootstrap": MIN_BOOTSTRAP, "hashes": hashes, "files": files,
            "slot": bundled_slot(slot_root) if slot_root.is_dir() else None,
            "bootstrap": bundled_bootstrap(boot_root) if boot_root.is_dir() else None}


def checkout_root(start=None):
    """The repository root of a source checkout, or None.

    A daemon started from ``daemon/src`` (an editable install, or the tests)
    has no ``presto_app/`` folder, because that is staged only at release
    time. The same public-file inventory in ``tools/presto_bundle.py`` still
    describes the app beside it, so a developer's admin page can serve the
    build that is actually running. A PyPI install has no such folder and
    says so.
    """
    here = Path(start or __file__).resolve()
    for candidate in here.parents:
        if ((candidate / "tools" / "presto_bundle.py").is_file() and
                (candidate / "presto" / "main.py").is_file()):
            return candidate
    return None


def source_snapshot(root=None):
    """The bundle of a checkout, through the one authoritative inventory."""
    root = Path(root) if root is not None else checkout_root()
    if root is None:
        raise NoBundledApp(
            "This install does not include the Presto app files. Install a release "
            "wheel from GitHub, or use tools/presto_device.py from a checkout.")
    spec = importlib.util.spec_from_file_location(
        "_presto_bundle_checkout", root / "tools" / "presto_bundle.py")
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise NoBundledApp("The checkout's Presto bundle inventory could not be read.")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.snapshot(root)


def app_snapshot(folder=None, root=None):
    """The app files this daemon build should install on a device.

    Release builds use the bundle inside the wheel (``presto_app/``). A source
    checkout falls back to ``tools/presto_bundle.py`` plus ``presto/`` so the
    admin page and the USB checks describe the code that is running.
    """
    checkout = Path(root) if root is not None else checkout_root()
    if folder is None and checkout is not None and (root is not None or
            Path(__file__).resolve().parent == checkout / "daemon" / "src" / "presto_ai_dash"):
        # A build may leave staged package-data beside editable source. The
        # checkout still serves its current shared app, not yesterday's copy.
        return source_snapshot(checkout)
    try:
        return bundled_snapshot(folder)
    except NoBundledApp:
        return source_snapshot(root)


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
              '"presto-ai-dash[device]"  (or: pip install "presto-ai-dash[device]")',
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
