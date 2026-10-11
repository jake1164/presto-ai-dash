# Presto AI Dash daemon

The daemon uses Python 3.11+ plus `cryptography` for encrypted keys. Install with `pip install -e .`,
then run `presto-ai-dash --config ../examples/config.example.toml`.

The fake source is enabled on a fresh database by default. Use `--admin` and
the local UI to disable fake and add/configure live provider accounts. Saved
source settings and encrypted keys live in SQLite. The API is `GET /api/v1/status` with
`Authorization: Bearer <shared-token>`. SQLite stores UTC-normalized snapshots
and alert observations, keeps bounded history for 72 hours, and removes expired
records on a 300-second maintenance interval. History remains internal; there
are no history endpoints or trend calculations. See [`docs/sources.md`](../docs/sources.md) for
provider coverage and the Ollama history limitation.

Run with `--admin` to enable the localhost configuration page on port 8788.
It supports multiple source instances and stores provider keys encrypted in
SQLite with a master key held outside the database. See
[`docs/configuration.md`](../docs/configuration.md) for setup, replacement, and recovery.
OpenCode Go reports subscription percentages and resets. Ollama Cloud reports
dated activity; its monthly credit allowance is unavailable in the current API.
Codex subscription reporting uses an existing local ChatGPT login; account
settings are saved through the UI. See [Codex setup and remaining login work](../docs/codex.md).
Claude uses a UI-configured [terminal quota exporter](../docs/claude.md) that
writes minimized telemetry directly to SQLite. Live terminal readings are verified. See
[`docs/quotas.md`](../docs/quotas.md) for current cloud coverage.

Run host tests with `python -m unittest discover -s tests -v`.
