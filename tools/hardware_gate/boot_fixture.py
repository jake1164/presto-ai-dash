"""Disposable bootstrap experiment, independent of production app code.

Writes only generated fixture paths plus the stock launcher's public volatile
handoff. It never replaces /main.py, /ai_dash.py, or /ai_dash_files contents.
"""

import re

NONCE = re.compile(r"^[0-9a-f]{16}$")
GOOD_A = b"KNOWN_GOOD_SYNTHETIC_A\n"
CANDIDATE_B = b"CANDIDATE_SYNTHETIC_B\n"


def build_sources(nonce):
    if not NONCE.fullmatch(nonce):
        raise ValueError("invalid_fixture_nonce")
    root = "/ai_dash_gate_" + nonce
    # MicroPython resolves a same-named folder as a namespace package before
    # the .py entry, even without __init__.py (as in the production launcher).
    entry = "ai_dash_gate_entry_" + nonce + ".py"
    loader = '''# ICON monitoring
# NAME Gate fixture
# DESC Disposable boot experiment.
import os, json, machine, time, hashlib, binascii
ROOT = %r
ENTRY = %r
def digest(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        while True:
            part = f.read(1024)
            if not part:
                break
            h.update(part)
    return binascii.hexlify(h.digest()).decode()
def save(state):
    with open(ROOT + '/state.tmp', 'w') as f:
        f.write(json.dumps(state))
    os.rename(ROOT + '/state.tmp', ROOT + '/state.json')
    if hasattr(os, 'sync'):
        os.sync()
with open(ROOT + '/state.json') as f:
    state = json.loads(f.read())
state['boots'] += 1
state['reset_cause'] = machine.reset_cause()
if state['pending'] and state['tried']:
    state.update({'active': 'a', 'pending': False, 'rolled_back': True, 'confirmed': True, 'writing': False})
    save(state)
elif state['pending']:
    state['tried'] = True
    save(state)
    # The stock carousel consumes its marker before importing the app.
    # Re-arm the same volatile handoff before running an unconfirmed candidate.
    with open('/ramfs/launch.txt', 'w') as f:
        f.write(ENTRY)
    mode = state['mode']
    if mode == 'reset':
        machine.reset()
    elif mode == 'watchdog':
        watchdog = machine.WDT(timeout=1500)
        while True:
            pass
    elif mode == 'power':
        state['writing'] = True
        state['writer_duration_ms'] = 240000
        save(state)
        stop = time.ticks_add(time.ticks_ms(), state['writer_duration_ms'])
        # Keep staging active longer than the host's 180-second armed window,
        # while truncating/reusing one file capped at 1 MiB of occupied space.
        while time.ticks_diff(stop, time.ticks_ms()) > 0:
            with open(ROOT + '/partial.bin', 'wb') as f:
                for i in range(1024):
                    f.write(bytes(range(256)) * 4)
                    time.sleep_ms(20)
                    if time.ticks_diff(stop, time.ticks_ms()) <= 0:
                        break
        state['writing'] = False
        save(state)
        while True:
            time.sleep_ms(100)
    elif mode == 'malformed':
        try:
            exec('this is deliberately invalid python !!!')
        except SyntaxError:
            state.update({'active': 'a', 'pending': False, 'rolled_back': True, 'confirmed': True})
            save(state)
    else:
        state.update({'active': 'b', 'pending': False, 'confirmed': True})
        save(state)
state['active_a_unchanged'] = digest(ROOT + '/a.py') == state['a_hash']
try:
    state['partial_bytes'] = os.stat(ROOT + '/partial.bin')[6]
except OSError:
    state['partial_bytes'] = 0
save(state)
try:
    with open('/ramfs/launch.txt') as f:
        own_marker = f.read() == ENTRY
    if own_marker:
        os.remove('/ramfs/launch.txt')
except OSError:
    pass
''' % (root, entry)
    setup = '''import os, json, hashlib, binascii, sys
if sys.implementation.name != 'micropython' or 'Presto' not in os.uname().machine:
    raise RuntimeError('wrong_board')
ROOT = %r
ENTRY = %r
try:
    os.stat(ROOT)
except OSError:
    pass
else:
    raise RuntimeError('scratch_exists')
try:
    os.stat('/' + ENTRY)
except OSError:
    pass
else:
    raise RuntimeError('entry_exists')
import psram
psram.mkramfs()
try:
    os.stat('/ramfs/launch.txt')
except OSError:
    pass
else:
    raise RuntimeError('launch_already_pending')
fs = os.statvfs('/')
if (fs[1] or fs[0]) * fs[4] < 3145728:
    raise RuntimeError('insufficient_safe_headroom')
os.mkdir(ROOT)
with open(ROOT + '/a.py', 'wb') as f:
    f.write(%r)
with open(ROOT + '/b.py', 'wb') as f:
    f.write(%r)
with open('/' + ENTRY, 'w') as f:
    f.write(%r)
print('PRESTO_GATE:' + json.dumps({'prepared': True}))
''' % (root, entry, GOOD_A, CANDIDATE_B, loader)
    return {"root": root, "entry": entry, "setup": setup, "loader": loader}


def launch_source(fixture, mode):
    if mode not in ("good", "malformed", "reset", "watchdog", "power", "resume"):
        raise ValueError("invalid_fixture_mode")
    prefix = "import os, json, machine, hashlib, binascii, psram\npsram.mkramfs()\nROOT = %r\nENTRY = %r\n" % (fixture["root"], fixture["entry"])
    if mode != "resume":
        prefix += ("state = {'mode': %r, 'active': 'a', 'pending': True, 'tried': False, "
                   "'confirmed': False, 'rolled_back': False, 'boots': 0, "
                   "'a_hash': binascii.hexlify(hashlib.sha256(%r).digest()).decode()}\n"
                   "with open(ROOT + '/state.json', 'w') as f:\n    f.write(json.dumps(state))\n") % (mode, GOOD_A)
    return prefix + "with open('/ramfs/launch.txt', 'w') as f:\n    f.write(ENTRY)\nmachine.reset()\n"


def read_source(fixture):
    return ("import json\nwith open(%r) as f:\n    state = json.loads(f.read())\n"
            "print('PRESTO_GATE:' + json.dumps(state))\n") % (fixture["root"] + "/state.json")


def cleanup_source(fixture):
    return ("import os, psram\npsram.mkramfs()\nROOT = %r\nENTRY = %r\n"
            "for name in ('state.json', 'state.tmp', 'a.py', 'b.py', 'partial.bin'):\n"
            "    try:\n        os.remove(ROOT + '/' + name)\n    except OSError:\n        pass\n"
            "os.rmdir(ROOT)\nos.remove('/' + ENTRY)\n"
            "try:\n    with open('/ramfs/launch.txt') as f:\n        own = f.read() == ENTRY\n"
            "    if own:\n        os.remove('/ramfs/launch.txt')\nexcept OSError:\n    pass\n") % (fixture["root"], fixture["entry"])
