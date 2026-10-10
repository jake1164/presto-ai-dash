# ICON monitoring
# NAME AI Dash
# DESC Daemon status dashboard.

"""Pimoroni carousel entry; app modules live outside the launcher directory."""

import os
import sys

# MicroPython treats even folders without __init__.py as packages. Keep this
# name distinct from ai_dash.py or the carousel imports the folder instead.
APP_DIR = "/ai_dash_files"
sys.path.insert(0, APP_DIR)
os.chdir(APP_DIR)

from ai_dash_app import main

main()
