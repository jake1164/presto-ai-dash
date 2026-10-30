#!/usr/bin/env python3
"""Presto AI Dash - set up or repair one Pimoroni Presto over USB.

Run this file on the computer the Presto is plugged into by a data-capable USB
cable. It carries the exact public Presto app files of the daemon build that
served it, so the device gets the app its daemon expects.

What it does, in order:

1. reads ``presto-setup.enc`` from this folder (or ``--handoff``);
2. asks for the setup unlock code shown in the daemon's admin page, privately,
   and decrypts the handoff in memory only;
3. lists the USB serial devices and asks which one is the Presto - it never
   chooses a board by itself;
4. checks the board really is a Pimoroni Presto before writing anything;
5. keeps the device's existing settings unless you explicitly replace them,
   and keeps a stable random device identifier outside the app files;
6. shows what will change, installs the app, verifies every file by SHA-256,
   starts the dashboard and checks that the device reaches the daemon.

On success it deletes ``presto-setup.enc``. On failure it keeps it, so the
same download can be retried; ``presto_setup.py`` never deletes anything else.
The setup unlock code is never written to this computer or to the device, and
the Wi-Fi password is only ever typed here and written to the device's
``/secrets.py``.

Nothing is written to the device before the handoff has decrypted and its app
revision has matched this script. Errors are reported as fixed text: a
traceback, a token or a Wi-Fi password is never printed.
"""

import argparse
import base64
import binascii
import getpass
import hashlib
import json
import os
import re
import sys
import time

SETUP_PROTOCOL = 1
REVISION_CHARS = 12
APP_FOLDER = "/ai_dash_files"
CONFIG_PATH = "/ai_dash_files/config.py"
SECRETS_PATH = "/secrets.py"
HANDOFF_NAME = "presto-setup.enc"
REVISION_TARGET = "/ai_dash_files/revision.txt"
# Present only on a Presto that was moved to the bootstrap and A/B app slots.
BOOT_ENTRY = "/ai_dash_boot.py"

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

PROBE_CODE = r'''
import os, sys, json
machine = ''
try:
    machine = os.uname().machine
except Exception:
    pass
print('PRESTO_SETUP:' + json.dumps({'micropython': sys.implementation.name == 'micropython', 'machine': machine}))
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

REACH_CODE = r'''
import sys, json
sys.path.insert(0, '/ai_dash_files')
# This runs in the session that already imported the old settings to read the
# device identifier. Without this, the check would test the old daemon address.
for name in ('config', 'secrets'):
    sys.modules.pop(name, None)
result = 'error'
try:
    import config
    import secrets
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
        wlan.connect(secrets.WIFI_SSID, secrets.WIFI_PASSWORD)
        for _ in range(48):
            if wlan.isconnected():
                break
            time.sleep(0.25)
        if wlan.isconnected():
            break
    if not wlan.isconnected():
        result = 'wifi'
    else:
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
            code = int(head.split(b' ')[1]) if b'\r\n' in head else 0
            if code == 200:
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
    if not isinstance(payload, dict) or set(payload) != {
            "v", "daemon", "device", "app", "release", "created_at"}:
        raise SetupStopped("The setup file's contents are not a setup configuration.")
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
    """The board's identity, after refusing anything that is not a Presto."""
    state = marker_json(device.run(PROBE_CODE.encode("ascii")), "board")
    machine = safe_text(state.get("machine"))
    if not state.get("micropython") or "Presto" not in machine:
        raise SetupStopped("The device on that port is not a Pimoroni Presto (%s). "
                           "Nothing was written." % machine)
    return machine


def config_state(device):
    """Only the identity a repair must keep: never the token or any other value."""
    return marker_json(device.run(CONFIG_STATE_CODE.encode("ascii")), "settings")


def check_reachability(device):
    """Run the app's own read on the device and report a fixed result code."""
    state = marker_json(device.run(REACH_CODE.encode("ascii"), timeout=180), "connection")
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


def sha256_hex(data):
    return hashlib.sha256(bytes(data)).hexdigest()


# -- installing ------------------------------------------------------------

def app_paths(device):
    """Every path the app owns: the bundle's and any stale module beside it."""
    paths = list(BUNDLE["hashes"])
    for name in device.listdir(APP_FOLDER):
        lower = name.lower()
        if lower.endswith((".py", ".mpy")) and not lower.startswith(("config.", "secrets.")):
            path = APP_FOLDER + "/" + name
            if path not in paths:
                paths.append(path)
    return paths


def install_app(device, say):
    """Stage, verify, then replace, with a rollback when any step fails."""
    hashes = BUNDLE["hashes"]
    for path, data in BUNDLE["files"].items():
        device.write(path + ".presto-new", data)
    for path, wanted in hashes.items():
        if device.digest(path + ".presto-new") != wanted:
            raise SetupStopped("A file did not arrive intact. No installed app file was "
                               "replaced; reconnect the Presto and run this script again.")
    # A module left over from an older build is retired, but only on a device
    # that already runs AI Dash: on a first install, an unrelated file in the
    # app folder belongs to the person, not to this app.
    retire = device.exists(REVISION_TARGET)
    extras = ([path for path in app_paths(device) if path not in hashes]
              if retire else [])
    changed = []
    try:
        for path in list(hashes) + extras:
            backup = path + ".presto-prev"
            if device.exists(backup):
                device.remove(backup)
            had_old = device.exists(path)
            if had_old:
                device.rename(path, backup)
            changed.append((path, had_old))
            if path in hashes:
                device.rename(path + ".presto-new", path)
        actual = {path: device.digest(path) for path in app_paths(device)}
        for path, wanted in hashes.items():
            if actual.get(path) != wanted:
                raise SetupStopped("The installed app did not verify. The previous app was "
                                   "put back; reconnect the Presto and run this script again.")
        if retire and set(actual) != set(hashes):
            raise SetupStopped("A stale app file could not be retired. The previous app was "
                               "put back; reconnect the Presto and run this script again.")
    except Exception:
        for path, had_old in reversed(changed):
            try:
                if device.exists(path):
                    device.remove(path)
                if had_old:
                    device.rename(path + ".presto-prev", path)
            except Exception:
                pass
        raise
    for path in list(hashes) + extras:
        backup = path + ".presto-prev"
        if device.exists(backup):
            device.remove(backup)
    say("Installed %d app files and verified every one by SHA-256." % len(hashes))


def write_private(device, path, data):
    """Write a settings file safely, with the previous one kept until verified."""
    staging, backup = path + ".presto-new", path + ".presto-prev"
    wanted = sha256_hex(data)
    device.write(staging, data)
    if device.digest(staging) != wanted:
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
        if device.digest(path) != wanted:
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


def ask_settings(device, payload, requested_name, ask, say):
    """Decide what to keep; nothing is read that does not have to be."""
    config_exists = device.exists(CONFIG_PATH)
    secrets_exists = device.exists(SECRETS_PATH)
    wifi = payload["device"].get("wifi")
    if wifi == "keep" and not secrets_exists:
        raise SetupStopped(NO_WIFI_MESSAGE)
    existing = config_state(device) if config_exists else {}
    replace_config = True
    if config_exists:
        say("This Presto already has /ai_dash_files/config.py.")
        replace_config = yes(ask("Replace its daemon address, token and device ID "
                                 "(the existing file is kept until the new one verifies)? [y/N] "))
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
    kept_id = bool(existing.get("device_id"))
    device_id = existing.get("device_id") or new_device_id()
    return {"config": replace_config, "secrets": replace_secrets,
            "ssid": ssid, "password": password, "name": name, "device_id": device_id,
            "kept_id": kept_id}


def preview(payload, plan, port, machine, say):
    total = sum(len(data) for data in BUNDLE["files"].values())
    daemon = payload["daemon"]
    say("")
    say("Presto AI Dash %s (app revision %s) on %s (%s):"
        % (RELEASE or "unknown", str(BUNDLE["revision"])[:REVISION_CHARS], port, machine))
    if plan.get("slot"):
        say("  - App files: left alone. This Presto updates its app over the air.")
    else:
        say("  - %d app files, %.1f KiB, installed in /ai_dash_files and as /ai_dash.py,"
            % (len(BUNDLE["files"]), total / 1024.0))
        say("    verified by SHA-256 and rolled back if a write fails.")
    if plan["config"]:
        say('  - /ai_dash_files/config.py: replace with daemon %s:%d, name "%s", %s device ID.'
            % (daemon["host"], int(daemon["port"]), plan["name"],
               "existing" if plan.get("kept_id") else "new"))
    else:
        say("  - /ai_dash_files/config.py: keep the existing file.")
    if plan["secrets"]:
        say("  - /secrets.py: replace with the Wi-Fi network you entered.")
    else:
        say("  - /secrets.py: keep the existing file.")
    say("  - Firmware, the stock launcher and every other file are left alone.")
    say("")


def apply_plan(device, payload, plan, say):
    if not plan.get("slot"):
        install_app(device, say)
    if plan["config"]:
        write_private(device, CONFIG_PATH, config_text(payload, plan["name"], plan["device_id"]))
        say("Wrote /ai_dash_files/config.py and verified it.")
    if plan["secrets"]:
        write_private(device, SECRETS_PATH, secrets_text(plan["ssid"], plan["password"]))
        say("Wrote /secrets.py and verified it.")
    if not device.exists(CONFIG_PATH):
        raise SetupStopped("The device has no /ai_dash_files/config.py, so it cannot reach "
                           "the daemon. Run this script again and replace its settings.")


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
    for _attempt in range(UNLOCK_ATTEMPTS):
        code = ask("Setup unlock code: ", secret=True)
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


def run(arguments, device_factory, ask, say):
    handoff_path = find_handoff(arguments.handoff)
    if not BUNDLE.get("revision"):
        raise SetupStopped("This setup script has no Presto app files. Download it again "
                           "from the daemon's admin page.")
    say("Presto AI Dash %s - setting up a Presto." % (RELEASE or "unknown"))
    payload = read_handoff(handoff_path, ask, say)
    check_bundle(payload)
    say("Opened the setup file for daemon %s:%d."
        % (payload["daemon"]["host"], int(payload["daemon"]["port"])))
    ports = list_ports()
    port = choose_port(ports, arguments.port, ask, say)
    device = device_factory(port)
    wrote = False
    completed = False
    try:
        machine = probe_board(device)
        # A Presto that updates its app over the air keeps its app: only its
        # settings change, and the app never comes from this script.
        slot = device.exists(BOOT_ENTRY)
        if slot:
            say("This Presto updates its app over the air, so only its settings will change.")
            device.reset_to_launcher()
        plan = ask_settings(device, payload, arguments.name, ask, say)
        plan["slot"] = slot
        preview(payload, plan, port, machine, say)
        if not yes(ask("Type yes to install: ")):
            raise Cancelled()
        wrote = True
        apply_plan(device, payload, plan, say)
        say("Checking that the Presto can reach the daemon...")
        reach = check_reachability(device)
        device.launch()
        if reach != "ok":
            raise SetupStopped(reach_message(reach))
        say("The Presto reached the daemon. AI Dash is running; it will appear in the "
            "admin page's Status tab on its next poll.")
        remove_handoff(handoff_path, say)
        completed = True
    finally:
        device.close()
        if wrote and not completed:
            say("The encrypted setup file was kept, so you can run this script again.")


NO_WIFI_MESSAGE = (
    "The admin page was set not to change the Wi-Fi, but this Presto has no Wi-Fi "
    "settings (/secrets.py), and it cannot reach the daemon without a network. Nothing "
    "was written. Prepare the download again with 'Set the Wi-Fi network on the "
    "Presto' ticked.")


def reach_message(reach):
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


def remove_handoff(path, say):
    try:
        os.remove(path)
    except OSError:
        say("Setup succeeded. Remove %s yourself; it holds a copy of the device token."
            % HANDOFF_NAME)
        return
    say("Removed %s. Setup is complete." % HANDOFF_NAME)


def prompt(text, secret=False):
    if secret:
        return getpass.getpass(text)
    return input(text)


def parse_arguments(argv):
    parser = argparse.ArgumentParser(
        prog="presto_setup.py",
        description="Install and configure Presto AI Dash on a USB-connected Presto.")
    parser.add_argument("--handoff", help="path to %s (default: beside this script)"
                        % HANDOFF_NAME)
    parser.add_argument("--port", help="USB port, for example COM3 or /dev/ttyACM0")
    parser.add_argument("--name", help="name this Presto reports to the daemon")
    return parser.parse_args(argv)


def main(argv=None, device_factory=None, ask=None, say=None):
    arguments = parse_arguments(sys.argv[1:] if argv is None else list(argv))
    ask = ask or prompt
    say = say or (lambda text: print(text))
    try:
        run(arguments, device_factory or open_device, ask, say)
        return 0
    except Cancelled:
        say("Nothing was written to the device.")
        return 2
    except SetupStopped as exc:
        say(str(exc))
        return 1
    except KeyboardInterrupt:
        say("Setup stopped. Nothing further was written; run this script again to retry.")
        return 1
    except Exception:
        # Never print an exception's text: it can carry a path, a token or a
        # response body. A fixed sentence and a retry is the whole answer.
        say("Setup stopped after an unexpected error. Reconnect the Presto and run this "
            "script again; the encrypted setup file was kept for a retry.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
