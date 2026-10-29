"""Stage the public Presto app into the daemon package inside the image build.

A wheel built by ``tools/build_release.py`` carries the app under
``presto_ai_dash/presto_app/``, but that folder is generated and ignored by
git, so it is absent from a plain container build. Without this step the image
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
    folder = Path(target) / "daemon" / "src" / "presto_ai_dash" / "presto_app"
    for path, data in bundle["files"].items():
        destination = folder / path.lstrip("/")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
    return bundle


if __name__ == "__main__":
    staged = stage(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else sys.argv[1])
    print("Staged the Presto app, revision " + staged["revision"][:12])
