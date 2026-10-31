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
STAGE_SLOT = DAEMON / "src" / "presto_ai_dash" / "presto_app_slot"
STAGE_BOOT = DAEMON / "src" / "presto_ai_dash" / "presto_app_boot"
PRIVATE = re.compile(r"(^|/)(dash\.toml|dash\.db[^/]*|[^/]*\.key|\.env[^/]*)$", re.IGNORECASE)
# Inside the device app, a config or secrets file (or a copy) is private too.
PRIVATE_APP = re.compile(r"(^|/)[^/]*(secret|config)[^/]*$", re.IGNORECASE)
# The revision scheme the device app declares for itself.
DEVICE_SCHEME = re.compile(r'^SCHEME\s*=\s*"([^"]+)"', re.MULTILINE)
# The schemes the protocol defines; a bundle must name one of them.
KNOWN_SCHEMES = ("legacy-v1", "slot-v1")
SLOT_REVISION_CHARS = 64


def check_bundle_identity(bundle):
    """The staged app and the daemon bundle must name the same revision scheme.

    A device reporting ``slot-v1`` against a ``legacy-v1`` daemon, or the
    reverse, can never be compared: every device would read as ``unknown``.
    The two declarations are checked at release time rather than discovered as
    a fleet-wide problem afterwards.
    """
    scheme = bundle.get("scheme")
    if scheme not in KNOWN_SCHEMES:
        raise ValueError("the app bundle does not declare a known revision scheme")
    if bundle.get("min_bootstrap") is not None:
        raise ValueError("the legacy install layout must not require a bootstrap version")
    source = bundle["files"].get("/ai_dash_files/lifecycle.py")
    if source is None:
        raise ValueError("missing from the app: /ai_dash_files/lifecycle.py")
    match = DEVICE_SCHEME.search(source.decode("utf-8"))
    if not match or match.group(1) != scheme:
        raise ValueError("the device app declares a different revision scheme")
    check_slot_identity(bundle.get("slot"), bundle.get("bootstrap"))
    return scheme


def check_slot_identity(slot, bootstrap):
    """The slot layout, when the sources ship one, must describe itself.

    A release that carries a slot bundle must carry a matching bootstrap:
    a slot nobody can boot is not an artifact. The reverse is allowed while
    the bootstrap is being developed, because a device can stay on USB.
    """
    if slot is None:
        if bootstrap is not None:
            raise ValueError("a bootstrap without a slot bundle cannot run a dashboard")
        return None
    if slot.get("scheme") != "slot-v1":
        raise ValueError("the slot bundle does not declare slot-v1")
    revision = slot.get("revision")
    if not isinstance(revision, str) or len(revision) != SLOT_REVISION_CHARS:
        raise ValueError("the slot bundle does not carry a full revision")
    if slot.get("min_bootstrap") != 1:
        raise ValueError("the slot bundle must declare its bootstrap requirement")
    names = {name.lstrip("/") for name in slot.get("files", {})}
    if not names:
        raise ValueError("the slot bundle is empty")
    if "ai_dash_app.py" not in names:
        raise ValueError("the slot bundle has no entry module")
    if bootstrap is None:
        return slot
    if bootstrap.get("version") != 1:
        raise ValueError("the bootstrap does not declare version 1")
    targets = set(bootstrap.get("files", {}))
    if "/ai_dash_boot.py" not in targets or "/ai_dash_boot_files/boot.py" not in targets:
        raise ValueError("the bootstrap bundle is incomplete")
    return slot


def stage(root=ROOT, destination=STAGE, slot_destination=None,
          boot_destination=None):
    """Copy the app's public files into the package; return the snapshot.

    The slot and bootstrap bundles are staged beside the legacy one, so a test
    that stages into a temporary folder never touches the checkout.
    """
    bundle = snapshot(root)
    destination = Path(destination)
    slot_destination = (Path(slot_destination) if slot_destination is not None
                        else destination.parent / "presto_app_slot")
    boot_destination = (Path(boot_destination) if boot_destination is not None
                        else destination.parent / "presto_app_boot")
    if destination.exists():
        shutil.rmtree(destination)
    for target, data in bundle["files"].items():
        path = destination / target.lstrip("/")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    # The slot and bootstrap bundles are installed at absolute device paths,
    # so the wheel stores them relative to their own folders.
    for folder, part in ((slot_destination, bundle.get("slot")),
                         (boot_destination, bundle.get("bootstrap"))):
        if folder.exists():
            shutil.rmtree(folder)
        if part is None:
            continue
        for target, data in part["files"].items():
            path = folder / target.lstrip("/")
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
            if PRIVATE.search(name):
                raise ValueError("private-looking file in the wheel: " + name)
            for folder in ("presto_app/", "presto_app_slot/", "presto_app_boot/"):
                if name.startswith("presto_ai_dash/" + folder) and PRIVATE_APP.search(name):
                    raise ValueError("private-looking file in the wheel: " + name)
        for target, data in bundle["files"].items():
            inside = "presto_ai_dash/presto_app" + target
            if inside not in names:
                raise ValueError("missing from the wheel: " + inside)
            if hashlib.sha256(archive.read(inside)).digest() != hashlib.sha256(data).digest():
                raise ValueError("differs from presto/: " + inside)
        for folder, part in (("presto_app_slot", bundle.get("slot")),
                             ("presto_app_boot", bundle.get("bootstrap"))):
            if part is None:
                continue
            for target, data in part["files"].items():
                inside = "presto_ai_dash/" + folder + "/" + target.lstrip("/")
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
        print("Staged the Presto app, revision " + bundle["revision"][:12] +
              " (" + check_bundle_identity(bundle) + ")")
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
