"""Presto AI Dash daemon.

A small, dependency-free Python daemon that polls local AI sources (fake,
Ollama, LiteLLM), normalises them into a single status payload, stores recent
snapshots in SQLite, and serves them to a Pimoroni Presto over HTTP.

See ``docs/architecture.md`` for the shape of the system.
"""

__version__ = "0.1.0"

APP_NAME = "presto-ai-dash"
