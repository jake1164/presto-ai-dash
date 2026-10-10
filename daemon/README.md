# Presto AI Dash daemon

M2 uses Python 3.11+ and the standard library. Install with `pip install -e .`,
then run `presto-ai-dash --config ../examples/config.example.toml`.

Only the fake source is available. The API is `GET /api/v1/status` with
`Authorization: Bearer <shared-token>`. SQLite stores snapshots; retention,
alert history, trends and real integrations are future milestones.

Run host tests with `python -m unittest discover -s tests -v`.
