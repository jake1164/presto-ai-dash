"""The device's updater: what may be executed, and how it is staged.

Every rule here exists because a bearer token and a SHA-256 received over plain
HTTP are not code authentication. Nothing in this module executes downloaded
code; it validates a manifest, streams the bytes into the inactive slot in
bounded chunks, verifies them, and then hands the decision to the durable
journal in ``activation.py``.

The module is pure Python with injected seams (filesystem, transport, clock,
firmware capabilities), so the tests drive it unchanged on a development
machine. On the device the seams are the measured primitives from the hardware
report: ``ssl.SSLContext`` with ``CERT_REQUIRED``, an explicit CA, an explicit
hostname, ``os.statvfs``, ``os.rename`` and SHA-256.

Normal unattended OTA is opt-in: ``Updater.enabled`` is false unless a caller
passes otherwise, and the app requires an explicit matching-firmware profile.
"""

import binascii
import hashlib
import json
import re
import time
import gc

# The wire version of the manifest this module understands.
MANIFEST_VERSION = 1
SCHEME_SLOT = "slot-v1"
MAX_MANIFEST_BYTES = 8192
MAX_SLOT_FILES = 40
MAX_FILE_BYTES = 131072
# One read at a time. The 262,144-byte write measured 35.9 seconds on the
# measured build, so a bigger buffer would not make the attempt shorter, it
# would only make each stalled read longer.
CHUNK_BYTES = 1024
# Verified files wait in RAM and are committed to flash together, so the
# display is stopped once per batch instead of once per chunk (see
# ``Transfer.flash``). A whole current bundle fits in one batch; a larger one
# is committed in several, which bounds the RAM held at once.
FLASH_BATCH_BYTES = 393216
# One flash write call, so a caller can feed the watchdog between them.
FLASH_WRITE_BYTES = 4096
# A whole attempt bound, from the measured write cost plus the TLS stream, not
# from the daemon's offline threshold.
MAX_ATTEMPT_SECONDS = 900
# The containment margin used by the hardware fixtures. It is a starting point,
# not a validated production free-space policy: the deployment gate replaces it
# with a measured value.
SAFETY_MARGIN_BYTES = 1572864
# Earliest calendar time this updater treats as usable: 2025-01-01T00:00:00Z.
# A board that still thinks it is 2021 must fail closed, as measured.
MIN_USABLE_EPOCH = 1735689600
# Files a slot may contain: canonical module names only.
SLOT_NAME = re.compile(r"^[A-Za-z0-9_]+\.py$")
HEX = "0123456789abcdef"


def _digest(data):
    return binascii.hexlify(hashlib.sha256(bytes(data)).digest()).decode()


def _is_hash(value):
    if not isinstance(value, str) or len(value) != 64:
        return False
    for char in value:
        if char not in HEX:
            return False
    return True


def revision_of(hashes):
    """The ``slot-v1`` revision of a {slot-relative name: sha256} mapping.

    Byte for byte the same serializer as ``tools/presto_bundle.slot_snapshot``
    and ``device_install``, so a manifest that disagrees with its own files is
    caught before anything is activated.
    """
    # Names and values have already passed the ASCII module/hash validators.
    # MicroPython has no sort_keys keyword, so construct the canonical mapping.
    encoded = ("{" + ",".join('"%s":"%s"' % (name, hashes[name])
                              for name in sorted(hashes)) + "}").encode()
    return _digest(encoded)


class ManifestError(ValueError):
    """A manifest that must not be used. ``code`` is the fixed report code."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


def parse_manifest(text):
    """A validated manifest: ``{revision, bootstrap, files: [(name, size, sha)]}``.

    Anything unexpected is refused whole. The revision is recomputed from the
    file hashes, so a manifest cannot claim a target its own contents do not
    produce.
    """
    if not isinstance(text, str) or not text or len(text) > MAX_MANIFEST_BYTES:
        raise ManifestError("manifest_invalid")
    try:
        data = json.loads(text)
    except Exception:
        raise ManifestError("manifest_invalid")
    if not isinstance(data, dict):
        raise ManifestError("manifest_invalid")
    if set(data) != {"v", "scheme", "revision", "bootstrap", "files"}:
        raise ManifestError("manifest_invalid")
    if type(data["v"]) is not int or data["v"] != MANIFEST_VERSION or data["scheme"] != SCHEME_SLOT:
        raise ManifestError("manifest_invalid")
    if not _is_hash(data["revision"]):
        raise ManifestError("manifest_invalid")
    bootstrap = data["bootstrap"]
    if type(bootstrap) is not int or not 1 <= bootstrap <= 99:
        raise ManifestError("manifest_invalid")
    raw = data["files"]
    if not isinstance(raw, list) or not raw or len(raw) > MAX_SLOT_FILES:
        raise ManifestError("manifest_invalid")
    files = []
    seen = set()
    for entry in raw:
        if not isinstance(entry, dict) or set(entry) != {"path", "size", "sha256"}:
            raise ManifestError("manifest_invalid")
        name = entry["path"]
        if (not isinstance(name, str) or name != name.strip() or not SLOT_NAME.match(name) or name in seen
                or "secret" in name.lower() or name.lower().startswith("config")):
            raise ManifestError("manifest_invalid")
        size = entry["size"]
        if type(size) is not int or not 0 < size <= MAX_FILE_BYTES:
            raise ManifestError("manifest_invalid")
        if not _is_hash(entry["sha256"]):
            raise ManifestError("manifest_invalid")
        seen.add(name)
        files.append((name, size, entry["sha256"]))
    # The generated revision files are written locally, never downloaded.
    if "revision.txt" in seen or "revision.sha256" in seen:
        raise ManifestError("manifest_invalid")
    if revision_of({name: sha for name, _, sha in files}) != data["revision"]:
        raise ManifestError("hash_mismatch")
    if "ai_dash_app.py" not in seen:
        raise ManifestError("manifest_invalid")
    # The list is not reordered: MicroPython's sort is not stable, and the
    # order of the files means nothing. Its total is what preflight needs.
    return {"revision": data["revision"], "bootstrap": bootstrap, "files": files,
            "total": sum(size for _, size, _ in files)}


def manifest_text(manifest):
    """The canonical manifest text for a bundle, used by the daemon and tests."""
    files = [{"path": name, "size": size, "sha256": sha}
             for name, size, sha in manifest["files"]]
    return json.dumps({"v": MANIFEST_VERSION, "scheme": SCHEME_SLOT,
                       "revision": manifest["revision"],
                       "bootstrap": manifest["bootstrap"], "files": files},
                      separators=(",", ":"))


def trust_error(transport, clock):
    """Why executable bytes may not be fetched from this transport, or None.

    A transport must be verified TLS with an explicit trust root and an
    explicit hostname, and the board's clock must be usable. This is the
    measured route: the firmware's ``ssl`` compatibility layer rejects a wrong
    or missing CA, a wrong hostname, and bad dates. There is no fallback to
    plain HTTP and none to ``CERT_NONE``.
    """
    if transport is None:
        return "transport_untrusted"
    if getattr(transport, "trusted", False) is not True:
        return "transport_untrusted"
    hostname = getattr(transport, "hostname", None)
    if not isinstance(hostname, str) or not hostname:
        return "transport_untrusted"
    try:
        now = clock()
    except Exception:
        return "transport_untrusted"
    if type(now) not in (int, float) or not now >= MIN_USABLE_EPOCH:
        return "transport_untrusted"
    return None


def preflight(manifest, journal, firmware, free_bytes, ota_enabled):
    """Whether this device may start the transfer, or the fixed blocking code.

    A blocking answer never spends a transfer attempt; it becomes
    ``manual_action`` with USB instructions.
    """
    if not ota_enabled:
        return "firmware_unsupported"
    if not isinstance(firmware, dict) or not (firmware.get("sha256")
                                              and firmware.get("rename")
                                              and firmware.get("statvfs")
                                              and firmware.get("watchdog")):
        return "firmware_unsupported"
    if journal.get("pending") is not None or journal.get("transfer") is not None:
        # An unresolved activation must be reconciled before another update.
        return "local_state_invalid"
    installed = journal.get("bootstrap")
    if type(installed) is not int or installed < manifest["bootstrap"]:
        return "bootstrap_incompatible"
    if type(free_bytes) is not int or free_bytes - manifest["total"] < SAFETY_MARGIN_BYTES:
        return "storage_insufficient"
    return None


def command_error(command, manifest):
    """Whether a permit and the manifest the device actually fetched agree."""
    if not isinstance(command, dict) or command.get("kind") != "start_update":
        return "manifest_invalid"
    if command.get("target_scheme") != SCHEME_SLOT:
        return "manifest_invalid"
    if command.get("target_revision") != manifest["revision"]:
        return "manifest_invalid"
    if (set(command) != {"id", "kind", "generation", "attempt", "attempt_no",
                         "target_scheme", "target_revision", "manifest_path"}
            or command["manifest_path"] != "/api/v1/device-app/manifest"
            or type(command["attempt_no"]) is not int or not 1 <= command["attempt_no"] <= 3):
        return "manifest_invalid"
    for name in ("id", "generation", "attempt"):
        value = command[name]
        if not isinstance(value, str) or len(value) != 32 or any(char not in HEX for char in value):
            return "manifest_invalid"
    return None


class Transfer(object):
    """A resumable, chunk-bounded staging of one manifest into one slot.

    ``step()`` performs **one** bounded read into RAM and returns None while
    there is more to do, or a terminal fixed error code. That is what lets the
    app advance a transfer between rendered frames instead of freezing the
    display for the whole download, and it is what the tests drive directly.

    ``step()`` never writes flash. On the measured firmware an internal-flash
    write hangs while the Presto display runs (see ``presto/main.py``), so each
    file is verified against its hash in RAM, and ``flash()`` commits verified
    files when ``flash_due`` says so. The caller stops the display around it.
    """

    def __init__(self, fs, transport, manifest, slot):
        self.fs = fs
        self.transport = transport
        self.manifest = manifest
        self.folder = "/ai_dash_slots/" + slot
        self._index = -1
        self._received = 0
        self._digest = None
        self._buffer = None
        self._chunks = None
        self._ready = []
        self._ready_bytes = 0
        self._downloaded = False
        self._error = None
        self._finished = False
        self._started_ms = self._milliseconds()
        self.metrics = {"steps": 0, "max_step_ms": 0, "elapsed_ms": 0,
                        "minimum_free_bytes": None, "bytes": 0,
                        "flash_windows": 0, "flash_ms": 0, "max_flash_ms": 0}

    @staticmethod
    def _milliseconds():
        ticks = getattr(time, "ticks_ms", None)
        return ticks() if ticks else int(time.monotonic() * 1000)

    @property
    def done(self):
        return self._finished

    @property
    def error(self):
        return self._error

    @property
    def downloaded(self):
        """Every file has arrived and matched its hash in RAM."""
        return self._downloaded

    @property
    def flash_due(self):
        """Whether ``flash()`` must run before the transfer can go on."""
        return (self._error is None and not self._finished and
                (self._downloaded or self._ready_bytes >= FLASH_BATCH_BYTES))

    def _open(self):
        name, size, sha = self.manifest["files"][self._index]
        try:
            # The exact size is known, so one allocation holds the whole file.
            self._buffer = bytearray(size)
        except MemoryError:
            return "storage_insufficient"
        try:
            stream = getattr(self.transport, 'stream_async', None) or self.transport.stream
            self._chunks = stream(name, CHUNK_BYTES)
        except Exception:
            self._buffer = None
            return "connection_lost"
        self._received = 0
        self._digest = hashlib.sha256()
        return None

    def _close(self):
        """Verify this file in RAM and queue it for the next flash commit."""
        name, size, sha = self.manifest["files"][self._index]
        data, self._buffer, self._chunks = self._buffer, None, None
        if self._received != size:
            return "transfer_timeout"
        if binascii.hexlify(self._digest.digest()).decode() != sha:
            return "hash_mismatch"
        self._ready.append((name, data))
        self._ready_bytes += size
        return None

    def step(self):
        started = self._milliseconds()
        difference = getattr(time, "ticks_diff", lambda a, b: a - b)
        if difference(started, self._started_ms) > MAX_ATTEMPT_SECONDS * 1000:
            return self._fail("transfer_timeout")
        try:
            return self._step()
        finally:
            now = self._milliseconds()
            self.metrics["steps"] += 1
            self.metrics["max_step_ms"] = max(self.metrics["max_step_ms"], difference(now, started))
            self.metrics["elapsed_ms"] = difference(now, self._started_ms)
            free = getattr(gc, "mem_free", None)
            if free:
                value = free()
                minimum = self.metrics["minimum_free_bytes"]
                self.metrics["minimum_free_bytes"] = value if minimum is None else min(value, minimum)

    def _step(self):
        if self._error is not None or self._finished:
            return self._error
        if self.flash_due:
            # Nothing to download until the verified files are committed.
            return None
        if self._chunks is None:
            self._index += 1
            if self._index >= len(self.manifest["files"]):
                self._downloaded = True
                return None
            error = self._open()
            if error:
                return self._fail(error)
            return None
        try:
            chunk = next(self._chunks)
        except StopIteration:
            error = self._close()
            return self._fail(error) if error else None
        except Exception:
            return self._fail("connection_lost")
        if not chunk:
            return None
        end = self._received + len(chunk)
        self.metrics["bytes"] += len(chunk)
        name, size, _sha = self.manifest["files"][self._index]
        if end > size:
            return self._fail("manifest_invalid")
        self._digest.update(chunk)
        self._buffer[self._received:end] = chunk
        self._received = end
        return None

    def flash(self, feed=None):
        """Commit the verified files to the inactive slot; finish after the last.

        The only part of a transfer that writes flash, run by the caller with
        the display stopped. It is bounded by the batch size, and ``feed`` is
        called between writes so a watchdog stays fed. Returns None or the
        terminal fixed error code, like ``step()``.
        """
        if self._error is not None or self._finished:
            return self._error
        feed = feed or (lambda: None)
        started = self._milliseconds()
        difference = getattr(time, "ticks_diff", lambda a, b: a - b)
        try:
            while self._ready:
                name, data = self._ready.pop(0)
                staging = self.folder + "/" + name + ".presto-new"
                view = memoryview(data)
                handle = self.fs.open(staging, "wb")
                try:
                    for start in range(0, len(data), FLASH_WRITE_BYTES):
                        handle.write(view[start:start + FLASH_WRITE_BYTES])
                        feed()
                finally:
                    handle.close()
                self.fs.replace(staging, self.folder + "/" + name)
                self._ready_bytes -= len(data)
                view = data = None
                feed()
            if self._downloaded:
                for _ in self._finalize_steps():
                    feed()
                self._finished = True
        except ManifestError as exc:
            return self._fail(exc.code)
        except Exception:
            return self._fail("storage_insufficient")
        finally:
            spent = difference(self._milliseconds(), started)
            self.metrics["flash_windows"] += 1
            self.metrics["flash_ms"] += spent
            self.metrics["max_flash_ms"] = max(self.metrics["max_flash_ms"], spent)
        return None

    def _fail(self, error):
        self._error = error
        self._finished = True
        self._buffer = None
        self._ready = []
        self._ready_bytes = 0
        try:
            self._chunks.close()
        except Exception:
            pass
        return error

    def _finalize_steps(self):
        """An inactive slot becomes eligible only with its exact inventory."""
        expected = set(name for name, _, _ in self.manifest["files"])
        for name in self.fs.listdir(self.folder):
            if "secret" in name.lower() or name.lower().startswith("config"):
                raise ManifestError("local_state_invalid")
            if name.endswith((".py", ".mpy", ".presto-new")) and name not in expected:
                self.fs.remove(self.folder + "/" + name)
                yield None
        for name, size, sha in self.manifest["files"]:
            hasher = hashlib.sha256()
            received = 0
            with self.fs.open(self.folder + "/" + name, "rb") as handle:
                while True:
                    chunk = handle.read(CHUNK_BYTES)
                    if not chunk:
                        break
                    received += len(chunk)
                    hasher.update(chunk)
                    yield None
            if received != size or binascii.hexlify(hasher.digest()).decode() != sha:
                raise ManifestError("hash_mismatch")
        actual = set(name for name in self.fs.listdir(self.folder) if name.endswith((".py", ".mpy")))
        if actual != expected:
            raise ManifestError("manifest_invalid")
        for name, value in (("revision.txt", self.manifest["revision"][:12]),
                            ("revision.sha256", self.manifest["revision"])):
            self.fs.write(self.folder + "/" + name + ".presto-new", (value + "\n").encode())
            self.fs.replace(self.folder + "/" + name + ".presto-new", self.folder + "/" + name)
            yield None
        self.fs.sync()


class Updater(object):
    """Stage, verify and activate one dashboard update.

    ``fs`` is a filesystem seam with ``exists``, ``read``, ``write``,
    ``replace``, ``remove`` and ``free_bytes``. ``transport`` fetches bytes
    from the running daemon. ``reset`` restarts the board so the bootstrap
    enters the candidate. Nothing is enabled unless the caller says so.
    """

    def __init__(self, fs, transport, clock, firmware=None, reset=None,
                 enabled=False, journal=None):
        self.fs = fs
        self.transport = transport
        self.clock = clock
        self.firmware = firmware or {}
        self.reset = reset
        self.enabled = bool(enabled)
        self.journal = journal

    # -- the attempt --------------------------------------------------------

    def readiness(self):
        if not self.enabled:
            return "firmware_unsupported"
        if not isinstance(self.firmware, dict) or not all(
                self.firmware.get(name) for name in ("sha256", "rename", "statvfs", "watchdog")):
            return "firmware_unsupported"
        return trust_error(self.transport, self.clock)

    def prepare(self, command, manifest_text_value, active_slot):
        """Validate the actual target and current gates before any file write."""
        error = self.readiness()
        if error:
            return None, error
        try:
            manifest = parse_manifest(manifest_text_value)
        except ManifestError as exc:
            return None, exc.code
        error = command_error(command, manifest)
        if error:
            return None, error
        if (self.journal is None or active_slot not in ("a", "b") or
                self.journal["active"] != active_slot or self.journal["confirmed"] != active_slot):
            return None, "local_state_invalid"
        error = preflight(manifest, self.journal, self.firmware, self.fs.free_bytes(), self.enabled)
        if error:
            return None, error
        return Transfer(self.fs, self.transport, manifest, "b" if active_slot == "a" else "a"), None

    def accept(self, command, manifest_text_value, active_slot):
        """Validate a permit and stage the opposite slot.

        Returns ``(journal, error)``. ``error`` is a fixed code, never an
        exception string, and the journal is unchanged when it is set.
        """
        transfer, error = self.prepare(command, manifest_text_value, active_slot)
        if error:
            return self.journal, error
        slot = "b" if active_slot == "a" else "a"
        manifest = transfer.manifest
        import activation
        accepted = activation.begin_transfer(self.journal, slot, manifest["revision"],
                                            command["generation"], command["attempt"], command["id"])
        activation.save(self.fs, accepted)
        self.journal = accepted
        while not transfer.done:
            error = transfer.flash() if transfer.flash_due else transfer.step()
            if error:
                failed = activation.record_failure(self.journal, "failed", error,
                    command["target_revision"], command["generation"], command["attempt"])
                activation.save(self.fs, failed)
                self.journal = failed
                return self.journal, error
        journal = _begin(self.journal, slot, manifest["revision"],
                         command.get("generation"), command.get("attempt"),
                         command.get("id"))
        activation.save(self.fs, journal)
        self.journal = journal
        if self.reset is not None:
            self.reset()
        return journal, None


def _begin(journal, slot, target, generation, attempt, command):
    """A local copy of ``activation.begin_candidate`` with the permit attached."""
    import activation
    return activation.begin_candidate(journal, slot, target, generation,
                                      attempt, command)


def manifest_for_bundle(files):
    """The manifest of a bundle's slot files: {name: bytes} -> validated dict."""
    hashes = {name: _digest(data) for name, data in files.items()
              if name not in ("revision.txt", "revision.sha256")}
    revision = revision_of(hashes)
    entries = [(name, len(files[name]), hashes[name]) for name in hashes]
    return {"revision": revision, "bootstrap": 1, "files": entries,
            "total": sum(size for _, size, _ in entries)}


def make_updater(fs, transport, clock, firmware, journal, reset=None,
                 enabled=True):
    """An :class:`Updater` only when every prerequisite is present, else None.

    This build ships no provisioned trust root and no verified TLS transport
    for the device, so the shipped app gets ``None`` and can start no
    transfer. Nothing here invents a trust root, guesses a hostname, or falls
    back to plain HTTP: a missing prerequisite means no updater at all.
    """
    if fs is None or journal is None:
        return None
    if transport is None or trust_error(transport, clock) is not None:
        return None
    if not isinstance(firmware, dict) or not all(
            firmware.get(name) for name in ("sha256", "rename", "statvfs", "watchdog")):
        return None
    return Updater(fs, transport, clock, firmware, reset, enabled=enabled,
                   journal=journal)
