"""Durable A/B activation state: which slot runs, and whether it is confirmed.

This is the one description of the activation journal, used by both the stable
bootstrap (which decides what to run) and the dashboard (which reports what it
is and confirms itself). It is pure Python with no hardware import, so CPython
runs it unchanged in the tests while MicroPython runs it on the device.

Three decisions are deliberately conservative:

* **A candidate gets one clean entry.** The bootstrap re-arms the stock
  carousel handoff before running an unconfirmed candidate, so a warm reset or
  a power cut that reaches the bootstrap again is the second entry, and a
  second entry rolls back to the confirmed slot. That is the measured fixture
  behaviour, not a guess.
* **Metadata is written through a temporary file, then a rename.** A crash
  between the two renames leaves the previous journal in the backup copy, and
  the reader falls back to it. A journal that cannot be read at all is treated
  as "no journal": the bootstrap then starts the slot it can trust most.
* **Failure metadata is kept until it is reconciled.** A reboot must not turn
  a failed update into a clean idle report, so the terminal attempt stays in
  the journal until the daemon settles it.
"""

import json
import os

# The journal format this module reads and writes.
JOURNAL_VERSION = 1
# The bootstrap compatibility version this journal was written for. A journal
# from a newer bootstrap is not interpreted; a device needing one needs USB.
BOOTSTRAP_VERSION = 1

# Everything durable lives here: outside both slots, outside the bootstrap
# files, and outside the private settings folder the setup utility writes.
STATE_FOLDER = "/ai_dash_state"
JOURNAL_PATH = STATE_FOLDER + "/journal.json"
JOURNAL_BACKUP = STATE_FOLDER + "/journal.bak"
JOURNAL_TEMP = STATE_FOLDER + "/journal.tmp"

SLOT_NAMES = ("a", "b")
MAX_COUNTER = 2147483647
HEX = "0123456789abcdef"
# How many times an unconfirmed candidate may be entered before it is rolled
# back. One: the first entry is the attempt, the next entry means it did not
# confirm and the previous slot runs again.
MAX_CANDIDATE_ENTRIES = 1

# The phases a journal may hold for the last attempt.
FAILED_PHASES = ("failed", "blocked", "rolled_back")
UPDATE_PHASES = ("idle", "downloading", "verifying", "activating", "confirming",
                 "succeeded", "rolled_back", "failed", "blocked")
ERROR_CODES = ("transfer_timeout", "connection_lost", "http_error",
               "storage_insufficient", "firmware_unsupported",
               "bootstrap_incompatible", "transport_untrusted",
               "manifest_invalid", "signature_invalid", "hash_mismatch",
               "boot_crash", "rollback_failed", "local_state_invalid",
               "unknown_error")

MAX_BYTES = 1024


def _is_id(value):
    if not isinstance(value, str) or len(value) != 32:
        return False
    for char in value:
        if char not in HEX:
            return False
    return True


def is_hash(value):
    """Whether a value is a full lowercase SHA-256, the only slot revision form."""
    if not isinstance(value, str) or len(value) != 64:
        return False
    for char in value:
        if char not in HEX:
            return False
    return True


def _is_slot(value):
    return value is None or value in SLOT_NAMES


def _counter(value):
    if type(value) is not int or not 1 <= value <= MAX_COUNTER:
        return None
    return value


def blank_journal(bootstrap=BOOTSTRAP_VERSION):
    """The journal of a device that has never been migrated or updated."""
    return {"v": JOURNAL_VERSION, "bootstrap": bootstrap, "boots": 1,
            "active": "a", "confirmed": "a", "pending": None,
            "failure": None, "success": None, "transfer": None}


def _pending(value):
    """A validated pending-activation record, or None for an explicit null."""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("pending is not an object")
    if set(value) != {"slot", "target", "generation", "attempt", "command", "tries"}:
        raise ValueError("pending has unexpected keys")
    if value["slot"] not in SLOT_NAMES or not is_hash(value["target"]):
        raise ValueError("pending slot or target")
    for name in ("generation", "attempt", "command"):
        if value[name] is not None and not _is_id(value[name]):
            raise ValueError("pending identifier")
    tries = value["tries"]
    if type(tries) is not int or not 0 <= tries <= 8:
        raise ValueError("pending tries")
    return {"slot": value["slot"], "target": value["target"],
            "generation": value["generation"], "attempt": value["attempt"],
            "command": value["command"], "tries": tries}


def _failure(value):
    """A validated terminal-attempt record, or None."""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("failure is not an object")
    if set(value) != {"phase", "error", "target", "generation", "attempt"}:
        raise ValueError("failure has unexpected keys")
    if value["phase"] not in FAILED_PHASES:
        raise ValueError("failure phase")
    if value["error"] not in ERROR_CODES:
        raise ValueError("failure error")
    if value["target"] is not None and not is_hash(value["target"]):
        raise ValueError("failure target")
    for name in ("generation", "attempt"):
        if value[name] is not None and not _is_id(value[name]):
            raise ValueError("failure identifier")
    return {"phase": value["phase"], "error": value["error"],
            "target": value["target"], "generation": value["generation"],
            "attempt": value["attempt"]}


def _success(value):
    """A validated successful-attempt record, or None."""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("success is not an object")
    if set(value) != {"target", "generation", "attempt"}:
        raise ValueError("success has unexpected keys")
    if not is_hash(value["target"]):
        raise ValueError("success target")
    for name in ("generation", "attempt"):
        if value[name] is not None and not _is_id(value[name]):
            raise ValueError("success identifier")
    return {"target": value["target"], "generation": value["generation"],
            "attempt": value["attempt"]}


def parse_journal(text):
    """A validated journal from stored JSON, or None when it is unusable.

    Nothing here echoes the input: a truncated or corrupt journal is reported
    as absent, and the caller recovers rather than trusting half a file.
    """
    if not isinstance(text, str) or not text or len(text) > MAX_BYTES:
        return None
    try:
        data = json.loads(text)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    keys = {"v", "bootstrap", "boots", "active", "confirmed", "pending", "failure", "success"}
    if set(data) not in (keys, keys | {"transfer"}):
        return None
    if data["v"] != JOURNAL_VERSION:
        return None
    bootstrap = _counter(data["bootstrap"])
    boots = _counter(data["boots"])
    if bootstrap is None or boots is None:
        return None
    if bootstrap > BOOTSTRAP_VERSION:
        # A newer bootstrap wrote this. Interpreting it could start the wrong
        # slot, so the caller requires a USB update instead.
        return None
    if data["active"] not in SLOT_NAMES or data["confirmed"] not in SLOT_NAMES:
        return None
    try:
        pending = _pending(data["pending"])
        failure = _failure(data["failure"])
        success = _success(data["success"])
        transfer = data.get("transfer")
        if transfer is not None:
            if not isinstance(transfer, dict) or set(transfer) != {
                    "slot", "target", "generation", "attempt", "command", "phase"}:
                return None
            pending_shape = dict((key, value) for key, value in transfer.items() if key != "phase")
            pending_shape["tries"] = 0
            _pending(pending_shape)
            if (not _is_id(transfer["generation"]) or not _is_id(transfer["attempt"])
                    or not _is_id(transfer["command"]) or transfer["phase"] not in
                    ("downloading", "verifying", "activating")):
                return None
    except (TypeError, ValueError):
        return None
    return {"v": JOURNAL_VERSION, "bootstrap": bootstrap, "boots": boots,
            "active": data["active"], "confirmed": data["confirmed"],
            "pending": pending, "failure": failure, "success": success, "transfer": transfer}


def encode_journal(journal):
    """The compact stored form. Values are already validated by the caller."""
    return json.dumps({"v": JOURNAL_VERSION, "bootstrap": int(journal["bootstrap"]),
                       "boots": int(journal["boots"]), "active": journal["active"],
                       "confirmed": journal["confirmed"],
                       "pending": journal["pending"], "failure": journal["failure"],
                       "success": journal["success"], "transfer": journal.get("transfer")},
                      separators=(",", ":"))


# -- the file system seam ---------------------------------------------------

class FileStore(object):
    """The real device filesystem, behind the four calls the journal needs.

    Injected rather than imported so the tests can drive every write and
    rename boundary from memory, including the crash between them.
    """

    def exists(self, path):
        try:
            os.stat(path)
            return True
        except OSError:
            return False
        except Exception:
            return False

    def read(self, path):
        try:
            with open(path, "r") as handle:
                return handle.read(MAX_BYTES + 1)
        except Exception:
            return None

    def write(self, path, text):
        folder = STATE_FOLDER
        try:
            os.mkdir(folder)
        except OSError:
            pass
        except Exception:
            pass
        with open(path, "wb" if isinstance(text, bytes) else "w") as handle:
            handle.write(text)

    def open(self, path, mode):
        return open(path, mode)

    def listdir(self, folder):
        return os.listdir(folder)

    def free_bytes(self):
        stat = os.statvfs('/')
        return (stat[1] or stat[0]) * stat[4]

    def replace(self, source, target):
        try:
            os.remove(target)
        except OSError:
            pass
        except Exception:
            pass
        os.rename(source, target)

    def remove(self, path):
        try:
            os.remove(path)
        except OSError:
            pass
        except Exception:
            pass

    def sync(self):
        try:
            os.sync()
        except Exception:
            pass


def load(fs):
    """The stored journal and where it came from.

    ``primary`` and ``backup`` are real stored journals; ``fresh`` means none
    was ever written; ``corrupt`` means one was found but could not be used.
    """
    primary = fs.read(JOURNAL_PATH)
    if primary is not None:
        try:
            header = json.loads(primary)
            if isinstance(header, dict) and type(header.get("bootstrap")) is int and header["bootstrap"] > BOOTSTRAP_VERSION:
                return None, "incompatible"
        except Exception:
            pass
    if primary is not None:
        parsed = parse_journal(primary)
        if parsed is not None:
            return parsed, "primary"
        found_corrupt = True
    else:
        found_corrupt = False
    backup = fs.read(JOURNAL_BACKUP)
    if backup is not None:
        parsed = parse_journal(backup)
        if parsed is not None:
            return parsed, "backup"
        found_corrupt = True
    # A torn write can leave only the temporary file.
    temporary = fs.read(JOURNAL_TEMP)
    if temporary is not None:
        parsed = parse_journal(temporary)
        if parsed is not None:
            return parsed, "temporary"
        found_corrupt = True
    found_corrupt = found_corrupt or any(fs.exists(path) for path in
                                         (JOURNAL_PATH, JOURNAL_BACKUP, JOURNAL_TEMP))
    return None, "corrupt" if found_corrupt else "fresh"


def save(fs, journal):
    """Write the journal through a temporary file and a rename.

    A crash after the temporary write leaves the current journal intact; a
    crash after the first rename leaves the previous journal in the backup.
    The reader understands all three states.
    """
    text = encode_journal(journal)
    if len(text) > MAX_BYTES:
        raise ValueError("journal too large")
    fs.write(JOURNAL_TEMP, text)
    if fs.exists(JOURNAL_PATH):
        fs.replace(JOURNAL_PATH, JOURNAL_BACKUP)
    fs.replace(JOURNAL_TEMP, JOURNAL_PATH)
    fs.sync()


# -- boot decisions ---------------------------------------------------------

def on_boot(journal, source="primary"):
    """This start's journal and what the bootstrap should do.

    Returns ``(journal, decision)``. The decision names the slot to run, says
    whether it is an unconfirmed candidate, and whether the previous confirmed
    slot must be restored instead. ``changed`` says whether it must be saved
    before the slot is entered.
    """
    if journal is None or source in ("corrupt", "incompatible", "fresh"):
        return journal, {"action": "recovery", "slot": None, "candidate": False,
                         "boot_confirmed": False, "changed": False, "source": source}
    if int(journal["boots"]) >= MAX_COUNTER:
        raise ValueError("boot counter exhausted; USB identity recovery required")
    journal = dict(journal)
    journal["boots"] = int(journal["boots"]) + 1
    transfer = journal.get("transfer")
    if transfer is not None:
        journal = record_failure(journal, "failed", "connection_lost", transfer["target"],
                                 transfer["generation"], transfer["attempt"])
    if journal["pending"] is not None and journal["pending"]["tries"] >= MAX_CANDIDATE_ENTRIES:
        pending = journal["pending"]
        journal = dict(journal)
        journal["active"] = journal["confirmed"]
        journal["pending"] = None
        journal["failure"] = {"phase": "rolled_back", "error": "boot_crash",
                              "target": pending["target"],
                              "generation": pending["generation"],
                              "attempt": pending["attempt"]}
        return journal, {"action": "run", "slot": journal["confirmed"],
                         "candidate": False, "boot_confirmed": True,
                         "rolled_back": True, "changed": True,
                         "source": source}
    journal = dict(journal)
    pending = journal["pending"]
    if pending is None:
        return journal, {"action": "run", "slot": journal["active"],
                         "candidate": False, "boot_confirmed": True,
                         "rolled_back": False, "changed": True, "source": source}
    pending = dict(pending)
    pending["tries"] = pending["tries"] + 1
    journal["pending"] = pending
    journal["active"] = pending["slot"]
    return journal, {"action": "run", "slot": pending["slot"], "candidate": True,
                     "boot_confirmed": False, "rolled_back": False,
                     "changed": True, "source": source}


def begin_candidate(journal, slot, target, generation=None, attempt=None,
                    command=None):
    """Start a bounded activation of ``slot`` for ``target``."""
    if slot not in SLOT_NAMES or not is_hash(target):
        raise ValueError("invalid candidate")
    journal = dict(journal)
    journal["pending"] = {"slot": slot, "target": target,
                          "generation": generation, "attempt": attempt,
                          "command": command, "tries": 0}
    journal["failure"] = None
    journal["success"] = None
    journal["transfer"] = None
    return journal


def begin_transfer(journal, slot, target, generation, attempt, command):
    if (slot not in SLOT_NAMES or slot == journal["active"] or not is_hash(target)
            or not all(_is_id(value) for value in (generation, attempt, command))
            or journal["pending"] is not None):
        raise ValueError("invalid transfer")
    value = dict(journal)
    value["failure"] = None
    value["success"] = None
    value["transfer"] = {"slot": slot, "target": target, "generation": generation,
                         "attempt": attempt, "command": command, "phase": "downloading"}
    return value


def confirm(journal, target=None):
    """The running slot is healthy: it becomes the confirmed slot.

    The target is the exact revision that was activated, so a slot that was
    replaced underneath a running app cannot be confirmed by accident. A
    daemon-issued attempt leaves a durable success record, so a device whose
    final poll was lost re-reports the same result after a reboot instead of
    looking unconfirmed.
    """
    journal = dict(journal)
    pending = journal["pending"]
    if pending is not None:
        if target is not None and pending["target"] != target:
            raise ValueError("confirmation target differs from the activation")
        journal["confirmed"] = pending["slot"]
        journal["active"] = pending["slot"]
        journal["pending"] = None
        journal["failure"] = None
        if pending["generation"] is not None and pending["attempt"] is not None:
            journal["success"] = {"target": pending["target"],
                                  "generation": pending["generation"],
                                  "attempt": pending["attempt"]}
    else:
        journal["confirmed"] = journal["active"]
    return journal


def record_failure(journal, phase, error, target=None, generation=None,
                   attempt=None):
    """Keep a terminal attempt record until the daemon reconciles it."""
    if phase not in FAILED_PHASES or error not in ERROR_CODES:
        raise ValueError("invalid failure")
    journal = dict(journal)
    journal["failure"] = {"phase": phase, "error": error, "target": target,
                          "generation": generation, "attempt": attempt}
    journal["pending"] = None
    journal["transfer"] = None
    journal["success"] = None
    journal["active"] = journal["confirmed"]
    return journal


def clear_failure(journal):
    """Forget a terminal attempt once the daemon has settled it."""
    journal = dict(journal)
    journal["failure"] = None
    return journal


def context(journal, source="primary"):
    """What a running app reports about its own start.

    ``boot_confirmed`` is true only for the confirmed slot; a candidate that
    is still awaiting its daemon acknowledgement reports false, which is what
    makes the two-poll confirmation handshake meaningful.
    """
    pending = journal["pending"]
    slot = journal["active"]
    if pending is not None:
        confirmed = False
    else:
        confirmed = journal["confirmed"] == slot
    return {"boot": int(journal["boots"]), "bootstrap": int(journal["bootstrap"]),
            "slot": slot, "boot_confirmed": confirmed,
            "target": pending["target"] if pending is not None else None,
            "source": source}


def _attempt_complete(record):
    """Whether an attempt record names a daemon-issued permit.

    A locally staged or USB-installed activation has no generation and no
    attempt, so it is reported as idle: the protocol requires both for any
    non-idle phase, and inventing them would make the daemon refuse the whole
    report. The local confirmation still happens in the journal.
    """
    return (record is not None and is_hash(record.get("target"))
            and _is_id(record.get("generation")) and _is_id(record.get("attempt")))


def update_block(journal, phase=None, error=None, command_ack=None):
    """The lifecycle ``update`` block this device should report.

    Pending activation is reported as ``confirming``; a retained failure keeps
    its terminal phase until the daemon settles it. Everything else is idle,
    because this app has no other update in flight.
    """
    pending = journal["pending"]
    failure = journal["failure"]
    success = journal.get("success")
    transfer = journal.get("transfer")
    if transfer is not None:
        return {"target": transfer["target"], "generation": transfer["generation"],
                "attempt": transfer["attempt"], "command_ack": transfer["command"],
                "phase": transfer["phase"], "error": None}
    if _attempt_complete(pending):
        return {"target": pending["target"], "generation": pending["generation"],
                "attempt": pending["attempt"], "command_ack": command_ack,
                "phase": "confirming", "error": None}
    if _attempt_complete(failure):
        return {"target": failure["target"], "generation": failure["generation"],
                "attempt": failure["attempt"], "command_ack": command_ack,
                "phase": failure["phase"], "error": failure["error"]}
    if _attempt_complete(success):
        return {"target": success["target"], "generation": success["generation"],
                "attempt": success["attempt"], "command_ack": command_ack,
                "phase": "succeeded", "error": None}
    if phase is not None and phase in UPDATE_PHASES and phase != "idle":
        return {"target": None, "generation": None, "attempt": None,
                "command_ack": command_ack, "phase": phase, "error": error}
    return {"target": None, "generation": None, "attempt": None,
            "command_ack": None, "phase": "idle", "error": None}
