# Presto AI Dash daemon

The daemon uses Python 3.11+ plus `cryptography` for encrypted keys. Install with `pip install -e .`,
then run `presto-ai-dash --config ../examples/config.example.toml`.

The fake source is enabled by default. To use Ollama or LiteLLM, disable fake
and enable the provider in TOML. The API is `GET /api/v1/status` with
`Authorization: Bearer <shared-token>`. SQLite stores UTC-normalized snapshots
and alert observations, keeps bounded history for 72 hours, and removes expired
records on a 300-second maintenance interval. History remains internal; there
are no history endpoints or trend calculations. See `docs/sources.md` for
provider coverage and the Ollama history limitation.

Run with `--admin` to enable the localhost configuration page on port 8788.
It supports multiple source instances and stores provider keys encrypted in
SQLite with a master key held outside the database. See
[`docs/configuration.md`](../docs/configuration.md) for setup, replacement, and recovery.

Run host tests with `python -m unittest discover -s tests -v`.
