"""Install or check the Presto app on a USB-connected device.

    presto-ai-dash install-presto --port COM3
    presto-ai-dash check-presto --port COM3
    presto-ai-dash set-daemon --port COM3 --daemon-host 192.168.1.50
    presto-ai-dash set-wifi --port COM3 --ssid MyNetwork

The app files travel inside the release wheel (``presto_app/``), so the device
always gets the version that matches this daemon. A daemon running from a
source checkout has no staged copy, so it reads the same inventory from
``tools/presto_bundle.py`` instead; there is still one list of public files.
Neither of those two reads or writes the device's config, secrets, provider data
or firmware launcher; ``set-daemon`` changes only the daemon address (and, when
asked, the device token) in the config, and puts it back if the Presto cannot
reach the new daemon; ``set-wifi`` changes only the Wi-Fi name and password,
and puts them back if the Presto cannot join the new network. The repository's ``tools/presto_device.py`` is a thin
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
import ipaddress
import json
import os
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



# -- changing the settings of a provisioned Presto over USB -------------------
#
# Moving the daemon (a new server, a new address, a test daemon on a laptop) or
# changing the Wi-Fi network each changes two or three lines of one private
# settings file and nothing else. The device identity, the other file, the app
# files or slots and the activation journal stay exactly as they are, on either
# layout. A secret is never printed. A change the device cannot use is put
# back, so a typing mistake cannot strand a Presto that is out of reach of the
# keyboard.

# Starts AI Dash whichever layout the device has: the bootstrap when it is
# installed, otherwise the legacy app.
LAUNCH_AUTO = (
    "import sys, os\n"
    "try:\n"
    "    os.stat('/ai_dash_boot.py')\n"
    "    booted = True\n"
    "except OSError:\n"
    "    booted = False\n"
    "if booted:\n"
    "    sys.path.insert(0, '/ai_dash_boot_files'); os.chdir('/ai_dash_boot_files')\n"
    "    import boot; boot.main()\n"
    "else:\n"
    "    sys.path.insert(0, '/ai_dash_files'); os.chdir('/ai_dash_files')\n"
    "    from ai_dash_app import main; main()\n")

SETTINGS_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
WIFI_PATH = "/secrets.py"
WIFI_HEADER = ("# Presto AI Dash Wi-Fi credentials.\n"
               "# Private: never commit or share this file. Use a 2.4 GHz network.\n")
DAEMON_LAYOUT_MESSAGE = (
    "The settings file does not have exactly one DAEMON_HOST, DAEMON_PORT and (when the "
    "token is changed) SHARED_TOKEN line, so it was not changed. Use 'Set up a Presto' in "
    "the daemon's admin page to write the settings again.")
WIFI_LAYOUT_MESSAGE = (
    "/secrets.py does not have exactly one WIFI_SSID and one WIFI_PASSWORD line, so it was "
    "not changed. Fix it by hand, or delete it from the Presto: a missing file is created.")
DAEMON_REACH_MESSAGES = {
    "unauthorised": "The Presto reached that address, but the daemon there refused its "
                    "device token. Run the command again with --token-env set to that "
                    "daemon's shared token.",
    "wifi": "The Presto could not join the Wi-Fi network from its /secrets.py, so it could "
            "not look for the daemon. A weak signal can cause this: run the command again.",
}
DAEMON_REACH_FALLBACK = (
    "The Presto could not reach a daemon at that address and port. Check that the daemon "
    "is running, that its device port is the one you gave (8787 by default), and that "
    "this address is reachable from the Presto's Wi-Fi network.")
DAEMON_HTTP_MESSAGE = (
    "The Presto reached a server at that address, but it did not answer as the daemon. "
    "Check that the port is the daemon's device port (8787 by default).")
WIFI_JOIN_MESSAGE = (
    "The Presto could not join that Wi-Fi network. Check the network name and the "
    "password, and that the network has a 2.4 GHz band (the Presto has no 5 GHz radio). "
    "A weak signal can also cause this, so move the Presto closer and try again.")
UNSAFE_CHANGE_MESSAGE = (
    "The settings file could not be changed safely. Nothing was replaced; reconnect the "
    "Presto and run this command again.")

_DAEMON_PREAMBLE = r'''
STAGE = TARGET + '.presto-new'
PREV = TARGET + '.presto-prev'
def say(result, **more):
    more['result'] = result
    print('PRESTO_SYNC:' + json.dumps(more))
'''

_DAEMON_APPLY = r'''
def apply():
    # An earlier run that was cut short leaves the settings it replaced in PREV.
    # A finished run always removes it, so PREV is the authoritative original.
    if exists(PREV):
        if exists(TARGET):
            os.remove(TARGET)
        os.rename(PREV, TARGET)
    if exists(STAGE):
        os.remove(STAGE)
    if exists(TARGET):
        before = digest(TARGET)
        with open(TARGET, 'rb') as stream:
            lines = stream.read().decode().split('\n')
        found = {}
        for number, line in enumerate(lines):
            bare = line.rstrip('\r')
            for name in WANT:
                if bare.startswith(name) and bare[len(name):].lstrip().startswith('='):
                    found.setdefault(name, []).append(number)
        for name in WANT:
            if len(found.get(name, ())) != 1:
                return say('layout', before=before)
        for name in WANT:
            number = found[name][0]
            tail = '\r' if lines[number].endswith('\r') else ''
            lines[number] = name + ' = ' + repr(WANT[name]) + tail
        data = '\n'.join(lines).encode()
    elif CREATE:
        before = None
        data = (HEADER + ''.join([name + ' = ' + repr(WANT[name]) + '\n' for name in ORDER])).encode()
    else:
        return say('no_config')
    space = {}
    try:
        exec(data.decode(), space)
    except Exception:
        return say('invalid', before=before)
    for name in WANT:
        if space.get(name) != WANT[name]:
            return say('invalid', before=before)
    hasher = hashlib.sha256()
    hasher.update(data)
    wanted = binascii.hexlify(hasher.digest()).decode()
    with open(STAGE, 'wb') as stream:
        stream.write(data)
    if digest(STAGE) != wanted:
        os.remove(STAGE)
        return say('write', before=before)
    if before is not None:
        os.rename(TARGET, PREV)
    os.rename(STAGE, TARGET)
    if digest(TARGET) != wanted:
        os.remove(TARGET)
        if before is not None:
            os.rename(PREV, TARGET)
        return say('write', before=before)
    return say('changed', before=before)
apply()
'''

# Joins the network, then (only if it joined) reads the daemon's status with the
# token the device holds. The two outcomes are reported separately: a network
# change must be kept when the join works even if the daemon is elsewhere.
_DAEMON_REACH = r'''
import sys, json
sys.path.insert(0, '/ai_dash_files')
code = 'error'
joined = False
try:
    import secrets, network, time, socket
    wlan = network.WLAN(network.STA_IF)
    # A weak signal sometimes leaves the radio linked but without an address, or
    # not linked at all; switching the radio off and on between tries clears it.
    for attempt in range(4):
        if attempt:
            wlan.disconnect()
            wlan.active(False)
            time.sleep(0.5)
        if not wlan.active():
            wlan.active(True)
        if wlan.isconnected():
            break
        wlan.connect(secrets.WIFI_SSID, secrets.WIFI_PASSWORD)
        for _ in range(48):
            if wlan.isconnected():
                break
            time.sleep(0.25)
        if wlan.isconnected():
            break
    joined = wlan.isconnected()
    if not joined:
        code = 'wifi'
    else:
        import config
        link = socket.socket()
        try:
            link.settimeout(8)
            link.connect(socket.getaddrinfo(config.DAEMON_HOST, int(config.DAEMON_PORT))[0][-1])
            link.send(b'GET /api/v1/status?cards=lazy HTTP/1.0\r\nHost: presto\r\n'
                      b'Authorization: Bearer ' + config.SHARED_TOKEN.encode() + b'\r\n\r\n')
            head = b''
            while b'\r\n' not in head and len(head) < 128:
                chunk = link.recv(64)
                if not chunk:
                    break
                head += chunk
            status = int(head.split(b' ')[1]) if b'\r\n' in head else 0
            if status == 200:
                code = 'ok'
            elif status == 401:
                code = 'unauthorised'
            else:
                code = 'http_%d' % status
        finally:
            link.close()
except Exception:
    code = 'error'
print('PRESTO_SYNC:' + json.dumps({'result': code, 'joined': joined}))
'''

_DAEMON_SETTLE = r'''
def settle():
    if RESTORE:
        if BEFORE is None:
            # The file did not exist before this run created it.
            if exists(TARGET):
                os.remove(TARGET)
            if exists(PREV):
                os.remove(PREV)
            return say('restored')
        if not exists(PREV):
            return say('restore_failed')
        if exists(TARGET):
            os.remove(TARGET)
        os.rename(PREV, TARGET)
        if digest(TARGET) != BEFORE:
            return say('restore_failed')
        return say('restored')
    if exists(PREV):
        os.remove(PREV)
    return say('done')
settle()
'''


def check_daemon_address(host, port):
    """The normalised IPv4 address, or a ValueError whose text is safe to print."""
    try:
        address = ipaddress.IPv4Address(host)
    except ValueError:
        raise ValueError(
            "--daemon-host must be the daemon computer's IPv4 address, such as 192.168.1.50. "
            "A name can freeze the Presto's touch screen while it is looked up.") from None
    if address.is_loopback or address.is_unspecified or address.is_multicast or address.is_reserved:
        raise ValueError(
            "--daemon-host must be the daemon computer's address on your network. localhost "
            "would mean the Presto itself.")
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("--daemon-port must be a number from 1 to 65535.")
    return str(address)


def _secret_from_environment(name, option, environ):
    environ = os.environ if environ is None else environ
    if not isinstance(name, str) or not SETTINGS_ENV_NAME.match(name):
        raise ValueError("%s must be the NAME of an environment variable." % option)
    value = environ.get(name)
    if not value:
        raise ValueError("The environment variable %s is not set." % name)
    return value


def token_from_environment(name, environ=None):
    """The device token named by an environment variable, never from the command line."""
    value = _secret_from_environment(name, "--token-env", environ)
    if not value.isascii() or any(char.isspace() for char in value) or not value.isprintable():
        raise ValueError("The device token must be printable ASCII without whitespace.")
    from .config import ConfigError, check_token_strength
    try:
        check_token_strength("The device token", value)
    except ConfigError as exc:
        raise ValueError(str(exc)) from None
    return value


def password_from_environment(name, environ=None):
    """The Wi-Fi password named by an environment variable."""
    return _secret_from_environment(name, "--password-env", environ)


def check_wifi_credentials(ssid, password):
    """Refuse values a Wi-Fi network cannot have; the text never repeats the password."""
    def printable(text):
        return all(" " <= char <= "~" for char in text)
    if not isinstance(ssid, str) or not 1 <= len(ssid) <= 32 or not printable(ssid):
        raise ValueError("--ssid must be 1 to 32 printable ASCII characters.")
    if not isinstance(password, str) or not printable(password) or \
            (password and not 8 <= len(password) <= 63):
        raise ValueError("The Wi-Fi password must be 8 to 63 printable ASCII characters, "
                         "or empty for an open network.")


def _daemon_script(body, **names):
    """A device script: the shared helpers, literal inputs, then the body."""
    literals = "".join("%s = %r\n" % item for item in sorted(names.items()))
    return HELPERS + literals + _DAEMON_PREAMBLE + body


def _run_daemon_script(remote, port, folder, name, text):
    script = Path(folder) / (name + ".py")
    script.write_bytes(text.encode("utf-8"))
    return parse_state(remote(port, ["soft-reset", "run", str(script)]))


def _reach_message(code):
    if code in DAEMON_REACH_MESSAGES:
        return DAEMON_REACH_MESSAGES[code]
    if isinstance(code, str) and code.startswith("http_"):
        return DAEMON_HTTP_MESSAGE
    return DAEMON_REACH_FALLBACK


def _settings_session(port, target, want, order, header, create, verify, keep, failure,
                      missing, layout_message, remote, receipt):
    """Rewrite named lines of one settings file, check it works, else put it back.

    ``keep(reach)`` says whether the device's check of the new settings is good
    enough to keep them; ``failure(reach)`` explains why not. Returns the
    device's check result (or ``None`` when ``verify`` is off).
    """
    with device_lock(Path(receipt).with_suffix(".lock")):
        attempted = False
        try:
            # A running app's watchdog would cut a longer USB session short.
            reset_to_launcher(port, remote)
            attempted = True
            with tempfile.TemporaryDirectory(prefix="presto-settings-") as temporary:
                applied = _run_daemon_script(
                    remote, port, temporary, "apply",
                    _daemon_script(_DAEMON_APPLY, TARGET=target, WANT=want, ORDER=order,
                                   HEADER=header, CREATE=create))
                outcome = applied.get("result")
                if outcome == "no_config":
                    raise RuntimeError(missing)
                if outcome in ("layout", "invalid"):
                    raise RuntimeError(layout_message)
                if outcome != "changed":
                    raise RuntimeError(UNSAFE_CHANGE_MESSAGE)
                reach = None
                if verify:
                    try:
                        reach = _run_daemon_script(remote, port, temporary, "reach",
                                                   HELPERS + _DAEMON_REACH)
                    except (RuntimeError, ValueError, OSError, subprocess.TimeoutExpired):
                        reach = {"result": "error", "joined": False}
                    if not keep(reach):
                        restored = _run_daemon_script(
                            remote, port, temporary, "restore",
                            _daemon_script(_DAEMON_SETTLE, TARGET=target, RESTORE=True,
                                           BEFORE=applied.get("before")))
                        if restored.get("result") != "restored":
                            raise RuntimeError(
                                "The new settings did not work and the earlier file could not "
                                "be put back automatically. It is %s.presto-prev on the "
                                "Presto; running this command again restores it first." % target)
                        raise RuntimeError(failure(reach) + " The previous settings were put back.")
                _run_daemon_script(remote, port, temporary, "finish",
                                   _daemon_script(_DAEMON_SETTLE, TARGET=target, RESTORE=False,
                                                  BEFORE=None))
            return reach
        finally:
            if attempted:
                pending_failure = sys.exc_info()[0] is not None
                try:
                    remote(port, ["soft-reset", "exec", "--no-follow", LAUNCH_AUTO])
                except (RuntimeError, OSError, subprocess.TimeoutExpired):
                    if not pending_failure:
                        raise RuntimeError("Could not relaunch AI Dash. Reconnect USB and "
                                           "run the command again.") from None
                    print("Could not relaunch AI Dash. Reconnect USB.", file=sys.stderr)


def set_daemon_operation(port, host, daemon_port, token=None, verify=True,
                         remote=run_remote, receipt=DEFAULT_RECEIPT):
    """Point the Presto at another daemon; the old settings come back if it fails.

    Only DAEMON_HOST, DAEMON_PORT and (when ``token`` is given) SHARED_TOKEN
    change; the rest of the file is kept byte for byte. With ``verify`` the
    device then joins Wi-Fi and reads the new daemon's status with the token it
    now holds, and anything but a 200 puts the previous file back.
    """
    host = check_daemon_address(host, daemon_port)
    want = {"DAEMON_HOST": host, "DAEMON_PORT": daemon_port}
    if token is not None:
        want["SHARED_TOKEN"] = token
    reach = _settings_session(
        port, CONFIG_PATH, want, sorted(want), "", False, verify,
        keep=lambda result: result.get("result") == "ok",
        failure=lambda result: _reach_message(result.get("result")),
        missing=CONFIG_MISSING, layout_message=DAEMON_LAYOUT_MESSAGE,
        remote=remote, receipt=receipt)
    return {"host": host, "port": daemon_port,
            "reach": reach["result"] if reach else "skipped",
            "token_replaced": token is not None}


def set_wifi_operation(port, ssid, password, verify=True,
                       remote=run_remote, receipt=DEFAULT_RECEIPT):
    """Give the Presto another Wi-Fi network; the old one comes back if it cannot join.

    The settings are kept whenever the Presto joins the new network, even if it
    then cannot reach its daemon from there: moving networks usually means the
    daemon's address changed too, which ``set-daemon`` handles. ``password`` may
    be empty for an open network. A missing ``/secrets.py`` is created.
    """
    check_wifi_credentials(ssid, password)
    want = {"WIFI_SSID": ssid, "WIFI_PASSWORD": password}
    reach = _settings_session(
        port, WIFI_PATH, want, ["WIFI_SSID", "WIFI_PASSWORD"], WIFI_HEADER, True, verify,
        keep=lambda result: bool(result.get("joined")),
        failure=lambda result: WIFI_JOIN_MESSAGE,
        missing=UNSAFE_CHANGE_MESSAGE, layout_message=WIFI_LAYOUT_MESSAGE,
        remote=remote, receipt=receipt)
    return {"ssid": ssid, "joined": reach is not None,
            "daemon": reach["result"] if reach else "skipped",
            "open": password == ""}


def _usb_ready(port, err):
    """True when a USB command may go ahead; otherwise prints why and is False."""
    if not port or port in ("auto", "list"):
        print("An explicit --port is required (for example COM3 or /dev/ttyACM0); "
              "automatic board selection is disabled.", file=err)
        return False
    if importlib.util.find_spec("mpremote") is None:
        print('USB tools are missing. Install them with: pipx install --force '
              '"presto-ai-dash[device]"  (or: pip install "presto-ai-dash[device]")',
              file=err)
        return False
    return True


def _usb_failure(exc, err):
    print(str(exc) if isinstance(exc, (RuntimeError, ValueError)) else
          "USB operation failed or timed out. Check the port and retry.", file=err)
    return 2


def set_daemon_main(argv=None, prog="presto-ai-dash set-daemon", receipt=DEFAULT_RECEIPT,
                    remote=run_remote, out=None, err=None):
    """Entry point for ``set-daemon``; prints the result and returns the exit code."""
    out = out or sys.stdout
    err = err or sys.stderr
    parser = argparse.ArgumentParser(
        prog=prog,
        description="Point a set-up Presto at a different daemon over USB. Only the daemon "
                    "address (and, if asked, the device token) changes. If the Presto cannot "
                    "reach the new daemon, the old settings are put back.")
    parser.add_argument("--port", help="explicit USB port, e.g. COM3 or /dev/ttyACM0")
    parser.add_argument("--daemon-host", required=True,
                        help="the daemon computer's IPv4 address on your network")
    parser.add_argument("--daemon-port", type=int, default=8787,
                        help="the daemon's device port (default 8787)")
    parser.add_argument("--token-env", metavar="NAME",
                        help="name of an environment variable holding the new daemon's shared "
                             "token. Leave it out to keep the token the Presto already has")
    parser.add_argument("--no-reach-check", action="store_true",
                        help="do not check that the Presto can reach the new daemon "
                             "(for an address you are preparing before the daemon runs)")
    args = parser.parse_args(argv)
    if not _usb_ready(args.port, err):
        return 2
    try:
        token = token_from_environment(args.token_env) if args.token_env else None
        result = set_daemon_operation(args.port, args.daemon_host, args.daemon_port, token,
                                      verify=not args.no_reach_check, remote=remote,
                                      receipt=receipt)
    except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
        return _usb_failure(exc, err)
    print("Pointed the Presto at %s:%d." % (result["host"], result["port"]), file=out)
    if result["token_replaced"]:
        print("The device token was replaced. It was not printed.", file=out)
    if result["reach"] == "ok":
        print("The Presto reached that daemon with its device token. AI Dash relaunched.",
              file=out)
    else:
        print("Not checked: the Presto was not asked to reach the new daemon. AI Dash "
              "relaunched.", file=out)
    return 0


def set_wifi_main(argv=None, prog="presto-ai-dash set-wifi", receipt=DEFAULT_RECEIPT,
                  remote=run_remote, out=None, err=None, prompt=None, interactive=None):
    """Entry point for ``set-wifi``; prints the result and returns the exit code."""
    out = out or sys.stdout
    err = err or sys.stderr
    parser = argparse.ArgumentParser(
        prog=prog,
        description="Give a set-up Presto another Wi-Fi network over USB. If the Presto "
                    "cannot join the new network, the old settings are put back.")
    parser.add_argument("--port", help="explicit USB port, e.g. COM3 or /dev/ttyACM0")
    parser.add_argument("--ssid", required=True, help="the network name (2.4 GHz)")
    secret = parser.add_mutually_exclusive_group()
    secret.add_argument("--password-env", metavar="NAME",
                        help="name of an environment variable holding the Wi-Fi password. "
                             "Without this or --open, the password is asked for with the "
                             "typing hidden")
    secret.add_argument("--open", action="store_true", help="the network has no password")
    parser.add_argument("--no-join-check", action="store_true",
                        help="do not check that the Presto can join the network")
    args = parser.parse_args(argv)
    if not _usb_ready(args.port, err):
        return 2
    try:
        if args.open:
            password = ""
        elif args.password_env:
            password = password_from_environment(args.password_env)
        else:
            if interactive is None:
                interactive = sys.stdin.isatty()
            if not interactive:
                raise ValueError("Give the password with --password-env NAME (an environment "
                                 "variable), or --open for a network without one.")
            if prompt is None:
                import getpass
                prompt = getpass.getpass
            password = prompt("Wi-Fi password for %s (hidden): " % args.ssid)
        result = set_wifi_operation(args.port, args.ssid, password,
                                    verify=not args.no_join_check, remote=remote,
                                    receipt=receipt)
    except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
        return _usb_failure(exc, err)
    print("Wi-Fi network set to %s. The password was not printed." % result["ssid"], file=out)
    if not result["joined"]:
        print("Not checked: the Presto was not asked to join it. AI Dash relaunched.", file=out)
    elif result["daemon"] == "ok":
        print("The Presto joined it and reached its daemon. AI Dash relaunched.", file=out)
    else:
        print("The Presto joined it, but could not reach its daemon from there. If the "
              "daemon's address is different on this network, run set-daemon. AI Dash "
              "relaunched.", file=out)
    return 0


def device_layout(port, remote=run_remote, receipt=DEFAULT_RECEIPT):
    """Which layout the device has, without writing anything."""
    probe = (SLOT_LAYOUT_HELPERS +
             "\nprint('PRESTO_SYNC:' + json.dumps({"
             "'config': exists('/ai_dash_files/config.py'), "
             "'legacy': exists('/ai_dash.py'), "
             "'boot': exists('/ai_dash_boot.py')}))\n")
    with device_lock(Path(receipt).with_suffix(".lock")):
        return parse_state(remote(port, ["soft-reset", "exec", probe]))


SLOT_DEVICE_MESSAGE = (
    "This Presto uses the bootstrap and A/B app slots. This command installs the older "
    "single-folder app, which would put a second copy beside them and confuse the next "
    "update, so nothing was written. Update it with the repository's Sync Presto task "
    "(tools/presto_device.py sync) or over the air.")


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
        layout = device_layout(port, receipt=receipt)
        if layout.get("boot") and not layout.get("legacy"):
            try:
                run_remote(port, ["soft-reset", "exec", "--no-follow", LAUNCH_AUTO])
            except (RuntimeError, OSError, subprocess.TimeoutExpired):
                pass
            print(SLOT_DEVICE_MESSAGE, file=err)
            return 2
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


def main(command, argv=None, receipt=DEFAULT_RECEIPT):
    """Entry point for ``install-presto``, ``check-presto``, ``set-daemon`` and ``set-wifi``."""
    if command == "set-daemon":
        return set_daemon_main(argv, receipt=receipt)
    if command == "set-wifi":
        return set_wifi_main(argv, receipt=receipt)
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
