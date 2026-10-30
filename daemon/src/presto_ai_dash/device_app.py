"""Serve the running daemon's own Presto dashboard to a Presto that asks.

This is the only place a device can fetch executable bytes, and it is narrow on
purpose:

* it serves **only** the ``slot-v1`` bundle of the running daemon, verified
  from the installed package at first use;
* it serves a manifest whose every entry is a canonical module name with an
  exact size and SHA-256, and whose claimed revision is recomputed from those
  hashes;
* it serves one file per request, with no redirect, no directory listing, no
  query parameter and no user-supplied path;
* it answers ``Cache-Control: no-store``, like every other authenticated
  device route.

The shared bearer token authenticates the *account*, not the code: the device
must independently authenticate the bytes through the measured TLS path (see
``presto/updater.py``). Nothing here is an attestation that a device installed
anything.
"""

import hashlib
import json
import logging
import re

log = logging.getLogger(__name__)

MANIFEST_PATH = "/api/v1/device-app/manifest"
FILE_PATH = r"^/api/v1/device-app/file/([A-Za-z0-9_]+\.py)$"
# The largest single slot file; a response that would exceed it is refused
# rather than streamed, because a 128 KiB module is already far larger than
# anything this app ships.
MAX_FILE_BYTES = 131072
MAX_FILES = 40
# Generated per slot by the installer, never downloaded.
GENERATED = ("revision.txt", "revision.sha256")
SLOT_NAME = re.compile(r"^[A-Za-z0-9_]+\.py$")
HEX = "0123456789abcdef"


def _is_hash(value):
    if not isinstance(value, str) or len(value) != 64:
        return False
    for char in value:
        if char not in HEX:
            return False
    return True


def slot_revision(hashes):
    """The ``slot-v1`` revision, byte for byte as the device computes it."""
    encoded = json.dumps(hashes, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def slot_inventory(bundle):
    """A verified inventory of the running bundle's slot files, or None.

    Accepts the two shapes the package uses (a checkout's bare names and a
    wheel's leading-slash paths) and normalises to bare slot-relative names.
    """
    if not isinstance(bundle, dict):
        return None
    slot = bundle.get("slot")
    if not isinstance(slot, dict) or slot.get("scheme") != "slot-v1":
        return None
    raw = slot.get("files")
    if not isinstance(raw, dict) or not raw:
        return None
    files = {}
    for name, data in raw.items():
        key = str(name).lstrip("/")
        if key in GENERATED:
            continue
        if not SLOT_NAME.match(key) or key in files:
            return None
        if not isinstance(data, bytes) or not data or len(data) > MAX_FILE_BYTES:
            return None
        files[key] = data
    if not files or len(files) > MAX_FILES:
        return None
    if "ai_dash_app.py" not in files:
        # The entry module is what a slot is for; without it nothing runs.
        return None
    hashes = {name: hashlib.sha256(data).hexdigest() for name, data in files.items()}
    revision = slot_revision(hashes)
    if revision != slot.get("revision"):
        # The bundle does not describe itself consistently, so it is not a
        # target anyone may be told to install.
        return None
    min_bootstrap = slot.get("min_bootstrap")
    if type(min_bootstrap) is not int or not 1 <= min_bootstrap <= 99:
        return None
    return {"revision": revision, "bootstrap": min_bootstrap,
            "files": files, "hashes": hashes}


def manifest_bytes(inventory):
    """The manifest a device validates before it downloads anything."""
    entries = [{"path": name, "size": len(inventory["files"][name]),
                "sha256": inventory["hashes"][name]}
               for name in sorted(inventory["files"])]
    return json.dumps({"v": 1, "scheme": "slot-v1",
                       "revision": inventory["revision"],
                       "bootstrap": inventory["bootstrap"],
                       "files": entries}, separators=(",", ":")).encode("utf-8")


class DeviceApp(object):
    """The running daemon's slot bundle, read once and then cached.

    A bundle that cannot be verified is not served at all: a device asking for
    a manifest receives ``404`` and can only stay on its current app.
    """

    def __init__(self, provider=None):
        if provider is None:
            from .device_install import app_snapshot
            provider = app_snapshot
        self._provider = provider
        self._inventory = None
        self._resolved = False

    def inventory(self):
        if self._resolved:
            return self._inventory
        try:
            self._inventory = slot_inventory(self._provider())
        except Exception:
            log.debug("the device app bundle could not be read", exc_info=True)
            self._inventory = None
        self._resolved = self._inventory is not None
        return self._inventory

    def manifest(self):
        inventory = self.inventory()
        return None if inventory is None else manifest_bytes(inventory)

    def file(self, name):
        """One slot file, or None. The name must come from the fixed route."""
        inventory = self.inventory()
        if inventory is None or not isinstance(name, str):
            return None
        return inventory["files"].get(name)
