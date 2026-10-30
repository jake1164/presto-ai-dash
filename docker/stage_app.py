"""Stage the public Presto app into the daemon package inside the image build.

A wheel built by ``tools/build_release.py`` carries the app under
``presto_ai_dash/presto_app/`` (with ``presto_app_slot/`` and ``presto_app_boot/``
beside it), but those folders are generated and ignored by git, so they are
absent from a plain container build. Without this step the image
would run a daemon whose ``install-presto`` and admin setup download had no app
at all. The files are taken through the one inventory in
``tools/presto_bundle.py``, so private settings, secrets or unrelated files can
never reach the image, and the staged bytes are identical to the wheel's.
"""

from pathlib import Path
import sys


def stage(source, target):
    """Write the public app bundle into the daemon source; return the bundle."""
    source, target = Path(source), Path(target)
    sys.path.insert(0, str(source / "tools"))
    from presto_bundle import snapshot
    bundle = snapshot(source)
    package = Path(target) / "daemon" / "src" / "presto_ai_dash"
    # The legacy bundle, then the slot and bootstrap bundles beside it, exactly as
    # tools/build_release.py stages them for the wheel. Without the last two the
    # image could not serve update manifests or compare a slot device's revision.
    for folder, part in ((package / "presto_app", bundle),
                         (package / "presto_app_slot", bundle.get("slot")),
                         (package / "presto_app_boot", bundle.get("bootstrap"))):
        if part is None:
            continue
        for path, data in part["files"].items():
            destination = folder / path.lstrip("/")
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(data)
    return bundle


if __name__ == "__main__":
    staged = stage(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else sys.argv[1])
    print("Staged the Presto app, revision " + staged["revision"][:12])
