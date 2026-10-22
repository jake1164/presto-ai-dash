"""One public app inventory for the simulator and USB deployment.

Private device configuration and firmware files are deliberately outside it.
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


def snapshot(root=ROOT):
    files = {target: source.read_bytes() for source, target in app_files(root).items()}
    for target, data in files.items():
        if target.endswith(".py"):
            compile(data, target, "exec")
    hashes = {target: hashlib.sha256(data).hexdigest() for target, data in files.items()}
    code_revision = revision(hashes)
    # The device reports the revision it runs, so it is written with the app.
    files[REVISION_TARGET] = (code_revision[:REVISION_CHARS] + "\n").encode("ascii")
    hashes[REVISION_TARGET] = hashlib.sha256(files[REVISION_TARGET]).hexdigest()
    return {"revision": code_revision, "hashes": hashes, "files": files}


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
