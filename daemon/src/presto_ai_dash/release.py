"""The identity of the release artifact that is actually running.

A container image records the tag it was built from in
``DASH_RELEASE_VERSION`` (``0.50.0-rc4``), and that is the identity the admin
page shows and the setup handoff carries. A wheel has no such override, so it
reports its package version, which names the same release in PEP 440 form
(``0.50.0rc4``): the release workflow refuses a tag that does not match it.
Releases up to ``v0.50.0-rc3`` were built with the package version ``0.50.0``.

Which registry a running copy is checked against is decided by
``installation()`` below, never by a user setting: a published wheel is
PyPI's, a published image is GHCR's, and anything else (a checkout, an editable
install, a locally built image) is a development build with no official
release to compare against.

An unusable override is ignored rather than trusted: it is only shown, never
used to decide behaviour, but a value with spaces or control characters has no
place in a page or a handoff.
"""

import json
import os
import re
from importlib import metadata
from pathlib import Path

ENV = "DASH_RELEASE_VERSION"
# A display label, never a comparison key: letters, digits and the punctuation
# PEP 440 and git tags use, at most 64 characters.
_VERSION = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._+-]{0,63}$")


def valid_version(value):
    """Whether a string is a usable release label."""
    return isinstance(value, str) and bool(_VERSION.match(value))


def release_version(environ=None):
    """The running release identifier, including RC metadata when present."""
    names = os.environ if environ is None else environ
    override = (names.get(ENV) or "").strip()
    if override and valid_version(override):
        return override
    from . import __version__
    return __version__


# The distribution name on PyPI, and the versions that several different
# builds shared. Up to v0.50.0-rc3 every release candidate was built with the
# package version 0.50.0, but none of those builds contains this check (it
# arrived in rc4). Code that runs it while reporting 0.50.0 is therefore the
# final release, so no version is ambiguous to it. The set stays, empty, for
# the policy that handles one.
DISTRIBUTION = "presto-ai-dash"
AMBIGUOUS_WHEEL_VERSIONS = frozenset()


class Installation(object):
    """What is running, and therefore which registry may be asked about it.

    ``kind`` is ``pip`` (a published wheel, from pip or pipx), ``docker`` (a
    published image) or ``development``. ``channel`` is ``pypi``, ``ghcr`` or
    ``development``. ``running`` is the exact identifier to show: the image's
    embedded release label, or the wheel's installed version. ``ambiguous`` is
    true for a wheel whose version several early candidates shared.
    """

    def __init__(self, kind, channel, running, ambiguous=False, reason=None):
        self.kind = kind
        self.channel = channel
        self.running = running
        self.ambiguous = ambiguous
        self.reason = reason

    @property
    def published(self):
        return self.kind != "development"

    def __repr__(self):  # pragma: no cover - debugging aid
        return "Installation(%r, %r, %r)" % (self.kind, self.channel, self.running)


def _published_wheel(distribution=None):
    """The installed version when this code is a published wheel, else None.

    The running code must be the files of an installed ``presto-ai-dash``
    distribution. A source checkout, an editable install, a local-directory
    install and a VCS install are all development: they are not a build
    anyone published, whatever version number they carry.
    """
    try:
        if distribution is None:
            distribution = metadata.distribution(DISTRIBUTION)
        import presto_ai_dash
        here = Path(presto_ai_dash.__file__).resolve()
        own = Path(distribution.locate_file("presto_ai_dash/__init__.py")).resolve()
        if here != own:
            return None
        # An installer writes INSTALLER and RECORD. A source tree's .egg-info
        # has neither, so a checkout that happens to carry one is not mistaken
        # for an installed wheel.
        if not distribution.read_text("INSTALLER") or not distribution.read_text("RECORD"):
            return None
        direct = distribution.read_text("direct_url.json")
        if direct:
            info = json.loads(direct)
            if not isinstance(info, dict) or "dir_info" in info or "vcs_info" in info:
                return None
        version = distribution.version
    except Exception:
        return None
    return version if isinstance(version, str) and valid_version(version) else None


def installation(environ=None, container=None, distribution=None):
    """Classify the running copy. See ``Installation``."""
    names = os.environ if environ is None else environ
    if container is None:
        from .sources import in_container
        container = in_container(names)
    override = (names.get(ENV) or "").strip()
    if container:
        if override and valid_version(override):
            return Installation("docker", "ghcr", override)
        return Installation("development", "development", release_version(names),
                            reason="local_image")
    version = _published_wheel(distribution)
    if version is not None:
        return Installation("pip", "pypi", version,
                            ambiguous=version in AMBIGUOUS_WHEEL_VERSIONS)
    return Installation("development", "development", release_version(names),
                        reason="checkout")


def release_channel(environ=None):
    """``published`` for a tagged build or an installed wheel, else ``development``.

    A wheel carries no build-time override, so the absence of
    ``DASH_RELEASE_VERSION`` says nothing about it: a published wheel is
    recognised by its installed distribution metadata instead.
    """
    names = os.environ if environ is None else environ
    override = (names.get(ENV) or "").strip()
    if valid_version(override):
        return "published"
    return "published" if _published_wheel() is not None else "development"
