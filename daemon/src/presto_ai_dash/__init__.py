"""Presto AI Dash daemon.

A small, dependency-free Python daemon that collects fake demo snapshots,
stores them in SQLite, and serves an authenticated status payload over HTTP.
Real Ollama and LiteLLM sources are future milestones.

See ``docs/architecture.md`` for the shape of the system.
"""

__version__ = "0.1.0"

APP_NAME = "presto-ai-dash"
