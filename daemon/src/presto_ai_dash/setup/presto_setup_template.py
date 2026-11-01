#!/usr/bin/env python3
"""Presto AI Dash - set up or repair one Pimoroni Presto over USB.

Run this file on the computer the Presto is plugged into by a data-capable USB
cable. It carries the exact public Presto app files of the daemon build that
served it, so the device gets the app its daemon expects.

What it does, in order:

1. reads ``presto_setup.enc`` from this folder (or ``--handoff``);
2. asks for the setup unlock code shown in the daemon's admin page, privately,
   and decrypts the handoff in memory only;
3. lists the USB serial devices and asks which one is the Presto - it never
   chooses a board by itself;
4. checks the board really is a Pimoroni Presto before writing anything;
5. changes only the daemon address and token in the device's existing settings,
   keeping every other setting and its stable random device identifier;
6. shows what will change, installs the app, verifies every file by SHA-256,
   starts the dashboard and checks that the device reaches the daemon.

On success it deletes ``presto_setup.enc``. On failure it keeps it, so the
same download can be retried; ``presto_setup.py`` never deletes anything else.
The setup unlock code is never written to this computer or to the device, and
the Wi-Fi password is only ever typed here and written to the device's
``/secrets.py``.

Nothing is written to the device before the handoff has decrypted and its app
revision has matched this script. Errors are reported as fixed text: a
traceback, a token or a Wi-Fi password is never printed.
"""

# The packages this script needs, in the form `pipx run presto_setup.py` and
# `uv run presto_setup.py` read, so those tools set up their own environment.
# /// script
# dependencies = [
#     "cryptography>=46",
#     "mpremote>=1.24,<2",
# ]
# ///

import argparse
import ast
import base64
import binascii
import getpass
import hashlib
import importlib.util
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import traceback

SETUP_PROTOCOL = 1
REVISION_CHARS = 12
APP_FOLDER = "/ai_dash_files"
CONFIG_PATH = "/ai_dash_files/config.py"
SECRETS_PATH = "/secrets.py"
HANDOFF_NAME = "presto_setup.enc"
REVISION_TARGET = "/ai_dash_files/revision.txt"
# Present only on a Presto that was moved to the bootstrap and A/B app slots.
BOOT_ENTRY = "/ai_dash_boot.py"
# The single-folder app every Presto ran before the bootstrap. Setup moves a
# device off it; nothing installs it any more.
LEGACY_ENTRY = "/ai_dash.py"
SLOT_ROOT = "/ai_dash_slots"
SLOT_NAMES = ("a", "b")
STATE_FOLDER = "/ai_dash_state"
JOURNAL_PATH = STATE_FOLDER + "/journal.json"
OTA_PROFILE_PATH = STATE_FOLDER + "/ota.json"
OTA_CA_PATH = STATE_FOLDER + "/ota-ca.der"
LAYOUT_FOLDERS = (APP_FOLDER, SLOT_ROOT, SLOT_ROOT + "/a", SLOT_ROOT + "/b",
                  "/ai_dash_boot_files", STATE_FOLDER)
# Where a single-folder app counted its boots.
BOOT_COUNTERS = ("/ai_dash_boot.txt", "/ai_dash_boot.txt.tmp")
# Room left on the Presto's filesystem after the install.
FREE_MARGIN = 64 * 1024
OTA_HOSTNAME_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?$")

MARKER = "PRESTO_SETUP:"
MAX_ENVELOPE_BYTES = 8192
MAX_PAYLOAD_BYTES = 4096
SALT_BYTES = 16
NONCE_BYTES = 12
KEY_BYTES = 32
CODE_CHARS = 32
CODE_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"
ENVELOPE_VERSION = 1
KDF_NAME = "HKDF-SHA256"
CIPHER_NAME = "AES-256-GCM"
AAD = b"presto-ai-dash:presto-setup:v1"
KDF_INFO = b"presto-ai-dash:presto-setup:v1"

UNLOCK_ATTEMPTS = 3
# Empty entries do not count as attempts, but they do not go on for ever.
EMPTY_CODE_LIMIT = 5
DEVICE_ID_CHARS = 32

# The same bounds the daemon applies before it seals a handoff. They are
# repeated here because this file must run without the daemon package.
SETTING_LIMITS = {
    "poll_interval": (5, 3600),
    "retry_interval": (5, 3600),
    "http_timeout": (2, 60),
    "wifi_timeout": (2, 120),
    "offline_after_s": (10, 3600),
}
HOSTNAME_PATTERN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$")
REVISION_PATTERN = re.compile(r"^[0-9a-f]{64}$")
LOOPBACK_NAMES = ("127.0.0.1", "localhost", "::1", "0.0.0.0", "::")

LAUNCH = ("import sys, os; sys.path.insert(0, '/ai_dash_files'); "
          "os.chdir('/ai_dash_files'); from ai_dash_app import main; main()")
# Starts AI Dash whichever layout the device has: the bootstrap when it is
# installed, otherwise the single-folder app.
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
# A running slot app arms a watchdog that resets the board within seconds of
# USB stopping it; a hard reset lands on the stock launcher, which arms none.
RESET_CODE = "import machine; machine.reset()"
RECONNECT_ATTEMPTS = 30
# After a hard reset the stock launcher is still starting up, radio included.
# Taking the board over before it has finished leaves the radio unable to join a
# network until the next hard reset, so the board is left alone for this long.
RESET_SETTLE_SECONDS = 8

DEPENDENCY_MESSAGE = (
    "This setup script needs two Python packages on this computer, and one is\n"
    "missing. Install them into the Python that runs this script, then run it\n"
    "again:\n"
    "\n"
    '    python -m pip install "cryptography>=46" "mpremote>=1.24,<2"\n'
    "\n"
    "The daemon itself does not need mpremote; only USB setup does.")

# The same two packages, for the private environment this script can build.
REQUIRED_PACKAGES = ("cryptography>=46", "mpremote>=1.24,<2")
# Set in the child process, so a private environment that still lacks them stops
# with the instructions instead of building another one.
BOOTSTRAP_ENV = "PRESTO_SETUP_PRIVATE_ENV"
MISSING_PACKAGES_MESSAGE = (
    "This script needs two Python packages that are not installed on this computer:\n"
    "cryptography, to open the setup file, and mpremote, to talk to the Presto over\n"
    "USB. It can install them into a private, temporary environment that is deleted\n"
    "when it finishes. Nothing else on this computer changes. Installing them\n"
    "downloads from the internet, so this computer needs to be online.")

PROBE_CODE = r'''
import os, sys, json
machine = ''
firmware = ''
free = None
try:
    machine = os.uname().machine
    firmware = os.uname().version
except Exception:
    pass
try:
    stat = os.statvfs('/')
    free = stat[0] * stat[3]
except Exception:
    pass
print('PRESTO_SETUP:' + json.dumps({'micropython': sys.implementation.name == 'micropython', 'machine': machine,
                                    'firmware': firmware, 'free': free}))
'''

CONFIG_STATE_CODE = r'''
import sys, json
sys.path.insert(0, '/ai_dash_files')
state = {'device_id': None, 'name': None}
try:
    import config
    value = getattr(config, 'DEVICE_ID', None)
    if isinstance(value, str) and len(value) == 32 and all(c in '0123456789abcdef' for c in value):
        state['device_id'] = value
    name = getattr(config, 'DEVICE_NAME', None)
    if isinstance(name, str) and 0 < len(name) <= 32 and all(' ' <= c <= '~' for c in name):
        state['name'] = name
except Exception:
    pass
print('PRESTO_SETUP:' + json.dumps(state))
'''

# One body for both on-device checks of the daemon: the one after the install,
# which asks for the status with the installed settings, and the one before it,
# which asks only the unauthenticated /healthz with the settings about to be
# written. @SETTINGS@ defines HOST, PORT, SSID, PASSWORD, REQUEST and EXPECT.
REACH_BODY = r'''
import sys, json
sys.path.insert(0, '/ai_dash_files')
# This runs in the session that already imported the old settings to read the
# device identifier. Without this, the check would test the old daemon address.
for name in ('config', 'secrets'):
    sys.modules.pop(name, None)
result = 'error'
try:
@SETTINGS@
    import network
    import time
    import socket
    wlan = network.WLAN(network.STA_IF)
    # A weak signal sometimes leaves the radio linked but without an address, or
    # not linked at all; switching the radio off and on between tries clears it.
    for attempt in range(6):
        if attempt:
            wlan.disconnect()
            wlan.active(False)
            time.sleep(0.5)
        if not wlan.active():
            wlan.active(True)
        if wlan.isconnected():
            break
        wlan.connect(SSID, PASSWORD)
        for _ in range(48):
            if wlan.isconnected():
                break
            time.sleep(0.25)
        if wlan.isconnected():
            break
    address = None
    if not wlan.isconnected():
        result = 'wifi'
    else:
        # A name lookup is tried on its own: the Presto adds no DNS search domain,
        # so a bare name that works on a computer can fail here.
        try:
            address = socket.getaddrinfo(HOST, PORT)[0][-1]
        except Exception:
            result = 'dns'
    if address is not None:
        link = socket.socket()
        try:
            link.settimeout(8)
            link.connect(address)
            link.send(REQUEST)
            head = b''
            while len(head) < 384:
                chunk = link.recv(64)
                if not chunk:
                    break
                head += chunk
            code = int(head.split(b' ')[1]) if b'\r\n' in head else 0
            if code == 200 and EXPECT in head:
                result = 'ok'
            elif code == 401:
                result = 'unauthorised'
            else:
                result = 'http_%d' % code
        finally:
            link.close()
except Exception:
    result = 'error'
print('PRESTO_SETUP:' + json.dumps({'reach': result}))
'''

# After the install: the app's own status read, with the settings just written.
REACH_CODE = REACH_BODY.replace("@SETTINGS@", (
    "    import config\n"
    "    import secrets\n"
    "    HOST = config.DAEMON_HOST\n"
    "    PORT = int(config.DAEMON_PORT)\n"
    "    SSID = secrets.WIFI_SSID\n"
    "    PASSWORD = secrets.WIFI_PASSWORD\n"
    "    EXPECT = b''\n"
    "    REQUEST = (b'GET /api/v1/status?cards=lazy HTTP/1.0\\r\\nHost: presto\\r\\n'\n"
    "               b'X-Presto-Client: setup\\r\\nX-Presto-Revision: @REVISION@\\r\\n'\n"
    "               b'Authorization: Bearer ' + config.SHARED_TOKEN.encode() + b'\\r\\n\\r\\n')"))

# Before anything is written: the daemon's health answer, which needs no token and
# says nothing but that a daemon is there. Only the daemon sends this exact body.
HEALTH_REQUEST = b"GET /healthz HTTP/1.0\r\nHost: presto\r\n\r\n"
HEALTH_EXPECT = b'"ok": true'

# ==== BEGIN embedded bundle (generated by presto_ai_dash.device_setup; do not edit) ====
BUNDLE = {}
RELEASE = ""
# ==== END embedded bundle ====


class SetupStopped(Exception):
    """Setup cannot continue, with a fixed message that is safe to print."""


class Cancelled(Exception):
    """The person chose not to install anything."""


class DeviceError(Exception):
    """A USB operation failed; the message never repeats device output."""


# -- the encrypted handoff -------------------------------------------------

def open_handoff(data, code):
    """The decrypted handoff, or SetupStopped.

    Kept deliberately in step with ``presto_ai_dash.device_setup``. A test
    opens every handoff the daemon builds with this function, so the two
    cannot drift apart without failing.
    """
    try:
        from cryptography.exceptions import InvalidTag
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    except ImportError:
        raise SetupStopped(DEPENDENCY_MESSAGE) from None
    if not isinstance(data, (bytes, bytearray)):
        raise SetupStopped("The setup file could not be read.")
    if len(data) > MAX_ENVELOPE_BYTES:
        raise SetupStopped("The setup file is larger than a setup handoff can be.")
    try:
        envelope = json.loads(bytes(data).decode("utf-8"))
    except (ValueError, UnicodeError, RecursionError):
        raise SetupStopped("The setup file is not a setup handoff.") from None
    if not isinstance(envelope, dict) or set(envelope) != {
            "v", "kdf", "cipher", "salt", "nonce", "ct"}:
        raise SetupStopped("The setup file is not a setup handoff of a supported version.")
    if (envelope["v"] != ENVELOPE_VERSION or envelope["kdf"] != KDF_NAME or
            envelope["cipher"] != CIPHER_NAME):
        raise SetupStopped("The setup file is not a setup handoff of a supported version.")
    salt = decode_field(envelope["salt"], SALT_BYTES)
    nonce = decode_field(envelope["nonce"], NONCE_BYTES)
    sealed = decode_field(envelope["ct"], None)
    if len(sealed) > MAX_PAYLOAD_BYTES + 16:
        raise SetupStopped("The setup file is larger than a setup handoff can be.")
    key = HKDF(algorithm=hashes.SHA256(), length=KEY_BYTES, salt=salt,
               info=KDF_INFO).derive(unlock_code_bytes(code))
    try:
        plain = AESGCM(key).decrypt(nonce, sealed, AAD)
    except (InvalidTag, ValueError):
        raise SetupStopped("The unlock code did not open the setup file. Check the code "
                           "in the admin page and try again.") from None
    if len(plain) > MAX_PAYLOAD_BYTES:
        raise SetupStopped("The setup file's contents are too large.")
    try:
        payload = json.loads(plain.decode("utf-8"))
    except (ValueError, UnicodeError, RecursionError):
        raise SetupStopped("The setup file's contents are not a setup configuration.") from None
    if not isinstance(payload, dict) or payload.get("v") != 1:
        raise SetupStopped("The setup file's contents are not a setup configuration.")
    validate_payload(payload)
    return payload


def usable_host(value):
    """Whether an address can be put into ``http://<host>:<port>/``."""
    if not isinstance(value, str) or not value or len(value) > 253:
        return False
    if value != value.strip() or any(char.isspace() for char in value):
        return False
    if "://" in value or "/" in value or ":" in value or "?" in value or "#" in value:
        return False
    if value.startswith(".") or value.endswith(".") or ".." in value:
        return False
    if value.lower() in LOOPBACK_NAMES or value.lower().endswith(".localhost"):
        return False
    if all(char.isdigit() or char == "." for char in value):
        parts = value.split(".")
        return (len(parts) == 4 and
                all(part.isdigit() and str(int(part)) == part and 0 <= int(part) <= 255
                    for part in parts))
    return bool(HOSTNAME_PATTERN.match(value))


def validate_payload(payload):
    """Refuse a decrypted configuration that is not exactly the documented v1."""
    base = {"v", "daemon", "device", "app", "release", "created_at"}
    # "ota" is the daemon's update certificate; a daemon that could not make one
    # leaves it out, and the Presto then updates over USB only.
    if not isinstance(payload, dict) or set(payload) not in (base, base | {"ota"}):
        raise SetupStopped("The setup file's contents are not a setup configuration.")
    if "ota" in payload:
        ota = payload["ota"]
        if (not isinstance(ota, dict) or set(ota) != {"hostname", "ca"} or
                not isinstance(ota["hostname"], str) or
                not OTA_HOSTNAME_PATTERN.match(ota["hostname"])):
            raise SetupStopped("The setup file's update settings are not usable.")
        ca = decode_field(ota["ca"], None) if isinstance(ota["ca"], str) else b""
        if not 64 <= len(ca) <= 4096:
            raise SetupStopped("The setup file's update settings are not usable.")
    daemon = payload["daemon"]
    if not isinstance(daemon, dict) or set(daemon) != {"host", "port", "token"}:
        raise SetupStopped("The setup file's daemon settings are not usable.")
    if not usable_host(daemon["host"]):
        raise SetupStopped("The setup file's daemon address is not one the Presto can use. "
                           "Prepare a new download with the daemon computer's LAN address.")
    port = daemon["port"]
    if type(port) is not int or not 1 <= port <= 65535:
        raise SetupStopped("The setup file's daemon port is not usable.")
    token = daemon["token"]
    if (not isinstance(token, str) or not 16 <= len(token) <= 512 or
            not token.isascii() or any(char.isspace() for char in token)):
        raise SetupStopped("The setup file's device token is not usable.")
    device = payload["device"]
    # "wifi" says whether to collect the Wi-Fi network ("set") or keep the one on
    # the Presto ("keep"). A file prepared by an older daemon has no such key.
    if not isinstance(device, dict) or set(device) - {"wifi"} != {"name", "settings"}:
        raise SetupStopped("The setup file's device settings are not usable.")
    if "wifi" in device and device["wifi"] not in ("set", "keep"):
        raise SetupStopped("The setup file's Wi-Fi choice is not usable.")
    if (not isinstance(device["name"], str) or not device["name"].isascii() or
            not 1 <= len(device["name"]) <= 32 or
            any(not " " <= char <= "~" for char in device["name"])):
        raise SetupStopped("The setup file's device name is not usable.")
    settings = device["settings"]
    if not isinstance(settings, dict) or set(settings) != set(SETTING_LIMITS):
        raise SetupStopped("The setup file's defaults are not usable.")
    for key, (low, high) in SETTING_LIMITS.items():
        value = settings[key]
        if type(value) is not int or not low <= value <= high:
            raise SetupStopped("The setup file's defaults are not usable.")
    app = payload["app"]
    if not isinstance(app, dict) or set(app) != {"revision", "label"}:
        raise SetupStopped("The setup file's app revision is not usable.")
    revision = app["revision"]
    if (not isinstance(revision, str) or not REVISION_PATTERN.match(revision) or
            app["label"] != revision[:REVISION_CHARS]):
        raise SetupStopped("The setup file's app revision is not usable.")
    release = payload["release"]
    if (not isinstance(release, dict) or set(release) != {"version"} or
            not isinstance(release["version"], str) or
            not 1 <= len(release["version"]) <= 64 or not release["version"].isascii()):
        raise SetupStopped("The setup file's release version is not usable.")
    if not isinstance(payload["created_at"], str) or len(payload["created_at"]) > 40:
        raise SetupStopped("The setup file's timestamp is not usable.")
    return payload


def unlock_code_bytes(code):
    """The key material of a typed code, or SetupStopped."""
    if not isinstance(code, str):
        raise SetupStopped("Enter the setup unlock code shown in the admin page.")
    text = "".join(code.split()).replace("-", "").upper()
    if len(text) != CODE_CHARS or any(char not in CODE_ALPHABET for char in text):
        raise SetupStopped("That is not a setup unlock code. Copy all 32 letters and digits.")
    padding = "=" * ((8 - len(text) % 8) % 8)
    try:
        return base64.b32decode(text + padding)
    except (binascii.Error, ValueError):
        raise SetupStopped("That is not a setup unlock code. "
                           "Copy all 32 letters and digits.") from None


def decode_field(value, length):
    if not isinstance(value, str):
        raise SetupStopped("The setup file is not a setup handoff.")
    try:
        raw = base64.b64decode(value.encode("ascii"), validate=True)
    except (binascii.Error, UnicodeError):
        raise SetupStopped("The setup file is not a setup handoff.") from None
    if length is not None and len(raw) != length:
        raise SetupStopped("The setup file is not a setup handoff.")
    return raw


# -- the device ------------------------------------------------------------

class MpremoteDevice:
    """The USB engine: ``mpremote``'s own transport, driven from memory.

    Files are written from bytes, never from a host temporary file and never
    from a command-line argument, so a credential is not left behind to be
    read by another program or a log.
    """

    def __init__(self, port):
        try:
            from mpremote.transport_serial import SerialTransport
        except ImportError:
            raise SetupStopped(DEPENDENCY_MESSAGE) from None
        self.port = port
        self._transport_class = SerialTransport
        try:
            self.transport = SerialTransport(port)
            self.transport.enter_raw_repl()
        except Exception:
            raise DeviceError(
                "The Presto on that port did not answer. Close any other serial "
                "program, press RESET on the Presto, then run this script again.") from None

    def run(self, code, timeout=20):
        try:
            output, error = self.transport.exec_raw(code, timeout=timeout)
        except Exception:
            raise DeviceError("The USB connection stopped. Reconnect the Presto "
                              "and run this script again.") from None
        if error:
            raise DeviceError("The Presto reported an error while running a setup step. "
                              "Reconnect it and run this script again.")
        return output

    def read(self, path):
        try:
            return bytes(self.transport.fs_readfile(path))
        except Exception:
            raise DeviceError("A file could not be read from the Presto. Reconnect it "
                              "and run this script again.") from None

    def exists(self, path):
        try:
            return bool(self.transport.fs_exists(path))
        except Exception:
            return False

    def digest(self, path):
        try:
            return self.transport.fs_hashfile(path, "sha256").hex()
        except Exception:
            return None

    def write(self, path, data):
        try:
            self.transport.fs_writefile(path, bytes(data))
        except Exception:
            raise DeviceError("A file could not be written to the Presto. Reconnect it "
                              "and run this script again.") from None

    def rename(self, before, after):
        self.run(("import os\nos.rename(%r, %r)" % (before, after)).encode("ascii"))

    def remove(self, path):
        try:
            self.transport.fs_rmfile(path)
        except Exception:
            raise DeviceError("A file could not be removed from the Presto. Reconnect it "
                              "and run this script again.") from None

    def mkdir(self, path):
        """Make one folder; one that already exists is fine."""
        if self.exists(path):
            return
        try:
            self.transport.fs_mkdir(path)
        except Exception:
            raise DeviceError("A folder could not be made on the Presto. Reconnect it "
                              "and run this script again.") from None

    def listdir(self, folder):
        try:
            entries = self.transport.fs_listdir(folder)
        except Exception:
            return []
        names = []
        for entry in entries:
            name = entry[0]
            if isinstance(name, (bytes, bytearray)):
                name = bytes(name).decode("utf-8", "replace")
            names.append(str(name))
        return names

    def reset_to_launcher(self):
        """Hard-reset the board and reconnect once it is back on the stock launcher."""
        try:
            self.transport.exec_raw_no_follow(RESET_CODE.encode("ascii"))
        except Exception:
            pass  # The port can vanish before the command has returned.
        self.close()
        time.sleep(RESET_SETTLE_SECONDS)
        for _attempt in range(RECONNECT_ATTEMPTS):
            time.sleep(1)
            try:
                self.transport = self._transport_class(self.port)
                self.transport.enter_raw_repl()
                return
            except Exception:
                continue
        raise DeviceError("The Presto did not come back after a restart. Reconnect it "
                          "and run this script again.")

    def launch(self):
        self.transport.exec_raw_no_follow(LAUNCH_AUTO.encode("ascii"))

    def close(self):
        for action in (self.transport.exit_raw_repl, self.transport.close):
            try:
                action()
            except Exception:
                pass


def open_device(port):
    return MpremoteDevice(port)


# -- ports -----------------------------------------------------------------

def parse_ports(text):
    """``(port, description)`` pairs from ``mpremote connect list`` output."""
    ports = []
    for line in str(text).splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(None, 1)
        device = parts[0]
        if not device or device.lower() in ("mpremote", "found"):
            continue
        if device.isdigit():
            continue
        ports.append((device, parts[1].strip() if len(parts) > 1 else ""))
    return ports


def list_ports():
    """What is plugged in, without choosing anything."""
    try:
        import serial.tools.list_ports as listing
    except Exception:
        raise SetupStopped(DEPENDENCY_MESSAGE) from None
    try:
        found = sorted(listing.comports(), key=lambda item: item.device)
    except Exception:
        return []
    return [(item.device, item.description or "") for item in found]


def choose_port(ports, requested, ask, say):
    if not ports:
        raise SetupStopped("No USB serial device was found. Plug the Presto in with a "
                           "data-capable USB cable, then run this script again.")
    if requested:
        for device, _description in ports:
            if device.lower() == str(requested).strip().lower():
                return device
        raise SetupStopped("That port is not one of the connected USB devices. "
                           "Run this script again and choose from the list.")
    if len(ports) == 1:
        # Nothing is written to it before it has proved to be a Presto, and the
        # summary names it before anything is changed.
        say("Using the only USB device found: %s  %s" % ports[0])
        return ports[0][0]
    say("USB devices:")
    for index, (device, description) in enumerate(ports, 1):
        say("  %d. %s  %s" % (index, device, description))
    while True:
        answer = ask("Choose the Presto by number (it is never chosen for you): ").strip()
        if answer.isdigit() and 1 <= int(answer) <= len(ports):
            return ports[int(answer) - 1][0]
        say("Enter a number from 1 to %d." % len(ports))


# -- device questions ------------------------------------------------------

def marker_json(output, what):
    text = (bytes(output).decode("utf-8", "replace")
            if isinstance(output, (bytes, bytearray)) else str(output))
    lines = [line for line in text.splitlines() if line.startswith(MARKER)]
    if len(lines) != 1:
        raise SetupStopped("The Presto did not answer the %s check. Reconnect it and "
                           "run this script again." % what)
    try:
        value = json.loads(lines[0][len(MARKER):])
    except ValueError:
        raise SetupStopped("The Presto did not answer the %s check. Reconnect it and "
                           "run this script again." % what) from None
    if not isinstance(value, dict):
        raise SetupStopped("The Presto did not answer the %s check. Reconnect it and "
                           "run this script again." % what)
    return value


def safe_text(value, limit=32):
    if not isinstance(value, str):
        return "unknown"
    text = "".join(char if " " <= char <= "~" else "?" for char in value)
    return text[:limit] or "unknown"


def probe_board(device):
    """The board's identity, after refusing anything that is not a Presto.

    Also its firmware version, which the update profile is tied to, and its
    free space.
    """
    state = marker_json(device.run(PROBE_CODE.encode("ascii")), "board")
    machine = safe_text(state.get("machine"))
    if not state.get("micropython") or "Presto" not in machine:
        raise SetupStopped("The device on that port is not a Pimoroni Presto (%s). "
                           "Nothing was written." % machine)
    firmware = state.get("firmware")
    free = state.get("free")
    return {"machine": machine,
            "firmware": firmware if isinstance(firmware, str) and firmware.isascii() else "",
            "free": free if type(free) is int and free >= 0 else None}


def config_state(device):
    """Only the identity a repair must keep: never the token or any other value."""
    return marker_json(device.run(CONFIG_STATE_CODE.encode("ascii")), "settings")


def check_reachability(device):
    """Run the app's own read on the device and report a fixed result code."""
    # The daemon lists whoever fetches its status: say this is the setup, and which
    # app build it carries, instead of appearing as an unnamed client.
    code = REACH_CODE.replace("@REVISION@",
                              str((BUNDLE.get("slot") or {}).get("revision") or "")[:REVISION_CHARS])
    state = marker_json(device.run(code.encode("ascii"), timeout=180), "connection")
    return safe_text(state.get("reach"), 16)


def probe_daemon(host, port, timeout=6):
    """Where the daemon is, seen from this computer, as a fixed result code.

    It asks only /healthz, which needs no token, so the device token is never
    sent to an address that has not yet proved to be the daemon. The codes are
    the ones the Presto's own check reports, plus "refused" and "timeout".
    """
    try:
        address = socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM)[0][-1]
    except (OSError, IndexError):
        return "dns"
    reply = b""
    try:
        with socket.create_connection(address, timeout=timeout) as link:
            link.sendall(HEALTH_REQUEST)
            while len(reply) < 384:
                chunk = link.recv(128)
                if not chunk:
                    break
                reply += chunk
    except socket.timeout:
        return "timeout"
    except ConnectionRefusedError:
        return "refused"
    except OSError:
        return "error"
    words = reply.split(b"\r\n", 1)[0].split(b" ")
    code = int(words[1]) if len(words) > 1 and words[1].isdigit() else 0
    if code == 200 and HEALTH_EXPECT in reply:
        return "ok"
    return "http_%d" % code if code else "error"


def precheck_code(host, port, ssid=None, password=None):
    """The on-device check to run before anything is written.

    It tries the Wi-Fi settings the install is about to write, or the ones the
    Presto already has when none are being replaced, and asks only /healthz.
    """
    if ssid is None:
        wifi = ("    import secrets\n"
                "    SSID = secrets.WIFI_SSID\n"
                "    PASSWORD = secrets.WIFI_PASSWORD\n")
    else:
        # ascii() keeps the code ASCII for the serial line and the value exact.
        wifi = "    SSID = %s\n    PASSWORD = %s\n" % (ascii(ssid), ascii(password or ""))
    settings = ("    HOST = %s\n    PORT = %d\n%s    EXPECT = %r\n    REQUEST = %r"
                % (ascii(host), int(port), wifi, HEALTH_EXPECT, HEALTH_REQUEST))
    return REACH_BODY.replace("@SETTINGS@", settings)


def check_daemon_from_presto(device, host, port, ssid=None, password=None):
    """Whether the Presto can reach the daemon with the settings about to be written."""
    code = precheck_code(host, port, ssid, password)
    state = marker_json(device.run(code.encode("ascii"), timeout=180), "connection")
    return safe_text(state.get("reach"), 16)


# -- settings files --------------------------------------------------------

def config_text(payload, name, device_id):
    """A complete device configuration, built from the handoff in memory."""
    daemon = payload["daemon"]
    settings = payload["device"]["settings"]
    lines = [
        "# Presto AI Dash device settings.",
        "# Private: this file holds the shared device token. Never commit or share it.",
        "# Generated by presto_setup.py for Presto AI Dash %s." % payload["release"]["version"],
        "",
        "DAEMON_HOST = %r" % daemon["host"],
        "DAEMON_PORT = %d" % daemon["port"],
        "# Must match daemon.shared_token in the daemon configuration.",
        "SHARED_TOKEN = %r" % daemon["token"],
        "",
        "DEVICE_NAME = %r" % name,
        "# Generated once, then kept on repair: it is how the daemon tells devices",
        "# apart, and it is not part of the replaceable app files.",
        "DEVICE_ID = %r" % device_id,
        "# Empty: the deployment writes the app revision next to the app files.",
        'DEVICE_REVISION = ""',
        "",
        "POLL_INTERVAL = %d" % settings["poll_interval"],
        "RETRY_INTERVAL = %d" % settings["retry_interval"],
        "HTTP_TIMEOUT = %d" % settings["http_timeout"],
        "WIFI_TIMEOUT = %d" % settings["wifi_timeout"],
        "OFFLINE_AFTER_S = %d" % settings["offline_after_s"],
        "",
    ]
    return ("\n".join(lines)).encode("utf-8")


def secrets_text(ssid, password):
    """The root-level Wi-Fi file the firmware reads."""
    lines = [
        "# Presto AI Dash Wi-Fi credentials.",
        "# Private: never commit or share this file. Use a 2.4 GHz network.",
        "WIFI_SSID = %r" % ssid,
        "WIFI_PASSWORD = %r" % password,
        "",
    ]
    return ("\n".join(lines)).encode("utf-8")


def new_device_id():
    return os.urandom(16).hex()


# -- installing ------------------------------------------------------------

def slot_targets():
    """Installed path -> bytes, in commit order: both slots, then the bootstrap.

    The bootstrap's entry file is committed last, so an install that stops
    part-way leaves a device that still starts what it had before.
    """
    slots = []
    for name in SLOT_NAMES:
        for file_name, data in sorted(BUNDLE["slot"]["files"].items()):
            # A wheel's bundle names its slot files with a leading slash, a
            # checkout's without; the installed path must not carry a doubled one.
            slots.append((SLOT_ROOT + "/" + name + "/" + file_name.lstrip("/"), data))
    boot = sorted(BUNDLE["boot"]["files"].items(), key=lambda item: item[0] == BOOT_ENTRY)
    return slots, boot


def install_size():
    slots, boot = slot_targets()
    return sum(len(data) for _path, data in slots + boot)


def boots_so_far(device):
    """The highest boot count the device has recorded, in either layout.

    The daemon orders a device's reports by (boot, sequence), so a device that
    keeps its identity must never count its boots from 1 again.
    """
    counts = [1]
    for path in BOOT_COUNTERS:
        if device.exists(path):
            text = device.read(path)[:12].strip()
            if text.isdigit():
                counts.append(int(text))
    if device.exists(JOURNAL_PATH):
        try:
            journal = json.loads(device.read(JOURNAL_PATH)[:1024].decode("utf-8"))
            if type(journal.get("boots")) is int:
                counts.append(journal["boots"])
        except (ValueError, UnicodeError, AttributeError):
            pass
    return max(counts)


def journal_text(boots):
    """A fresh activation journal: slot a active and confirmed, nothing pending."""
    return json.dumps({"v": 1, "bootstrap": int(BUNDLE["bootstrap"]), "boots": int(boots),
                       "active": "a", "confirmed": "a", "pending": None, "failure": None,
                       "success": None, "transfer": None},
                      separators=(",", ":")).encode("ascii")


def legacy_paths(device):
    """The single-folder app a Presto ran before, never its settings."""
    paths = [LEGACY_ENTRY, REVISION_TARGET]
    for name in device.listdir(APP_FOLDER):
        lower = name.lower()
        # The modules, the revision label, and staging files an interrupted
        # older install left behind.
        if (lower.endswith((".py", ".mpy", ".txt", ".presto-new", ".presto-prev"))
                and not lower.startswith(("config.", "secrets."))):
            paths.append(APP_FOLDER + "/" + name)
    return [path for path in dict.fromkeys(paths) if device.exists(path)]


def previous_app_is_back(device, attempted):
    """Whether every file the install tried to replace is as it was before.

    A file that existed is back under its own name with no backup left beside
    it; a file that did not exist is gone. A device that cannot answer is not
    trusted.
    """
    try:
        for path, had_old in attempted:
            if had_old:
                if not device.exists(path) or device.exists(path + ".presto-prev"):
                    return False
            elif device.exists(path):
                return False
    except Exception:
        return False
    return True


def install_slots(device, plan, say):
    """Install the bootstrap and both app slots, then retire a single-folder app.

    Every file is staged beside its target, verified by SHA-256 and only then
    moved into place; a failure puts back every file it moved. Both slots get
    the same verified app, so the device never starts on an unproven one.
    """
    # Until the first file is replaced, the app on the device is the previous
    # one, untouched. ``run`` starts it again if setup stops in that state.
    plan["previous_app_intact"] = True
    free = plan.get("free")
    needed = install_size() + FREE_MARGIN
    if type(free) is int and free < needed:
        raise SetupStopped("The Presto has %d KiB free and the app needs %d KiB. Nothing "
                           "was written; remove files you do not need from it and run this "
                           "script again." % (free // 1024, needed // 1024))
    for folder in LAYOUT_FOLDERS:
        device.mkdir(folder)
    slots, boot = slot_targets()
    journal = journal_text(boots_so_far(device))
    say("Copying %d files (%.0f KiB) to the Presto; this takes a minute or two..."
        % (len(slots) + len(boot), install_size() / 1024.0))
    for path, data in slots + boot:
        device.write(path + ".presto-new", data)
    for path, data in slots + boot:
        if device.digest(path + ".presto-new") != hashlib.sha256(data).hexdigest():
            raise SetupStopped("A file did not arrive intact. No installed file was "
                               "replaced; reconnect the Presto and run this script again.")
    device.write(JOURNAL_PATH + ".presto-new", journal)
    if device.read(JOURNAL_PATH + ".presto-new") != journal:
        raise SetupStopped("A file did not arrive intact. No installed file was "
                           "replaced; reconnect the Presto and run this script again.")
    order = [path for path, _data in slots] + [JOURNAL_PATH] + [path for path, _data in boot]
    wanted = {path: hashlib.sha256(data).hexdigest() for path, data in slots + boot}
    wanted[JOURNAL_PATH] = hashlib.sha256(journal).hexdigest()
    changed = []
    attempted = []
    plan["previous_app_intact"] = False
    try:
        for path in order:
            backup = path + ".presto-prev"
            if device.exists(backup):
                device.remove(backup)
            had_old = device.exists(path)
            attempted.append((path, had_old))
            if had_old:
                device.rename(path, backup)
            changed.append((path, had_old))
            device.rename(path + ".presto-new", path)
        for path in order:
            if device.digest(path) != wanted[path]:
                raise SetupStopped("The installed app did not verify. Everything was put "
                                   "back; reconnect the Presto and run this script again.")
    except Exception:
        for path, had_old in reversed(changed):
            try:
                if device.exists(path):
                    device.remove(path)
                if had_old:
                    device.rename(path + ".presto-prev", path)
            except Exception:
                pass
        # The undo above is best effort; only a device that shows every moved
        # file back under its own name counts as the previous app being intact.
        plan["previous_app_intact"] = previous_app_is_back(device, attempted)
        plan["rollback_unconfirmed"] = not plan["previous_app_intact"]
        raise
    for path in order:
        backup = path + ".presto-prev"
        if device.exists(backup):
            device.remove(backup)
    # A module an older build had in a slot is no longer part of the app.
    for name in SLOT_NAMES:
        folder = SLOT_ROOT + "/" + name
        for file_name in device.listdir(folder):
            path = folder + "/" + file_name
            if path not in wanted and file_name.lower().endswith((".py", ".mpy")):
                device.remove(path)
    # Only now, with the new layout verified, does the old one go.
    retired = 0
    if plan.get("legacy"):
        for path in legacy_paths(device):
            device.remove(path)
            retired += 1
    say("Installed the app in two slots with its bootstrap and verified every file by "
        "SHA-256%s." % ("; the previous single-folder app was removed" if retired else ""))


def ota_profile(payload, firmware):
    """The device's update profile: where to fetch the app and whom to trust."""
    ota = payload["ota"]
    return json.dumps({"enabled": True, "host": payload["daemon"]["host"],
                       "port": int(payload["daemon"]["port"]),
                       "hostname": ota["hostname"], "ca_file": OTA_CA_PATH,
                       "firmware": firmware}, sort_keys=True).encode("ascii")


def write_ota(device, payload, firmware, say):
    """Turn on updates over the air: the profile and the daemon's certificate.

    Neither file is secret: the certificate is public, and the profile holds
    the daemon's address. They live outside both app slots, so an update never
    replaces them.
    """
    if "ota" not in payload:
        say("This daemon did not include an update certificate, so app updates over "
            "the air stay off. Check the daemon log, then run this script again.")
        return
    ca = base64.b64decode(payload["ota"]["ca"])
    profile = ota_profile(payload, firmware)
    for path, data in ((OTA_CA_PATH, ca), (OTA_PROFILE_PATH, profile)):
        device.write(path + ".presto-new", data)
        if device.read(path + ".presto-new") != data:
            raise SetupStopped("A file did not arrive intact. Reconnect the Presto and run "
                               "this script again.")
        if device.exists(path):
            device.remove(path)
        device.rename(path + ".presto-new", path)
    say("Turned on app updates over the air from this daemon.")


def write_private(device, path, data):
    """Write a settings file safely, with the previous one kept until verified.

    The file is read back and compared byte for byte. It is a few hundred bytes,
    and it holds the token and the Wi-Fi password, which are never hashed.
    """
    staging, backup = path + ".presto-new", path + ".presto-prev"
    wanted = bytes(data)
    device.write(staging, data)
    if device.read(staging) != wanted:
        try:
            device.remove(staging)
        except Exception:
            pass
        raise SetupStopped("A settings file did not arrive intact. Nothing was replaced; "
                           "reconnect the Presto and run this script again.")
    if device.exists(backup):
        device.remove(backup)
    had_old = device.exists(path)
    if had_old:
        device.rename(path, backup)
    try:
        device.rename(staging, path)
        if device.read(path) != wanted:
            raise SetupStopped("A settings file did not verify. The previous one was put back.")
    except Exception:
        try:
            if device.exists(path):
                device.remove(path)
            if had_old:
                device.rename(backup, path)
        except Exception:
            pass
        raise
    if device.exists(backup):
        device.remove(backup)


CONFIG_LINES = ("DAEMON_HOST", "DAEMON_PORT", "SHARED_TOKEN")


def merge_assignments(existing, wanted, defaults=None):
    """A settings file with only the named lines changed, or None.

    ``wanted`` maps each name to the value it must have; a name the file lacks is
    added at the end. ``defaults`` are added only when the file has no such line.
    Everything else stays: tuning, comments, other keys. The file is parsed, never
    run, because it came from a device. None means it does not have the usual shape
    (it does not parse, or sets a wanted name twice, or over several lines), and the
    caller writes a fresh file instead.
    """
    try:
        text = bytes(existing).decode("utf-8")
        tree = ast.parse(text)
    except (UnicodeError, SyntaxError, ValueError):
        return None
    found = {}
    for node in tree.body:
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)):
            found.setdefault(node.targets[0].id, []).append(node)
    lines = text.split("\n")
    for key in wanted:
        nodes = found.get(key, [])
        if len(nodes) > 1 or any(node.lineno != node.end_lineno for node in nodes):
            return None
    additions = []
    for key, value in wanted.items():
        nodes = found.get(key, [])
        line = "%s = %r" % (key, value)
        if nodes:
            index = nodes[0].lineno - 1
            tail = "\r" if lines[index].endswith("\r") else ""
            lines[index] = line + tail
        else:
            additions.append(line)
    for key, value in (defaults or {}).items():
        if key not in found:
            additions.append("%s = %r" % (key, value))
    merged = "\n".join(lines)
    if additions:
        merged = merged.rstrip("\r\n") + "\n" + "\n".join(additions) + "\n"
    try:
        check = {}
        for node in ast.parse(merged).body:
            if (isinstance(node, ast.Assign) and len(node.targets) == 1
                    and isinstance(node.targets[0], ast.Name) and node.targets[0].id in wanted):
                check[node.targets[0].id] = ast.literal_eval(node.value)
    except (SyntaxError, ValueError):
        return None
    if check != wanted:
        return None
    return merged.encode("utf-8")


def merge_config(existing, payload, name, device_id):
    """The Presto's own settings file with only the daemon lines changed, or None."""
    daemon = payload["daemon"]
    return merge_assignments(
        existing,
        {"DAEMON_HOST": daemon["host"], "DAEMON_PORT": int(daemon["port"]),
         "SHARED_TOKEN": daemon["token"]},
        {"DEVICE_ID": device_id, "DEVICE_NAME": name})


def ask_settings(device, payload, requested_name, ask, say):
    """Decide what to keep; nothing is read that does not have to be."""
    config_exists = device.exists(CONFIG_PATH)
    secrets_exists = device.exists(SECRETS_PATH)
    wifi = payload["device"].get("wifi")
    if wifi == "keep" and not secrets_exists:
        raise SetupStopped(NO_WIFI_MESSAGE)
    existing = config_state(device) if config_exists else {}
    if wifi == "set":
        replace_secrets = True
    elif wifi == "keep":
        replace_secrets = False
    else:
        # A setup file from an older daemon does not say, so ask as before.
        replace_secrets = not secrets_exists
        if secrets_exists:
            replace_secrets = yes(ask("Replace the Wi-Fi settings in /secrets.py? [y/N] "))
    ssid = password = None
    if replace_secrets:
        ssid = ask("Wi-Fi network name (SSID, 2.4 GHz): ").strip()
        if not ssid:
            raise Cancelled()
        password = ask("Wi-Fi password (hidden; empty for an open network): ", secret=True)
    name = (requested_name or existing.get("name") or payload["device"]["name"] or "presto")
    if not isinstance(name, str) or not name.isascii() or len(name) > 32:
        name = "presto"
    # The Wi-Fi file is treated the same way: only its two Wi-Fi lines change, so any
    # other key kept there (other apps keep theirs in this same file) stays.
    secrets_mode, secrets_data = "create", None
    if replace_secrets and secrets_exists:
        secrets_data = merge_assignments(device.read(SECRETS_PATH),
                                         {"WIFI_SSID": ssid, "WIFI_PASSWORD": password})
        secrets_mode = "update" if secrets_data is not None else "rewrite"
    kept_id = bool(existing.get("device_id"))
    device_id = existing.get("device_id") or new_device_id()
    # A Presto that already has settings keeps all of them: only the daemon lines
    # change, so there is nothing to ask. A fresh file is written only when there is
    # none, or when the existing one does not have the usual shape.
    mode, data = "create", None
    if config_exists:
        data = merge_config(device.read(CONFIG_PATH), payload, name, device_id)
        mode = "update" if data is not None else "rewrite"
    return {"config": mode, "config_data": data, "secrets": replace_secrets,
            "secrets_mode": secrets_mode, "secrets_data": secrets_data,
            "ssid": ssid, "password": password, "name": name, "device_id": device_id,
            "kept_id": kept_id}


def preview(payload, plan, port, machine, say):
    daemon = payload["daemon"]
    slots, boot = slot_targets()
    say("")
    say("Presto AI Dash %s (app revision %s) on %s (%s):"
        % (RELEASE or "unknown", str(BUNDLE["slot"]["revision"])[:REVISION_CHARS], port, machine))
    say("  - The app with over-the-air updates: %d files, %.0f KiB, in two app slots"
        % (len(slots) + len(boot), install_size() / 1024.0))
    say("    and a small bootstrap, verified by SHA-256 and put back if a write fails.")
    if plan.get("legacy"):
        say("  - The single-folder app this Presto runs now (/ai_dash.py) is removed once")
        say("    the new one has verified. Its settings stay.")
    elif plan.get("slot"):
        say("  - This Presto already updates over the air: both slots are reinstalled.")
    if "ota" in payload:
        say("  - Later app updates arrive from the daemon; USB is only needed to recover.")
    if plan["config"] == "update":
        say("  - /ai_dash_files/config.py: point it at daemon %s:%d with the new device token."
            % (daemon["host"], int(daemon["port"])))
        say("    Every other setting in it stays.")
    elif plan["config"] == "rewrite":
        say('  - /ai_dash_files/config.py: write a new one for daemon %s:%d, name "%s", %s device ID.'
            % (daemon["host"], int(daemon["port"]), plan["name"],
               "existing" if plan.get("kept_id") else "new"))
        say("    The existing file does not have the usual shape, so its other settings are not kept.")
    else:
        say('  - /ai_dash_files/config.py: create it for daemon %s:%d, name "%s", new device ID.'
            % (daemon["host"], int(daemon["port"]), plan["name"]))
    if plan["secrets"] and plan.get("secrets_mode") == "update":
        say("  - /secrets.py: change the Wi-Fi network and password to the ones you entered.")
        say("    Every other line in it stays.")
    elif plan["secrets"] and plan.get("secrets_mode") == "rewrite":
        say("  - /secrets.py: write a new one with the Wi-Fi network you entered.")
        say("    The existing file does not have the usual shape, so its other lines are not kept.")
    elif plan["secrets"]:
        say("  - /secrets.py: create it with the Wi-Fi network you entered.")
    else:
        say("  - /secrets.py: keep the existing file.")
    say("  - Firmware, the stock launcher and every other file are left alone.")
    say("")


def apply_plan(device, payload, plan, say):
    install_slots(device, plan, say)
    if plan["config"] == "update":
        write_private(device, CONFIG_PATH, plan["config_data"])
    else:
        write_private(device, CONFIG_PATH, config_text(payload, plan["name"], plan["device_id"]))
    say("Wrote /ai_dash_files/config.py and verified it.")
    if plan["secrets"]:
        if plan.get("secrets_mode") == "update":
            write_private(device, SECRETS_PATH, plan["secrets_data"])
        else:
            write_private(device, SECRETS_PATH, secrets_text(plan["ssid"], plan["password"]))
        say("Wrote /secrets.py and verified it.")
    if not device.exists(CONFIG_PATH):
        raise SetupStopped("The device has no /ai_dash_files/config.py, so it cannot reach "
                           "the daemon. Run this script again and replace its settings.")
    write_ota(device, payload, plan.get("firmware") or "", say)


# -- the run ---------------------------------------------------------------

def find_handoff(explicit):
    if explicit:
        return explicit
    beside = os.path.join(os.path.dirname(os.path.abspath(__file__)), HANDOFF_NAME)
    if os.path.exists(beside):
        return beside
    here = os.path.join(os.getcwd(), HANDOFF_NAME)
    if os.path.exists(here):
        return here
    raise SetupStopped("No %s was found. Put it in the same folder as this script, "
                       "or pass --handoff with its path." % HANDOFF_NAME)


def check_handoff(path):
    """Stop now if the setup file cannot be opened.

    This runs before the two packages are offered, so nobody installs them only
    to learn that the setup file is missing.
    """
    try:
        with open(path, "rb"):
            pass
    except OSError:
        raise SetupStopped("The setup file could not be read. Download it again from "
                           "the admin page.") from None


def require_cryptography():
    """Fail with the install instructions before asking for a code.

    A missing library is not a wrong code, so it must not be retried three
    times as one.
    """
    try:
        from cryptography.exceptions import InvalidTag  # noqa: F401
        from cryptography.hazmat.primitives import hashes  # noqa: F401
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # noqa: F401
        from cryptography.hazmat.primitives.kdf.hkdf import HKDF  # noqa: F401
    except ImportError:
        raise SetupStopped(DEPENDENCY_MESSAGE) from None


def read_handoff(path, ask, say):
    require_cryptography()
    try:
        with open(path, "rb") as handle:
            data = handle.read(MAX_ENVELOPE_BYTES + 1)
    except OSError:
        raise SetupStopped("The setup file could not be read. Download it again from "
                           "the admin page.") from None
    if len(data) > MAX_ENVELOPE_BYTES:
        raise SetupStopped("The setup file is larger than a setup handoff can be.")
    attempts, empty = 0, 0
    while attempts < UNLOCK_ATTEMPTS:
        code = ask("Setup unlock code: ", secret=True)
        if not str(code).strip():
            # Enter on its own (or a paste that did not arrive) is not a wrong code.
            empty += 1
            if empty > EMPTY_CODE_LIMIT:
                raise SetupStopped("No unlock code was entered. Run this script again and "
                                   "paste or type the code shown in the admin page.")
            say("Nothing was entered. Paste or type the code shown in the admin page.")
            continue
        attempts += 1
        try:
            return open_handoff(data, code)
        except SetupStopped:
            say("The unlock code did not open the setup file, or the file is damaged."
                " Check the code in the admin page and try again.")
    raise SetupStopped("The setup file could not be opened. Generate a new handoff in the "
                       "admin page and make sure the unlock code is copied exactly.")


def check_bundle(payload):
    revision = BUNDLE.get("revision")
    if not isinstance(revision, str) or len(revision) != 64:
        raise SetupStopped("This setup script has no Presto app files. Download it again "
                           "from the daemon's admin page.")
    app = payload.get("app")
    if not isinstance(app, dict) or app.get("revision") != revision:
        raise SetupStopped("This setup file was made for a different Presto app build than "
                           "this script. Download both files again from the admin page.")
    daemon = payload.get("daemon")
    if not isinstance(daemon, dict) or not isinstance(daemon.get("host"), str):
        raise SetupStopped("The setup file's daemon address is not usable.")
    settings = (payload.get("device") or {}).get("settings")
    if not isinstance(settings, dict):
        raise SetupStopped("The setup file's defaults are not usable.")


def yes(answer):
    return str(answer).strip().lower() in ("y", "yes")


def proceed(answer):
    """The one confirmation before anything is written: Enter, y or yes."""
    return str(answer).strip().lower() in ("", "y", "yes")


def run(arguments, device_factory, ask, say):
    handoff_path = find_handoff(arguments.handoff)
    if not BUNDLE.get("revision"):
        raise SetupStopped("This setup script has no Presto app files. Download it again "
                           "from the daemon's admin page.")
    say("Presto AI Dash %s - setting up a Presto." % (RELEASE or "unknown"))
    payload = read_handoff(handoff_path, ask, say)
    check_bundle(payload)
    host, daemon_port = safe_text(payload["daemon"]["host"], 253), int(payload["daemon"]["port"])
    say("Opened the setup file for daemon %s:%d." % (host, daemon_port))
    check = not arguments.skip_daemon_check
    if check:
        # Before a USB device is opened: a wrong address or a daemon that is not
        # running is found here, with nothing on the Presto touched.
        say("Checking that the daemon answers at %s:%d from this computer..." % (host, daemon_port))
        seen = probe_daemon(host, daemon_port)
        if seen != "ok":
            raise SetupStopped(computer_message(seen, host, daemon_port))
        say("The daemon answered.")
    ports = list_ports()
    port = choose_port(ports, arguments.port, ask, say)
    device = device_factory(port)
    wrote = False
    completed = False
    had_app = False
    plan = {}
    try:
        board = probe_board(device)
        # Every Presto ends on the bootstrap and two app slots, which is what
        # updates over the air. A running slot app arms a watchdog that would
        # reset the board under this script, so it goes back to the launcher
        # first.
        slot = device.exists(BOOT_ENTRY)
        # Reading the Presto stops the app that is running on it. Until
        # something is written, that app is still intact and is started again
        # if setup ends without installing (a "no", Ctrl-C or an early stop).
        had_app = slot or device.exists(LEGACY_ENTRY)
        if slot:
            say("Stopping AI Dash on the Presto so setup can read it; its screen goes "
                "dark. If you answer n below, AI Dash is started again.")
            device.reset_to_launcher()
        plan = ask_settings(device, payload, arguments.name, ask, say)
        plan["slot"] = slot
        plan["legacy"] = device.exists(LEGACY_ENTRY) or device.exists(REVISION_TARGET)
        plan["firmware"] = board["firmware"]
        plan["free"] = board["free"]
        if check:
            # The Presto reaches the daemon over Wi-Fi, which this computer cannot
            # see. Ask it now, with the Wi-Fi settings it is about to be given, so
            # a wrong address or password is found before anything is replaced.
            say("Checking that the Presto can reach the daemon (this can take a minute)...")
            reach = check_daemon_from_presto(
                device, host, daemon_port,
                plan["ssid"] if plan["secrets"] else None,
                plan["password"] if plan["secrets"] else None)
            if reach != "ok":
                raise SetupStopped(precheck_message(reach, host))
            say("The Presto reached the daemon.")
        preview(payload, plan, port, board["machine"], say)
        if not proceed(ask("Continue? [Y/n] ")):
            raise Cancelled()
        wrote = True
        apply_plan(device, payload, plan, say)
        say("Checking that the Presto can reach the daemon...")
        reach = check_reachability(device)
        device.launch()
        if reach != "ok":
            raise SetupStopped(reach_message(reach, safe_text(payload["daemon"]["host"], 253)))
        say("The Presto reached the daemon. AI Dash is running; it will appear in the "
            "admin page's Status tab on its next poll.")
        remove_handoff(handoff_path, say)
        completed = True
    finally:
        # The previous app runs again when nothing was written, or when a failed
        # install put every file back and the device confirms it.
        if had_app and (not wrote or plan.get("previous_app_intact")):
            restart_app(device, say)
        elif had_app and plan.get("rollback_unconfirmed"):
            say("The previous app could not be confirmed after the failed install, so it "
                "was not started. Run this script again to repair the Presto.")
        device.close()
        if wrote and not completed:
            say("The encrypted setup file was kept, so you can run this script again.")


def restart_app(device, say):
    """Start the Presto's untouched app again after setup stopped it and wrote nothing."""
    try:
        device.launch()
    except Exception:
        say("AI Dash could not be started again. Press RESET on the Presto, or unplug "
            "it and plug it back in.")
        return
    say("AI Dash was started again on the Presto.")


NO_WIFI_MESSAGE = (
    "The admin page was set not to change the Wi-Fi, but this Presto has no Wi-Fi "
    "settings (/secrets.py), and it cannot reach the daemon without a network. Nothing "
    "was written. Prepare the download again with 'Set the Wi-Fi network on the "
    "Presto' ticked.")


def reach_message(reach, host=""):
    if reach == "dns":
        bare = "." not in host
        return ("The Presto could not look up the daemon's name (%s). %s Prepare a new "
                "download in the admin page with the daemon's IP address or its full name, "
                "and run this script again." % (
                    host, "A computer often adds its network's domain to a short name, "
                    "but the Presto does not, so use the full name (with its domain)."
                    if bare else "Check the name, or use the IP address."))
    if reach == "unauthorised":
        return ("The Presto reached the daemon, but the daemon refused the device token. "
                "Prepare a new download in the admin page and run this script again.")
    if reach == "wifi":
        return ("The Presto could not join the Wi-Fi network just now, so it could not look "
                "for the daemon. The settings are saved and the Presto keeps trying on its "
                "own. If you entered a new network, check its name and password (2.4 GHz "
                "only); a weak signal can also cause this. Run this script again to retry.")
    if reach.startswith("http_"):
        return ("The Presto reached a server on that address, but it did not answer as the "
                "daemon. Check that the port is the daemon's device port (8787 by default).")
    return ("The Presto could not reach the daemon. Check the address and port, that the "
            "daemon is running, and that the device joined the Wi-Fi network.")


SKIP_HINT = (" If you are sure the address is right and this computer is on a different "
             "network from the Presto, run this script again with --skip-daemon-check.")


def computer_message(result, host, port):
    """Why this computer could not find the daemon, before any USB device is touched."""
    where = "%s:%d" % (host, port)
    if result == "dns":
        text = ("This computer could not look up the daemon's address (%s). Check it for a "
                "typo, or prepare a new download in the admin page with the daemon's IP "
                "address." % host)
    elif result == "refused":
        text = ("Nothing is listening at %s. Check that the daemon is running and that %d "
                "is its device port (8787 by default), then prepare a new download if the "
                "address or port was wrong." % (where, port))
    elif result == "timeout":
        text = ("%s did not answer. Check the address, that the daemon computer is on and "
                "running the daemon, and that its firewall allows port %d." % (where, port))
    else:
        text = ("Something answered at %s, but it is not the daemon's device port. Check "
                "the address and the port (8787 by default), then prepare a new download."
                % where)
    return text + " Nothing was written." + SKIP_HINT


def precheck_message(reach, host):
    """Why the Presto could not find the daemon with the settings about to be written."""
    if reach == "wifi":
        return ("The Presto could not join the Wi-Fi network just now, so it could not look "
                "for the daemon. Nothing was written. Check the network name and password "
                "(2.4 GHz only); a weak signal can also cause this. Run this script again "
                "to retry." + SKIP_HINT)
    return reach_message(reach, host) + " Nothing was written." + SKIP_HINT


def remove_handoff(path, say):
    try:
        os.remove(path)
    except OSError:
        say("Setup succeeded. Remove %s yourself; it holds a copy of the device token."
            % HANDOFF_NAME)
        return
    say("Removed %s. Setup is complete." % HANDOFF_NAME)


def masked_input(text):
    """Read a secret, showing one * per character so a paste is seen to have landed.

    Falls back to ``getpass``, which shows nothing, when there is no real terminal.
    """
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return getpass.getpass(text)
    try:
        return _masked_windows(text) if os.name == "nt" else _masked_posix(text)
    except (ImportError, OSError):
        return getpass.getpass(text)


def _masked_windows(text):
    import msvcrt
    sys.stdout.write(text)
    sys.stdout.flush()
    typed = []
    while True:
        char = msvcrt.getwch()
        if char in ("\r", "\n"):
            break
        if char == "\x03":
            raise KeyboardInterrupt
        if char in ("\x00", "\xe0"):
            msvcrt.getwch()  # an arrow or function key sends two codes: ignore both
            continue
        if char == "\b":
            if typed:
                typed.pop()
                sys.stdout.write("\b \b")
                sys.stdout.flush()
            continue
        typed.append(char)
        sys.stdout.write("*")
        sys.stdout.flush()
    sys.stdout.write("\n")
    return "".join(typed)


def _masked_posix(text):
    import termios
    import tty
    descriptor = sys.stdin.fileno()
    saved = termios.tcgetattr(descriptor)
    sys.stdout.write(text)
    sys.stdout.flush()
    typed = []
    try:
        tty.setcbreak(descriptor)
        while True:
            char = sys.stdin.read(1)
            if char in ("\r", "\n", ""):
                break
            if char == "\x03":
                raise KeyboardInterrupt
            if char in ("\x7f", "\b"):
                if typed:
                    typed.pop()
                    sys.stdout.write("\b \b")
                    sys.stdout.flush()
                continue
            typed.append(char)
            sys.stdout.write("*")
            sys.stdout.flush()
    finally:
        termios.tcsetattr(descriptor, termios.TCSADRAIN, saved)
        sys.stdout.write("\n")
    return "".join(typed)


def prompt(text, secret=False):
    if secret:
        return masked_input(text)
    return input(text)


def parse_arguments(argv):
    parser = argparse.ArgumentParser(
        prog="presto_setup.py",
        description="Install and configure Presto AI Dash on a USB-connected Presto.")
    parser.add_argument("--handoff", help="path to %s (default: beside this script)"
                        % HANDOFF_NAME)
    parser.add_argument("--port", help="USB port, for example COM3 or /dev/ttyACM0")
    parser.add_argument("--name", help="name this Presto reports to the daemon")
    parser.add_argument("--skip-daemon-check", action="store_true",
                        help="do not check that the daemon answers before changing the "
                             "Presto (for a computer on a different network from the Presto)")
    return parser.parse_args(argv)


def dependencies_present():
    return all(importlib.util.find_spec(name) is not None
               for name in ("cryptography", "mpremote"))


def private_environment_python(folder):
    if os.name == "nt":
        return os.path.join(folder, "Scripts", "python.exe")
    return os.path.join(folder, "bin", "python")


def run_in_private_environment(argv, ask, say):
    """Install the two packages into a temporary environment and run this script there.

    Asks first. Returns the exit code of the run inside it. Nothing it installs
    survives the run.
    """
    if os.environ.get(BOOTSTRAP_ENV):
        raise SetupStopped(DEPENDENCY_MESSAGE)
    say(MISSING_PACKAGES_MESSAGE)
    try:
        answer = str(ask("Install them now into a private, temporary environment? [Y/n] "))
    except EOFError:
        # No terminal to answer on (piped or closed input): that is a no.
        answer = "n"
    if answer.strip().lower() not in ("", "y", "yes"):
        raise SetupStopped(DEPENDENCY_MESSAGE)
    folder = tempfile.mkdtemp(prefix="presto-setup-env-")
    try:
        say("Creating the environment and installing the packages...")
        python = private_environment_python(folder)
        try:
            subprocess.run([sys.executable, "-m", "venv", folder], check=True,
                           capture_output=True)
            subprocess.run([python, "-m", "pip", "install", "--disable-pip-version-check",
                            "--quiet", *REQUIRED_PACKAGES], check=True, capture_output=True)
        except (OSError, subprocess.SubprocessError):
            raise SetupStopped("The packages could not be installed automatically. That "
                               "needs the internet and Python's venv module.\n\n"
                               + DEPENDENCY_MESSAGE) from None
        say("Installed. Starting the setup inside it.\n")
        sys.stdout.flush()
        environment = dict(os.environ)
        environment[BOOTSTRAP_ENV] = "1"
        return subprocess.call([python, os.path.abspath(__file__), *argv], env=environment)
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def main(argv=None, device_factory=None, ask=None, say=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    arguments = parse_arguments(argv)
    ask = ask or prompt
    say = say or (lambda text: print(text))
    try:
        check_handoff(find_handoff(arguments.handoff))
        if device_factory is None and not dependencies_present():
            return run_in_private_environment(argv, ask, say)
        run(arguments, device_factory or open_device, ask, say)
        return 0
    except Cancelled:
        say("Nothing was written to the device.")
        return 2
    except (SetupStopped, DeviceError) as exc:
        say(str(exc))
        return 1
    except KeyboardInterrupt:
        say("Setup stopped. Nothing further was written; run this script again to retry.")
        return 1
    except Exception as exc:
        # Never print an exception's text: it can carry a path, a token or a
        # response body. Its type and the place it was raised say what happened.
        frame = traceback.extract_tb(exc.__traceback__)[-1]
        say("Setup stopped after an unexpected error (%s in %s, line %d). Reconnect the "
            "Presto and run this script again; the encrypted setup file was kept for a "
            "retry." % (type(exc).__name__, frame.name, frame.lineno))
        return 1


if __name__ == "__main__":
    sys.exit(main())
