"""One public app inventory for the simulator and USB deployment.

Private device configuration and firmware files are deliberately outside it.
"""

import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PRESTO = ROOT / "presto"
RECEIPT = ROOT / ".preview" / "device-sync.json"


def app_files(root=ROOT):
    """Source -> installed path; new public app modules join automatically."""
    folder = Path(root) / "presto"
    result = {}
    for source in sorted(folder.glob("*.py")):
        name = source.name.lower()
        if name == "secrets.py" or name.startswith("config."):
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
        compile(data, target, "exec")
    hashes = {target: hashlib.sha256(data).hexdigest() for target, data in files.items()}
    return {"revision": revision(hashes), "hashes": hashes, "files": files}


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
