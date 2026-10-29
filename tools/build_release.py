"""Build the release files: a wheel that carries the Presto app, and an sdist.

    python tools/build_release.py              stage the app, build, check
    python tools/build_release.py --stage-only copy the app into the package only
    python tools/build_release.py --tag v0.50.0  also require the tag to match

The Presto app (``presto/``) is copied into ``presto_ai_dash/presto_app/`` using
the same public-file inventory as the USB sync, so private files never travel.
That folder is generated and ignored by git. The wheel is checked afterwards:
the app files are in it, and no private file is.
"""

import argparse
import hashlib
from pathlib import Path
import re
import shutil
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from presto_bundle import snapshot  # noqa: E402

DAEMON = ROOT / "daemon"
STAGE = DAEMON / "src" / "presto_ai_dash" / "presto_app"
PRIVATE = re.compile(r"(^|/)(dash\.toml|dash\.db[^/]*|[^/]*\.key|\.env[^/]*)$", re.IGNORECASE)
# Inside the device app, a config or secrets file (or a copy) is private too.
PRIVATE_APP = re.compile(r"(^|/)[^/]*(secret|config)[^/]*$", re.IGNORECASE)


def stage(root=ROOT, destination=STAGE):
    """Copy the app's public files into the package; return the snapshot."""
    bundle = snapshot(root)
    if destination.exists():
        shutil.rmtree(destination)
    for target, data in bundle["files"].items():
        path = destination / target.lstrip("/")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return bundle


def declared_versions():
    """The version in pyproject.toml and the one the package reports."""
    pyproject = (DAEMON / "pyproject.toml").read_text(encoding="utf-8")
    init = (DAEMON / "src" / "presto_ai_dash" / "__init__.py").read_text(encoding="utf-8")
    first = re.search(r'^version\s*=\s*"([^"]+)"', pyproject, re.MULTILINE)
    second = re.search(r'^__version__\s*=\s*"([^"]+)"', init, re.MULTILINE)
    if not first or not second:
        raise ValueError("could not read the package version")
    return first.group(1), second.group(1)


def tag_version(tag):
    """The PEP 440 package version a tag names.

    ``v0.50.0`` is ``0.50.0`` and ``v0.50.0-rc4`` is ``0.50.0rc4``. Each
    pre-release therefore builds its own version: PyPI and TestPyPI refuse a
    version twice, and pip can only order and upgrade distinct versions.
    """
    match = re.fullmatch(r"v(\d+\.\d+\.\d+)(?:-(a|b|rc)(\d+))?", tag)
    if not match:
        raise ValueError("tag %r is not vMAJOR.MINOR.PATCH or vMAJOR.MINOR.PATCH-rcN" % tag)
    base, kind, number = match.groups()
    return base if kind is None else "%s%s%d" % (base, kind, int(number))


def check_tag(tag):
    """The tag (``v0.50.0`` or ``v0.50.0-rc4``) must name the packaged version."""
    wanted = tag_version(tag)
    project, package = declared_versions()
    if project != package:
        raise ValueError("pyproject version %s and __version__ %s differ" % (project, package))
    if wanted != project:
        raise ValueError("tag %s needs the package version %s, not %s" % (tag, wanted, project))
    return project


def check_wheel(wheel, bundle):
    """The wheel holds every app file, byte for byte, and nothing private."""
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        for name in names:
            if PRIVATE.search(name) or (name.startswith("presto_ai_dash/presto_app/")
                                        and PRIVATE_APP.search(name)):
                raise ValueError("private-looking file in the wheel: " + name)
        for target, data in bundle["files"].items():
            inside = "presto_ai_dash/presto_app" + target
            if inside not in names:
                raise ValueError("missing from the wheel: " + inside)
            if hashlib.sha256(archive.read(inside)).digest() != hashlib.sha256(data).digest():
                raise ValueError("differs from presto/: " + inside)
        check_setup_template(archive)


def check_setup_template(archive):
    """The standalone installer template the admin page renders is present.

    It is not an app file (it is not installed on the Presto), but without it
    the admin page's **Set up a Presto** has nothing to serve, so a release
    missing it is not shippable.
    """
    name = "presto_ai_dash/setup/presto_setup_template.py"
    if name not in archive.namelist():
        raise ValueError("missing from the wheel: " + name)
    text = archive.read(name).decode("utf-8")
    if "BUNDLE = {}" not in text or "def main(" not in text:
        raise ValueError("the setup installer template is incomplete: " + name)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--stage-only", action="store_true")
    parser.add_argument("--tag", help="a release tag that must match the package version")
    args = parser.parse_args(argv)
    try:
        if args.tag:
            print("Tag %s matches version %s" % (args.tag, check_tag(args.tag)))
        bundle = stage()
        print("Staged the Presto app, revision " + bundle["revision"][:12])
        if args.stage_only:
            return 0
        dist = ROOT / "dist"
        if dist.exists():
            shutil.rmtree(dist)
        subprocess.run([sys.executable, "-m", "build", "--outdir", str(dist), str(DAEMON)], check=True)
        wheels = sorted(dist.glob("*.whl"))
        if len(wheels) != 1:
            raise ValueError("expected one wheel, found %d" % len(wheels))
        check_wheel(wheels[0], bundle)
        print("Checked %s: the app files are inside and nothing private is." % wheels[0].name)
        return 0
    except (ValueError, OSError, subprocess.CalledProcessError) as exc:
        print("build_release: " + str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
