"""Device lifecycle: what this Presto says about itself, and what it shows.

Two small jobs, both pure Python so they run under CPython in the tests and on
the device unchanged:

``build`` writes the compact ``X-Presto-Lifecycle`` header. It is the device's
own telemetry: which app revision it runs, which boot and report this is, and
what (if anything) it is doing to its app. The daemon validates it against
``device_protocol.py``; nothing here decides whether the device is out of date,
because a device cannot see the daemon's bundle.

``notice`` reads the two objects the daemon adds to the status response and
turns them into the short ASCII lines the shared bottom-right indicator shows.
Every line comes from a fixed code-to-text table, never from the payload, so a
hostile or broken daemon cannot put arbitrary text on the screen.

The boot counter is the one thing written to flash: it is a durable count of
application starts, kept outside the replaceable app files so a repair does not
restart the ordering epoch. A torn write is recovered from the temporary file;
a lost counter makes the daemon treat the device's reports as stale, which is
what the plan asks for rather than silently reusing an old epoch.
"""

import os

# The lifecycle protocol version this app speaks.
PROTOCOL_VERSION = 1
# The revision scheme of the current fixed-path installation.
SCHEME = "legacy-v1"

# The two headers, on status GETs only.
DEVICE_ID_HEADER = "X-Presto-Device-ID"
LIFECYCLE_HEADER = "X-Presto-Lifecycle"

# The counters are 32-bit positive integers.
MAX_COUNTER = 2147483647
ID_CHARS = 32
DIGITS = "0123456789"
HEX = "0123456789abcdef"

# The boot counter lives outside the replaceable app files: a repair rewrites
# /ai_dash.py and /ai_dash_files/*, and must not restart the ordering epoch.
BOOT_FILE = "/ai_dash_boot.txt"
BOOT_TMP = "/ai_dash_boot.tmp"
BOOT_MAX_CHARS = 10

# The indicator's text budget: short lines in the detail font. Eight lines fit
# the information view between its title and its close hint.
MAX_NOTICE_LINES = 8
MAX_LINE_CHARS = 34

# A version label on screen: letters, digits and the punctuation a release
# name uses. Anything else is not shown at all.
VERSION_CHARS = ("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
                 "0123456789._+-")
MAX_VERSION_CHARS = 16

# The fixed text for each daemon reason code. A code that is not here has no
# text and shows nothing, so an unknown code can never be echoed to the screen.
REASON_TEXT = {
    "usb_update_required": (
        "Presto app does not match",
        "this daemon. Update over USB",
        "with presto_setup.py.",
        "See daemon admin."),
    "revision_uncomparable": (
        "Presto app revision cannot be",
        "compared with this daemon.",
        "See daemon admin."),
    "bundle_unavailable": (
        "The daemon cannot read its",
        "own Presto app bundle."),
    "retry_exhausted": (
        "Presto update failed after",
        "retries.",
        "See daemon admin."),
    "confirmation_timeout": (
        "Presto update was not",
        "confirmed.",
        "See daemon admin."),
    "attempt_timeout": (
        "Presto update timed out.",
        "See daemon admin."),
    "target_changed": (
        "Presto update target changed.",
        "See daemon admin."),
    "registry_full": (
        "Too many devices registered",
        "with the daemon."),
    "state_unavailable": (
        "Daemon device records are",
        "unavailable."),
}

RELEASE_LINES = (
    "Daemon update available:",
    "Open daemon admin for",
    "instructions.")


def _is_id(value):
    """Whether a value is 32 lowercase hexadecimal characters."""
    if not isinstance(value, str) or len(value) != ID_CHARS:
        return False
    for char in value:
        if char not in HEX:
            return False
    return True


def _ascii_lines(lines):
    """The lines, kept ASCII and inside the screen's budget."""
    kept = []
    for line in lines:
        text = "".join(char if " " <= char <= "~" else "?" for char in str(line))
        kept.append(text[:MAX_LINE_CHARS])
    return kept[:MAX_NOTICE_LINES]


def _version(value):
    """A release label fit to draw, or None."""
    if not isinstance(value, str) or not value or len(value) > MAX_VERSION_CHARS:
        return None
    for char in value:
        if char not in VERSION_CHARS:
            return None
    return value


def _json_string(value):
    if value is None:
        return "null"
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def build(device_id, boot, seq, revision, bootstrap=None, slot=None,
          boot_confirmed=True, caps=None):
    """The ``X-Presto-Lifecycle`` header value, or None when it is unusable.

    A malformed value is never sent: the daemon would refuse the whole report,
    and ordinary quota polling would then look worse than it is. ``revision``
    is the 12-character legacy label this install writes, or None when it is
    not known.

    The update block is always ``idle``. This device has no updater, so it has
    no attempt to report; claiming a phase it cannot complete would be the one
    thing the daemon must never believe.
    """
    if not _is_id(device_id):
        return None
    if type(boot) is not int or not 1 <= boot <= MAX_COUNTER:
        return None
    if type(seq) is not int or not 1 <= seq <= MAX_COUNTER:
        return None
    scheme = "null"
    if revision is not None:
        if not isinstance(revision, str) or not revision:
            return None
        if len(revision) not in (12, 64):
            return None
        for char in revision:
            if char not in HEX:
                return None
        scheme = _json_string(SCHEME)
        revision = _json_string(revision)
    else:
        revision = "null"
    names = []
    for name in caps or ():
        if isinstance(name, str) and name and len(name) <= 24:
            if name not in names:
                names.append(name)
    return ("{%s}" % ",".join([
        '"v":%d' % PROTOCOL_VERSION,
        '"boot":%d' % boot,
        '"seq":%d' % seq,
        '"scheme":%s' % scheme,
        '"revision":%s' % revision,
        '"bootstrap":%s' % ("null" if bootstrap is None else "%d" % int(bootstrap)),
        '"slot":%s' % _json_string(slot),
        '"boot_confirmed":%s' % ("true" if boot_confirmed else "false"),
        '"caps":[%s]' % ",".join(_json_string(name) for name in names[:4]),
        '"update":%s' % _update_json(None),
    ]))


def _update_json(update):
    """The update block: always idle, which is the only phase this app has.

    ``update`` is accepted so a caller can pass the state it holds, but a
    non-idle phase is never emitted: an attempt needs a permit, a target and a
    deadline the daemon issued, and this device has none of them.
    """
    del update
    return ("{" + ",".join([
        '"target":null',
        '"generation":null',
        '"attempt":null',
        '"command_ack":null',
        '"phase":"idle"',
        '"error":null',
    ]) + "}")


# -- the boot counter -------------------------------------------------------

def _read_boot(path):
    """A stored boot count, or None when it is missing or unusable."""
    try:
        with open(path, "r") as handle:
            text = handle.read(BOOT_MAX_CHARS + 1)
    except OSError:
        return None
    except Exception:
        return None
    if not isinstance(text, str):
        return None
    text = text.strip()
    if not text or len(text) > BOOT_MAX_CHARS:
        return None
    for char in text:
        if char not in DIGITS:
            return None
    value = int(text)
    return value if 1 <= value <= MAX_COUNTER else None


def _write_boot(value, path=BOOT_FILE):
    """Store the boot count through a temporary file.

    The temporary file is written first, so a power cut during the write leaves
    the previous count intact. The rename is not atomic on every filesystem the
    device may use, which is why ``_read_boot`` also accepts the temporary file.
    """
    temporary = path + ".tmp"
    try:
        with open(temporary, "w") as handle:
            handle.write("%d" % value)
    except Exception:
        return
    try:
        os.remove(path)
    except OSError:
        pass
    except Exception:
        pass
    try:
        os.rename(temporary, path)
    except Exception:
        pass


def bump_boot(file=None):
    """This start's boot count: the stored count plus one, stored again.

    A missing count starts at one. An exhausted counter stays at the maximum:
    the daemon then reports the device's reports as stale rather than letting
    an old ordering epoch be reused, and identity repair needs USB. ``file``
    is for the preview, which is not a device and keeps its count in a
    temporary directory instead of the filesystem root.
    """
    path = file or BOOT_FILE
    previous = _read_boot(path)
    if previous is None:
        previous = _read_boot(path + ".tmp")
    value = 1 if previous is None else previous + 1
    if value > MAX_COUNTER:
        value = MAX_COUNTER
    _write_boot(value, path)
    return value


# -- the indicator ----------------------------------------------------------

def _device_lines(lifecycle):
    """The device's own warning lines, or None when it needs no attention."""
    if not isinstance(lifecycle, dict):
        return None
    update = lifecycle.get("update")
    if not isinstance(update, dict):
        return None
    if update.get("needs_attention") is not True:
        return None
    reason = update.get("reason")
    if not isinstance(reason, str):
        return None
    return REASON_TEXT.get(reason)


def _release_lines(release):
    """The daemon-release lines, or None when no fresh release is announced."""
    if not isinstance(release, dict):
        return None
    if release.get("state") != "available":
        return None
    running = _version(release.get("running"))
    latest = _version(release.get("latest"))
    if not running or not latest or running == latest:
        return None
    return ("Daemon update available:",
            running + " -> " + latest,
            RELEASE_LINES[1],
            RELEASE_LINES[2])


def notice(payload):
    """What the shared indicator should show, or None for nothing.

    Returns ``{"warning": bool, "release": bool, "lines": [...]}``: the yellow
    device warning wins the icon when both are present, and the lines list the
    device problem first, then the release. An absent or unusable extension
    gives None, so an older daemon costs the screen nothing.
    """
    if not isinstance(payload, dict):
        return None
    device = _device_lines(payload.get("device_lifecycle"))
    release = _release_lines(payload.get("daemon_release"))
    if not device and not release:
        return None
    lines = []
    if device:
        # A release announcement keeps its own lines, so "open daemon admin"
        # is never the line that gets cut when a device problem is verbose.
        keep = MAX_NOTICE_LINES - (len(release) if release else 0)
        lines.extend(device[:max(1, keep)])
    if release:
        lines.extend(release)
    return {"warning": bool(device), "release": bool(release),
            "lines": _ascii_lines(lines)}


def kind(notice_value):
    """The icon to draw for a notice: ``warning``, ``release`` or None."""
    if not isinstance(notice_value, dict):
        return None
    if notice_value.get("warning"):
        return "warning"
    if notice_value.get("release"):
        return "release"
    return None
