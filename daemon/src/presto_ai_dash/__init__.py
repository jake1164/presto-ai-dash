"""Presto AI Dash daemon.

A small Python daemon that collects usage and quota readings from AI services,
stores them in SQLite, and serves an authenticated status payload over HTTP to
the Presto.

See https://jake1164.github.io/presto-ai-dash/docs/developers/architecture/ for the
shape of the system.
"""

__version__ = "0.50.0"

APP_NAME = "presto-ai-dash"
