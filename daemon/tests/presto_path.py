"""Shared helpers for the Presto logic tests.

The Presto app modules live in ``presto/`` at the repository root, outside the
daemon package. These tests add that directory to ``sys.path`` so the pure
modules can be imported by CPython on a development machine.
"""

import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PRESTO_DIR = os.path.join(REPO_ROOT, "presto")

if PRESTO_DIR not in sys.path:
    sys.path.insert(0, PRESTO_DIR)
