"""Environment isolation that keeps the test runner's own variables intact.

``patch.dict(os.environ, {}, clear=True)`` also wipes variables such as the ones
VS Code's Python extension uses to report results, which silently ends the run.
"""

import os
from unittest.mock import patch

PROJECT_PREFIXES = ("DASH_", "LITELLM_", "OLLAMA_", "CODEX_")


def clean_env(**extra):
    """Patch os.environ without project settings, optionally adding ``extra``."""
    kept = {key: value for key, value in os.environ.items() if not key.startswith(PROJECT_PREFIXES)}
    kept.update(extra)
    return patch.dict(os.environ, kept, clear=True)
