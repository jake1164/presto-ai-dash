"""Copies the public Presto app and its preview tool into the image.

Run while the image is built, for the container's DASH_PRESTO_PREVIEW switch.
The files kept are the ones the USB deployment installs (one inventory, in
tools/presto_bundle.py), so a device's config or secrets file, or a copy or
backup of one, never reaches the image.
"""

from pathlib import Path
import shutil
import sys


def collect(source, target):
    source, target = Path(source), Path(target)
    sys.path.insert(0, str(source / "tools"))
    from presto_bundle import app_files
    for path in app_files(source):
        destination = target / path.relative_to(source)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, destination)
    tools = target / "tools"
    tools.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source / "tools" / "presto_bundle.py", tools / "presto_bundle.py")
    shutil.copytree(source / "tools" / "presto_preview", tools / "presto_preview",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))


if __name__ == "__main__":
    collect(sys.argv[1], sys.argv[2])
