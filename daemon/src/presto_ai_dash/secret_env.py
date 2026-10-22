"""Read a secret from the environment, or from the file a ``*_FILE`` variable names.

A container platform can hand a secret over as a file (Docker and Compose
``secrets:`` appear under ``/run/secrets``), which keeps it out of
``docker inspect`` and out of the process environment. ``DASH_ADMIN_TOKEN_FILE``
is read the way ``DASH_ADMIN_TOKEN`` would be. Setting both is a mistake, and
says so rather than choosing one silently.
"""

import os

MAX_SECRET_BYTES = 4096


class SecretFileError(ValueError):
    """A ``*_FILE`` variable that cannot be used. Never carries the secret."""


def secret_from_environment(name, environ=None):
    """The value of ``name``, or the contents of the file ``name_FILE`` names.

    Returns None when neither is set. A trailing newline is dropped, as an
    editor or ``echo`` leaves one. The message of an error names the variable
    and the file path, never the contents.
    """
    names = os.environ if environ is None else environ
    direct = names.get(name)
    path = names.get(name + "_FILE")
    if not path:
        return direct or None
    if direct:
        raise SecretFileError("set either %s or %s_FILE, not both" % (name, name))
    try:
        with open(path, "rb") as handle:
            data = handle.read(MAX_SECRET_BYTES + 1)
    except OSError as error:
        raise SecretFileError("cannot read %s_FILE (%s): %s"
                              % (name, path, type(error).__name__)) from None
    if len(data) > MAX_SECRET_BYTES:
        raise SecretFileError("%s_FILE (%s) is larger than %d bytes"
                              % (name, path, MAX_SECRET_BYTES))
    try:
        value = data.decode("utf-8")
    except UnicodeError:
        raise SecretFileError("%s_FILE (%s) is not text" % (name, path)) from None
    value = value.rstrip("\r\n")
    if not value:
        raise SecretFileError("%s_FILE (%s) is empty" % (name, path))
    return value
