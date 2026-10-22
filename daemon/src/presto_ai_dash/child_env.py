"""The environment a helper program, such as Codex, is started with.

The daemon takes its secrets from environment variables: its own tokens and
the master key (``DASH_*``) and a provider key named by ``api_key_env``. A
helper it starts has no use for any of them, and anything the helper starts
in turn would inherit them. So they are left out. Everything else is passed on
unchanged, because a helper may need ``PATH``, ``HOME``, proxy settings and the
rest to run at all.
"""

import os

PREFIX = "DASH_"

# Provider key variables, by upper-cased name. LiteLLM's default is always
# hidden; load_config adds the one a configuration names.
_hidden = {"LITELLM_API_KEY"}


def hide(name):
    """Keep one more variable out of every helper's environment."""
    if isinstance(name, str) and name:
        _hidden.add(name.upper())


def is_hidden(name):
    upper = str(name).upper()
    return upper.startswith(PREFIX) or upper in _hidden


def child_environment():
    """A copy of the environment without the daemon's own secrets."""
    return {name: value for name, value in os.environ.items() if not is_hidden(name)}
