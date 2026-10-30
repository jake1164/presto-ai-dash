"""Which published release, if any, a running installation should hear about.

Pure functions: no network, no storage, nothing Presto-specific. Registry
adapters hand in the labels they found; this module decides which of them are
real numbered releases and which one a given running release is offered.

A numbered release is ``X.Y.Z`` or ``X.Y.Z`` plus an ``aN``, ``bN`` or ``rcN``
pre-release. Python spells a candidate ``0.50.0rc4`` and a container tag spells
it ``0.50.0-rc4``; both normalize to one ``packaging.version.Version`` for
ordering, and the exact spelling a registry published is kept as the label to
show. Floating names (``latest``, ``0.50``, ``0``), prefixes (``v0.50.0``),
build tags and anything else are not releases, even where a permissive parser
would read them.

Policy: a stable installation hears about newer stable releases only. A
candidate hears about later candidates of its own release and about any newer
stable release.
"""

import re

from packaging.version import InvalidVersion, Version

# At most four digits per part keeps a hostile tag from becoming a huge number.
_PYTHON = re.compile(r"(\d{1,4})\.(\d{1,4})\.(\d{1,4})(?:(a|b|rc)(\d{1,4}))?")
_DOCKER = re.compile(r"(\d{1,4})\.(\d{1,4})\.(\d{1,4})(?:-(a|b|rc)(\d{1,4}))?")

# The last candidate that was built with the bare package version. A wheel that
# says only ``0.50.0`` and has no final release behind it is one of rc1-rc3.
PRE_METADATA_BASELINE = Version("0.50.0rc3")


def _build(match):
    major, minor, patch, kind, number = match.groups()
    text = "%s.%s.%s" % (major, minor, patch)
    if kind:
        text += "%s%s" % (kind, number)
    try:
        return Version(text)
    except InvalidVersion:  # pragma: no cover - the patterns only allow valid text
        return None


def parse_python(text):
    """A ``Version`` for a numbered release in Python spelling, else None."""
    match = _PYTHON.fullmatch(text) if isinstance(text, str) else None
    return _build(match) if match else None


def parse_docker(text):
    """A ``Version`` for a numbered release in container-tag spelling, else None."""
    match = _DOCKER.fullmatch(text) if isinstance(text, str) else None
    return _build(match) if match else None


def parse_running(text):
    """A ``Version`` for a running release label in either spelling, else None.

    The label itself is kept for display; this is only the ordering key.
    """
    return parse_python(text) or parse_docker(text)


class Release(object):
    """One published numbered release: its ordering key and its exact label."""

    __slots__ = ("version", "label")

    def __init__(self, version, label):
        self.version = version
        self.label = label

    def __repr__(self):  # pragma: no cover - debugging aid
        return "Release(%r)" % (self.label,)


def eligible(running, candidate):
    """Whether ``candidate`` is a newer release this installation should hear of."""
    if candidate <= running:
        return False
    if candidate.is_prerelease and not running.is_prerelease:
        return False
    if candidate.is_prerelease:
        # Candidates of other releases are not this installation's progression.
        return candidate.base_version == running.base_version
    return True


class Outcome(object):
    """The result of comparing a running release with what was published."""

    def __init__(self, state, latest, running_note=None):
        self.state = state          # "available" or "current"
        self.latest = latest        # Release or None
        self.running_note = running_note


def choose(running_label, releases, ambiguous=False):
    """Compare the running release with the published ones.

    ``releases`` is a list of ``Release``. Returns an ``Outcome``, or None when
    ``running_label`` is not a numbered release at all (the caller reports
    ``running_version_unknown``: nothing is guessed).

    ``ambiguous`` marks a wheel whose version several early candidates shared.
    If that exact version has really been published as a final release, the
    wheel is compared as that release. If it has not, the wheel cannot be the
    final release, so it is compared as a candidate older than the first one
    that carries its number: it is offered later candidates and the final
    release, and the missing candidate number is never invented.
    """
    running = parse_running(running_label)
    if running is None:
        return None
    note = None
    if ambiguous:
        if any(release.version == running for release in releases):
            note = "version_ambiguous"
        else:
            running = PRE_METADATA_BASELINE
            note = "pre_release_unspecified"
    newer = [release for release in releases if eligible(running, release.version)]
    if newer:
        return Outcome("available", max(newer, key=lambda r: r.version), note)
    # Nothing newer: the newest published release the policy shows, when the
    # running one is among them. Never a release older than the running one.
    visible = [release for release in releases
               if release.version == running
               and (ambiguous is False or note == "version_ambiguous")]
    return Outcome("current", visible[0] if visible else None, note)
