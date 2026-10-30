# ICON monitoring
# NAME AI Dash
# DESC Daemon status dashboard.

"""Stable carousel entry that chooses which dashboard slot runs.

This file is installed as ``/ai_dash_boot.py`` and is never replaced by a
dashboard update. It shares no name with ``/ai_dash_boot_files`` because
MicroPython resolves a same-named folder as a namespace package before the
module (measured in the hardware report).

The decision itself lives in ``activation.py``, which is pure Python and is
tested off the device. This module only supplies the device.
"""

import os
import sys

BOOT_DIR = "/ai_dash_boot_files"
if BOOT_DIR not in sys.path:
    sys.path.insert(0, BOOT_DIR)

import boot

boot.main()
