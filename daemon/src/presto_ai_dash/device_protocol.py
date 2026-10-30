"""The device lifecycle protocol: identity, revision schemes, and validation.

This module is the one place that decides what a device may say about itself
and what the daemon may believe. It holds no state and touches no database, so
both the polling path and the admin page validate the same way, and the tests
can exercise the rules without a server.

Three ideas are deliberately kept apart here, because conflating them is how a
"device is out of date" claim turns into a lie:

* **Identity.** A device ID groups reports from one device. It is generated
  once at setup and is not an authentication credential: anyone holding the
  shared token can present any ID, so neither a report nor its confirmation is
  physical USB verification.
* **Revision.** A content hash of the installed public app files. Two
  revisions are equal or different; a different hash is never "older". The
  scheme says which files were hashed: ``legacy-v1`` is the aggregate of the
  current fixed-path install (``/ai_dash.py`` plus ``/ai_dash_files/*``), and
  ``slot-v1`` is the future per-slot hash. They are not comparable.
* **Update state.** What the device and the daemon believe is happening to the
  installed app. It is derived, never supplied by the device as a verdict.

The wire shapes and their bounds are section 8.2 of the device lifecycle plan;
the constants below are that contract.
"""

import json
import re

# The version of the lifecycle extension carried in X-Presto-Lifecycle.
PROTOCOL_VERSION = 1

# The two revision schemes. legacy-v1 means the existing fixed-path aggregate,
# including the launcher. slot-v1 means the hash of the canonical public slot
# files. A legacy value must never be compared with the prefix of a slot value.
SCHEME_LEGACY = "legacy-v1"
SCHEME_SLOT = "slot-v1"
SCHEMES = (SCHEME_LEGACY, SCHEME_SLOT)

# A device ID, a generation, an attempt and a command ID are one shape: 32
# lowercase hexadecimal characters. They correlate; they do not authenticate.
ID_CHARS = 32
ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")

# A legacy revision may be reported as the 12-character display label the
# current install already writes, or in full when one is genuinely available.
LEGACY_REVISION_CHARS = 12
# A slot-v1 revision is always the full SHA-256.
SLOT_REVISION_CHARS = 64
HASH_PATTERN = re.compile(r"^[0-9a-f]+$")

# The two headers this module validates. Both go on status GETs only.
DEVICE_ID_HEADER = "X-Presto-Device-ID"
LIFECYCLE_HEADER = "X-Presto-Lifecycle"
# The header is one compact JSON object, at most this many bytes, at most this
# deep. Anything larger or deeper is not the protocol.
LIFECYCLE_MAX_BYTES = 1024
LIFECYCLE_MAX_DEPTH = 3

# The counters are 32-bit positive integers. Booleans are not integers here.
MAX_COUNTER = 2147483647
# The capability list: unique names, each short ASCII. Only ota-v1 is known in
# v1; an unknown name is ignored and never grants a capability.
MAX_CAPABILITIES = 4
MAX_CAPABILITY_CHARS = 24
KNOWN_CAPABILITIES = ("ota-v1",)

# The phases a device may report. They are the device's own progress, not the
# daemon's derived update state, and a device cannot report the daemon's.
UPDATE_PHASES = ("idle", "downloading", "verifying", "activating", "confirming",
                 "succeeded", "rolled_back", "failed", "blocked")
# The phases that end an attempt.
TERMINAL_PHASES = ("succeeded", "rolled_back", "failed", "blocked")
# The phases that require an error code.
ERROR_PHASES = ("failed", "blocked", "rolled_back")

# The fixed error codes. A device maps a local exception to one of these and
# never sends an exception string, a path, a credential or a response body.
ERROR_CODES = ("transfer_timeout", "connection_lost", "http_error",
               "storage_insufficient", "firmware_unsupported",
               "bootstrap_incompatible", "transport_untrusted",
               "manifest_invalid", "signature_invalid", "hash_mismatch",
               "boot_crash", "rollback_failed", "local_state_invalid",
               "unknown_error")

# The seven update states shown on both surfaces.
UPDATE_STATES = ("current", "pending", "updating", "failed", "manual_action",
                 "unconfirmed", "unknown")
# The states that need a person to do something, or to know something.
ATTENTION_STATES = ("failed", "manual_action", "unconfirmed")

# The daemon's own reason codes. Fixed strings with local UI text, never a
# remotely supplied message.
REASONS = ("usb_update_required", "revision_uncomparable", "bundle_unavailable",
           "ota_not_enabled", "retry_exhausted", "attempt_timeout",
           "confirmation_timeout", "target_changed", "registry_full",
           "state_unavailable")

# How the daemon classifies the report it just received.
REPORT_STATUSES = ("accepted", "legacy", "invalid", "stale", "unsupported",
                   "registry_full", "unavailable")
REPORT_ACCEPTED = "accepted"
REPORT_LEGACY = "legacy"
REPORT_INVALID = "invalid"
REPORT_STALE = "stale"
REPORT_UNSUPPORTED = "unsupported"
REPORT_REGISTRY_FULL = "registry_full"
REPORT_UNAVAILABLE = "unavailable"

# How a reported revision relates to the expected one. Hashes have no order.
COMPARISON_EQUAL = "equal"
COMPARISON_DIFFERENT = "different"
COMPARISON_INCOMPARABLE = "incomparable"
COMPARISON_UNKNOWN = "unknown"
COMPARISONS = (COMPARISON_EQUAL, COMPARISON_DIFFERENT,
               COMPARISON_INCOMPARABLE, COMPARISON_UNKNOWN)

# The bounded automatic retry policy. Part 3 never starts an attempt, so these
# only describe the budget a future updater would spend.
ATTEMPT_LIMIT = 3
RETRY_DELAYS_S = (60, 300)
# How many durable devices the daemon keeps.
MAX_DEVICES = 64
# How often an unchanged device's last-seen and sequence are checkpointed.
CHECKPOINT_SECONDS = 300
# How long an admin reconciliation request waits for a fresh report.
RECOVERY_SECONDS = 600

# The largest serialized extension each response key may add, and the largest
# whole status body. The device's own memory bound is the reason for the last.
MAX_LIFECYCLE_BYTES = 2048
MAX_RELEASE_BYTES = 1024
MAX_STATUS_BYTES = 65536


class LifecycleError(ValueError):
    """A lifecycle report that cannot be used.

    ``code`` is the fixed classification the daemon reports: ``unsupported``
    for a version it does not speak, ``invalid`` for everything else. The
    rejected input is never echoed.
    """

    def __init__(self, code, message=""):
        super().__init__(message or code)
        self.code = code


def valid_id(value):
    """Whether a value is a well-formed device, generation, attempt or command ID."""
    return isinstance(value, str) and bool(ID_PATTERN.match(value))


def valid_revision(scheme, value):
    """Whether a revision is well-formed for its scheme.

    A legacy report may be the 12-character display label or the full hash.
    A slot report is always the full 64 characters.
    """
    if not isinstance(value, str) or not HASH_PATTERN.match(value):
        return False
    if scheme == SCHEME_LEGACY:
        return len(value) in (LEGACY_REVISION_CHARS, SLOT_REVISION_CHARS)
    if scheme == SCHEME_SLOT:
        return len(value) == SLOT_REVISION_CHARS
    return False


def _counter(value):
    """A positive 32-bit integer, or None. Booleans, strings and floats fail."""
    if type(value) is not int:
        return None
    if not 1 <= value <= MAX_COUNTER:
        return None
    return value


def _optional_counter(value):
    """A positive 32-bit integer or an explicit null."""
    if value is None:
        return None
    return _counter(value)


def _object_pairs(pairs):
    """A JSON object hook that refuses a duplicate key.

    Duplicate keys are how a report says two different things at once, so the
    whole report is refused rather than resolved in the parser's favour.
    """
    seen = set()
    result = {}
    for key, value in pairs:
        if key in seen:
            raise LifecycleError(REPORT_INVALID, "duplicate key")
        seen.add(key)
        result[key] = value
    return result


def _depth(value):
    """How deeply a parsed JSON value nests."""
    if isinstance(value, dict):
        return 1 + max([_depth(item) for item in value.values()] or [0])
    if isinstance(value, list):
        return 1 + max([_depth(item) for item in value] or [0])
    return 1


def parse_lifecycle(raw):
    """A validated lifecycle report, or ``LifecycleError``.

    ``raw`` is the ``X-Presto-Lifecycle`` header value. It is untrusted input:
    it is refused whole on an unknown version, a duplicate key, a wrong type,
    an out-of-range number, an invalid cross-field combination, or a size or
    depth outside the contract. Nothing in the rejected value is echoed.
    """
    if not isinstance(raw, str) or not raw:
        raise LifecycleError(REPORT_INVALID, "empty")
    if any(not 32 <= ord(char) < 127 for char in raw):
        raise LifecycleError(REPORT_INVALID, "not printable ascii")
    if len(raw.encode("utf-8")) > LIFECYCLE_MAX_BYTES:
        raise LifecycleError(REPORT_INVALID, "too long")
    try:
        data = json.loads(raw, object_pairs_hook=_object_pairs)
    except LifecycleError:
        raise
    except Exception:
        # The parser's own message can carry the input; never propagate it.
        raise LifecycleError(REPORT_INVALID, "not json")
    if not isinstance(data, dict):
        raise LifecycleError(REPORT_INVALID, "not an object")
    if _depth(data) > LIFECYCLE_MAX_DEPTH:
        raise LifecycleError(REPORT_INVALID, "too deep")
    version = data.get("v")
    if type(version) is not int:
        raise LifecycleError(REPORT_INVALID, "no version")
    if version != PROTOCOL_VERSION:
        raise LifecycleError(REPORT_UNSUPPORTED, "unsupported version")
    expected_keys = {"v", "boot", "seq", "scheme", "revision", "bootstrap",
                     "slot", "boot_confirmed", "caps", "update"}
    if set(data) != expected_keys:
        raise LifecycleError(REPORT_INVALID, "unexpected keys")
    boot = _counter(data["boot"])
    seq = _counter(data["seq"])
    if boot is None or seq is None:
        raise LifecycleError(REPORT_INVALID, "bad counters")
    scheme, revision = _scheme_and_revision(data["scheme"], data["revision"])
    bootstrap = _optional_counter(data["bootstrap"])
    if data["bootstrap"] is not None and bootstrap is None:
        raise LifecycleError(REPORT_INVALID, "bad bootstrap")
    slot = data["slot"]
    if slot is not None and slot not in ("a", "b"):
        raise LifecycleError(REPORT_INVALID, "bad slot")
    if type(data["boot_confirmed"]) is not bool:
        raise LifecycleError(REPORT_INVALID, "bad boot_confirmed")
    capabilities = _capabilities(data["caps"])
    update = _update(data["update"])
    return {"v": PROTOCOL_VERSION, "boot": boot, "seq": seq,
            "scheme": scheme, "revision": revision, "bootstrap": bootstrap,
            "slot": slot, "boot_confirmed": data["boot_confirmed"],
            "caps": capabilities, "update": update}


def _scheme_and_revision(scheme, revision):
    """The pair, which is either both usable or both null."""
    if scheme is None and revision is None:
        return None, None
    if not isinstance(scheme, str) or scheme not in SCHEMES:
        raise LifecycleError(REPORT_INVALID, "bad scheme")
    if not valid_revision(scheme, revision):
        raise LifecycleError(REPORT_INVALID, "bad revision")
    return scheme, revision


def _capabilities(raw):
    """The validated capability list: unique short ASCII names, in order."""
    if not isinstance(raw, list):
        raise LifecycleError(REPORT_INVALID, "caps not a list")
    if len(raw) > MAX_CAPABILITIES:
        raise LifecycleError(REPORT_INVALID, "too many caps")
    result = []
    for name in raw:
        if not isinstance(name, str) or not name:
            raise LifecycleError(REPORT_INVALID, "bad cap")
        if len(name) > MAX_CAPABILITY_CHARS:
            raise LifecycleError(REPORT_INVALID, "cap too long")
        if any(not 32 <= ord(char) < 127 for char in name):
            raise LifecycleError(REPORT_INVALID, "cap not ascii")
        if name in result:
            raise LifecycleError(REPORT_INVALID, "duplicate cap")
        result.append(name)
    return result


def _update(raw):
    """The validated update block, which must agree with its own phase."""
    if not isinstance(raw, dict):
        raise LifecycleError(REPORT_INVALID, "update not an object")
    if set(raw) != {"target", "generation", "attempt", "command_ack",
                    "phase", "error"}:
        raise LifecycleError(REPORT_INVALID, "unexpected update keys")
    phase = raw["phase"]
    if not isinstance(phase, str) or phase not in UPDATE_PHASES:
        raise LifecycleError(REPORT_INVALID, "bad phase")
    target = raw["target"]
    generation = raw["generation"]
    attempt = raw["attempt"]
    command_ack = raw["command_ack"]
    error = raw["error"]
    for value in (generation, attempt, command_ack):
        if value is not None and not valid_id(value):
            raise LifecycleError(REPORT_INVALID, "bad identifier")
    if error is not None and error not in ERROR_CODES:
        raise LifecycleError(REPORT_INVALID, "bad error")
    if phase == "idle":
        if target is not None or generation is not None or attempt is not None \
                or command_ack is not None or error is not None:
            raise LifecycleError(REPORT_INVALID, "idle with attempt data")
    else:
        if not valid_revision(SCHEME_SLOT, target):
            raise LifecycleError(REPORT_INVALID, "bad target")
        if generation is None or attempt is None:
            raise LifecycleError(REPORT_INVALID, "attempt without a permit")
        if phase in ERROR_PHASES:
            if error is None:
                raise LifecycleError(REPORT_INVALID, "terminal phase without an error")
        elif error is not None:
            raise LifecycleError(REPORT_INVALID, "error outside a failed phase")
    return {"target": target, "generation": generation, "attempt": attempt,
            "command_ack": command_ack, "phase": phase, "error": error}


def idle_update():
    """The update block of a device that is not updating."""
    return {"target": None, "generation": None, "attempt": None,
            "command_ack": None, "phase": "idle", "error": None}


def _json_string(value):
    """A JSON string literal for an already-validated ASCII value."""
    if value is None:
        return "null"
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def lifecycle_text(report):
    """The compact header value for a validated report.

    Written by hand rather than by ``json.dumps`` so the device and the daemon
    agree byte for byte, and so no serializer option can change the shape.
    """
    update = report.get("update") or idle_update()
    caps = report.get("caps") or []
    bootstrap = report.get("bootstrap")
    return ("{%s}" % ",".join([
        '"v":%d' % PROTOCOL_VERSION,
        '"boot":%d' % report["boot"],
        '"seq":%d' % report["seq"],
        '"scheme":%s' % _json_string(report.get("scheme")),
        '"revision":%s' % _json_string(report.get("revision")),
        '"bootstrap":%s' % ("null" if bootstrap is None else "%d" % bootstrap),
        '"slot":%s' % _json_string(report.get("slot")),
        '"boot_confirmed":%s' % ("true" if report.get("boot_confirmed") else "false"),
        '"caps":[%s]' % ",".join(_json_string(name) for name in caps),
        '"update":{%s}' % ",".join([
            '"target":%s' % _json_string(update.get("target")),
            '"generation":%s' % _json_string(update.get("generation")),
            '"attempt":%s' % _json_string(update.get("attempt")),
            '"command_ack":%s' % _json_string(update.get("command_ack")),
            '"phase":%s' % _json_string(update.get("phase") or "idle"),
            '"error":%s' % _json_string(update.get("error")),
        ]),
    ]))


def compare(reported, expected):
    """How a reported revision relates to the expected one.

    ``reported`` and ``expected`` are ``{"scheme", "revision"}`` shapes. A
    legacy report is compared with a legacy expectation only: a 12-character
    label against the first 12 characters of the expected aggregate, a full
    legacy hash in full. A slot value is compared in full. Different schemes
    are incomparable, not different, and a missing value is unknown.
    """
    if not isinstance(reported, dict) or not isinstance(expected, dict):
        return COMPARISON_UNKNOWN
    reported_scheme = reported.get("scheme")
    reported_revision = reported.get("revision")
    expected_scheme = expected.get("scheme")
    expected_revision = expected.get("revision")
    if not reported_scheme or not reported_revision:
        return COMPARISON_UNKNOWN
    if not expected_scheme or not expected_revision:
        return COMPARISON_UNKNOWN
    if reported_scheme != expected_scheme:
        return COMPARISON_INCOMPARABLE
    if reported_scheme == SCHEME_LEGACY:
        if not valid_revision(SCHEME_LEGACY, reported_revision):
            return COMPARISON_UNKNOWN
        if len(reported_revision) == LEGACY_REVISION_CHARS:
            return (COMPARISON_EQUAL
                    if reported_revision == expected_revision[:LEGACY_REVISION_CHARS]
                    else COMPARISON_DIFFERENT)
        return (COMPARISON_EQUAL if reported_revision == expected_revision
                else COMPARISON_DIFFERENT)
    if reported_scheme == SCHEME_SLOT:
        if not valid_revision(SCHEME_SLOT, reported_revision):
            return COMPARISON_UNKNOWN
        if not valid_revision(SCHEME_SLOT, expected_revision):
            return COMPARISON_UNKNOWN
        return (COMPARISON_EQUAL if reported_revision == expected_revision
                else COMPARISON_DIFFERENT)
    return COMPARISON_UNKNOWN


def bundle_identity(bundle):
    """The expected identity of the running daemon's app bundle, or None.

    The bundle is the one ``device_install.app_snapshot()`` returns: a wheel's
    staged files or a checkout's. A bundle that does not declare a scheme and a
    full revision is not verifiable, and an unverifiable bundle means the
    daemon must report ``unknown`` rather than guess.
    """
    if not isinstance(bundle, dict):
        return None
    scheme = bundle.get("scheme", SCHEME_LEGACY)
    revision = bundle.get("revision")
    if not isinstance(scheme, str) or scheme not in SCHEMES:
        return None
    if not isinstance(revision, str) or not valid_revision(scheme, revision):
        return None
    if len(revision) != SLOT_REVISION_CHARS:
        # The expected aggregate is always the full hash, whatever a device
        # chooses to report. A short expectation could not be compared safely.
        return None
    min_bootstrap = bundle.get("min_bootstrap")
    if min_bootstrap is not None and _counter(min_bootstrap) is None:
        return None
    if scheme == SCHEME_LEGACY:
        # The current install has no A/B bootstrap, so nothing may require one.
        min_bootstrap = None
    return {"scheme": scheme, "revision": revision,
            "min_bootstrap": min_bootstrap}


def expected_shape(identity):
    """The ``expected`` block of a status response, or None when unavailable."""
    if identity is None:
        return None
    return {"scheme": identity["scheme"], "revision": identity["revision"],
            "min_bootstrap": identity["min_bootstrap"]}


def slot_identity(bundle):
    """The ``slot-v1`` identity of the running daemon's bundle, or None.

    A wheel built before Part 2 carries no slot layout, and then no device can
    be compared against a slot revision or offered an update. That is
    reported as unavailable rather than inferred from the legacy aggregate:
    the two schemes hash different file sets.
    """
    if not isinstance(bundle, dict):
        return None
    slot = bundle.get("slot")
    if not isinstance(slot, dict):
        return None
    if slot.get("scheme") != SCHEME_SLOT:
        return None
    revision = slot.get("revision")
    if not valid_revision(SCHEME_SLOT, revision):
        return None
    min_bootstrap = slot.get("min_bootstrap")
    if _counter(min_bootstrap) is None:
        # The slot layout always requires a positive bootstrap version; a slot
        # bundle without one could not be installed by anything.
        return None
    return {"scheme": SCHEME_SLOT, "revision": revision,
            "min_bootstrap": min_bootstrap}


def bootstrap_version(bundle):
    """The bootstrap compatibility version this bundle ships, or None."""
    if not isinstance(bundle, dict):
        return None
    bootstrap = bundle.get("bootstrap")
    if not isinstance(bootstrap, dict):
        return None
    version = bootstrap.get("version")
    return _counter(version)


def reported_shape(scheme, revision, bootstrap):
    """The ``reported`` block of a status response."""
    return {"scheme": scheme, "revision": revision, "bootstrap": bootstrap}
