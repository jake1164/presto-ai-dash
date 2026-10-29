"""The identity of the release artifact that is actually running.

A container image records the tag it was built from in
``DASH_RELEASE_VERSION`` (``0.50.0-rc4``), and that is the identity the admin
page shows and the setup handoff carries. A wheel has no such override, so it
reports its package version, which names the same release in PEP 440 form
(``0.50.0rc4``): the release workflow refuses a tag that does not match it.
Releases up to ``v0.50.0-rc3`` were built with the package version ``0.50.0``.

An unusable override is ignored rather than trusted: it is only shown, never
used to decide behaviour, but a value with spaces or control characters has no
place in a page or a handoff.
"""

import os
import re

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


def release_channel(environ=None):
    """``published`` for a tagged build, ``development`` for a source checkout.

    Only the presence of the build-time override is used. This is not the
    Part 4 registry channel: a PyPI wheel and a container image built by the
    release workflow are both published artifacts.
    """
    names = os.environ if environ is None else environ
    override = (names.get(ENV) or "").strip()
    return "published" if valid_version(override) else "development"
