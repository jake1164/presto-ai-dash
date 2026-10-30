"""One public app inventory for the simulator and USB deployment.

Private device configuration and firmware files are deliberately outside it.

The inventory describes two installation layouts from the same ``presto/``
sources, and never makes one stand in for the other:

* the **legacy** layout (``legacy-v1``) — the current fixed paths, ``/ai_dash.py``
  plus ``/ai_dash_files/*``, whose revision aggregates both;
* the **slot** layout (``slot-v1``) — a small stable bootstrap outside two
  replaceable A/B slots, whose revision hashes only the canonical public
  slot-relative files.

``snapshot()`` keeps returning the legacy bundle at the top level, because the
current device install and every existing caller speak that. The slot and
bootstrap bundles travel beside it, so nothing has to reinterpret an old
12-character revision as a slot hash.
"""

import hashlib
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PRESTO = ROOT / "presto"
RECEIPT = ROOT / ".preview" / "device-sync.json"

# The revision is written next to the app files, so the device can report the
# build it actually runs. It is not part of the revision itself.
REVISION_TARGET = "/ai_dash_files/revision.txt"
REVISION_CHARS = 12

# The scheme this inventory describes: the aggregate of the current fixed-path
# install, including the launcher. It is stated explicitly so a report can
# never be compared with a hash of a different layout, and so a future A/B
# slot layout (``slot-v1``) can be added without reinterpreting an old value.
REVISION_SCHEME = "legacy-v1"
# This layout has no boot manager, so nothing may require a bootstrap version.
MIN_BOOTSTRAP = None

# -- the slot layout -------------------------------------------------------
#
# The bootstrap lives outside both slots, so replacing a dashboard never
# touches the code that decides which dashboard to run. Identity, credentials,
# settings and the activation journal live outside both slots too.
SLOT_ROOT = "/ai_dash_slots"
SLOT_NAMES = ("a", "b")
REVISION_SCHEME_SLOT = "slot-v1"
SLOT_MIN_BOOTSTRAP = 1
# Per-slot generated files: the 12-character compatibility label the existing
# ``X-Presto-Revision`` header carries, and the full hash for the lifecycle
# report. Neither is part of the slot revision.
SLOT_REVISION_LABEL = "revision.txt"
SLOT_REVISION_FULL = "revision.sha256"
# The stable entry the stock carousel launches. It must not share its name
# with the folder it loads, because MicroPython would pick the namespace
# package first (measured in the hardware report).
BOOT_ENTRY = "/ai_dash_boot.py"
BOOT_FOLDER = "/ai_dash_boot_files"
# Durable boot counter, activation journal and confirmation marker. Outside
# both slots and outside the bootstrap files, so neither an app repair nor a
# bootstrap replacement restarts the ordering epoch.
STATE_FOLDER = "/ai_dash_state"
BOOTSTRAP_VERSION = 1
# Settings keep the path the setup utility already writes, so a migration
# never moves a private file.
SETTINGS_FOLDER = "/ai_dash_files"

# Bootstrap source -> installed target. ``activation.py`` is installed twice
# (once here, once in each slot) from this one source: the bootstrap cannot
# import the dashboard it is deciding whether to trust.
BOOTSTRAP_SOURCES = (
    ("bootstrap/ai_dash_boot.py", BOOT_ENTRY),
    ("bootstrap/boot.py", BOOT_FOLDER + "/boot.py"),
    ("activation.py", BOOT_FOLDER + "/activation.py"),
)


_MODULE_NAME = re.compile(r"^[A-Za-z0-9_]+\.py$")


def is_private_name(name):
    """Whether a file in ``presto/`` could hold the device's private settings.

    Not just ``secrets.py`` and ``config.py``: a copy or a backup of either
    (``secrets_old.py``, ``my_secrets.py``, ``config-backup.py``) is private
    too, and is never uploaded.
    """
    lowered = name.lower()
    return "secret" in lowered or lowered.startswith("config")


def app_files(root=ROOT):
    """Source -> installed path; new public app modules join automatically."""
    folder = Path(root) / "presto"
    result = {}
    for source in sorted(folder.glob("*.py")):
        # Only plain module names; nothing an editor or a person left behind.
        if not _MODULE_NAME.match(source.name) or is_private_name(source.name):
            continue
        if source.is_symlink():
            raise ValueError("app files must not be symbolic links")
        destination = "ai_dash_app.py" if source.name == "main.py" else source.name
        result[source] = "/ai_dash_files/" + destination
    launcher = folder / "launcher" / "ai_dash.py"
    if launcher.is_symlink():
        raise ValueError("launcher must not be a symbolic link")
    result[launcher] = "/ai_dash.py"
    if folder / "main.py" not in result:
        raise ValueError("Presto main.py is missing")
    return result


def revision(hashes):
    encoded = json.dumps(hashes, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def slot_files(root=ROOT):
    """Source -> canonical slot-relative name for the replaceable dashboard.

    The launcher is not part of a slot: the bootstrap replaces it, and the
    private settings folder stays outside both slots. The generated revision
    files are added afterwards and are excluded from the hash.
    """
    result = {}
    for source, target in app_files(root).items():
        if target == "/ai_dash.py":
            continue
        result[source] = target.rsplit("/", 1)[1]
    return result


def bootstrap_files(root=ROOT):
    """Source -> installed target for the stable bootstrap, or {} if absent.

    A checkout without ``presto/bootstrap/`` still produces a legacy bundle:
    a daemon that has no bootstrap must report that honestly rather than
    pretend a device can be migrated.
    """
    folder = Path(root) / "presto"
    result = {}
    for relative, target in BOOTSTRAP_SOURCES:
        source = folder / relative
        if not source.is_file() or source.is_symlink():
            return {}
        if is_private_name(source.name):
            return {}
        result[source] = target
    return result


def _compiled(files):
    for target, data in files.items():
        if target.endswith(".py"):
            compile(data, target, "exec")
    return files


def slot_snapshot(root=ROOT):
    """The ``slot-v1`` dashboard bundle, or None when the sources are absent."""
    sources = slot_files(root)
    if not sources:
        return None
    files = _compiled({name: source.read_bytes() for source, name in sources.items()})
    hashes = {name: hashlib.sha256(data).hexdigest() for name, data in files.items()}
    code_revision = revision(hashes)
    files[SLOT_REVISION_FULL] = (code_revision + "\n").encode("ascii")
    files[SLOT_REVISION_LABEL] = (code_revision[:REVISION_CHARS] + "\n").encode("ascii")
    return {"scheme": REVISION_SCHEME_SLOT, "revision": code_revision,
            "min_bootstrap": SLOT_MIN_BOOTSTRAP, "hashes": hashes, "files": files}


def bootstrap_snapshot(root=ROOT):
    """The bootstrap bundle, or None when this checkout has no bootstrap."""
    sources = bootstrap_files(root)
    if not sources:
        return None
    files = _compiled({target: source.read_bytes() for source, target in sources.items()})
    hashes = {target: hashlib.sha256(data).hexdigest() for target, data in files.items()}
    return {"version": BOOTSTRAP_VERSION, "hashes": hashes, "files": files}


def snapshot(root=ROOT):
    files = {target: source.read_bytes() for source, target in app_files(root).items()}
    _compiled(files)
    hashes = {target: hashlib.sha256(data).hexdigest() for target, data in files.items()}
    code_revision = revision(hashes)
    # The device reports the revision it runs, so it is written with the app.
    files[REVISION_TARGET] = (code_revision[:REVISION_CHARS] + "\n").encode("ascii")
    hashes[REVISION_TARGET] = hashlib.sha256(files[REVISION_TARGET]).hexdigest()
    return {"revision": code_revision, "scheme": REVISION_SCHEME,
            "min_bootstrap": MIN_BOOTSTRAP, "hashes": hashes, "files": files,
            "slot": slot_snapshot(root), "bootstrap": bootstrap_snapshot(root)}


def sync_status(code_revision, receipt=RECEIPT):
    """A dated USB observation, never an assertion of continuous device parity."""
    try:
        data = json.loads(Path(receipt).read_text(encoding="utf-8"))
        status = data["status"]
        if status not in ("matched", "different", "unverified"):
            raise ValueError("unknown sync status")
        if status == "matched" and data["revision"] != code_revision:
            status = "source_changed"
        return {"status": status, "checked_at": data.get("checked_at"),
                "revision": data.get("revision")}
    except (OSError, ValueError, TypeError, KeyError):
        return {"status": "not_checked", "checked_at": None, "revision": None}
