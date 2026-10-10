# M3 readiness and Luna handoff

Reviewed 2026-10-10 against commit `93f1a46`.

**Verdict: ready for bounded Luna implementation using the contract below.**
The original M3 description was too vague: M2 already stores snapshots and
serves the latest one. This brief defines the additional behaviour rather than
asking the implementation agent to invent a history platform.

This is a readiness deliverable only. Implementation is not authorized by this
review. M3 remains not started. No runtime code, provider integrations, or
device files were changed or deployed during the review.

## Verified starting point

- Working tree was clean at review start. The prior device work and future
  source drafts are now tracked in `93f1a46`; the drafts still have no daemon
  runtime path. Do not assume they are validated because they are committed.
- All **170 existing host tests pass** on Windows / Python 3.13. These include
  real localhost HTTP, bearer auth, SQLite round-trip/reopen, concurrent
  snapshot reads/writes, and daemon start/stop. Python 3.11 remains the floor;
  macOS/Linux execution was not verified in this review.
- `storage.py` has one `snapshots` table, an `RLock`, committed writes, and
  latest-per-source reads. Alerts are already preserved inside snapshot JSON.
- `app.py` collects fake data before serving requests and joins collectors
  before closing storage. It has no retention scheduling.
- `api.py` serves only `/api/v1/status`. `dashboard.py` already reports the
  latest fake snapshot's timestamp when data exists.
- The Presto runs its independent static, four-row scrolling demo. M3 needs
  no physical Presto, cloud account, Ollama, or LiteLLM service.

## Findings to address within M3

| Finding | Evidence | Required resolution |
| --- | --- | --- |
| Offset timestamps sort incorrectly | In-memory probe stored warning at `12:00+02:00` (10:00 UTC), then active at `11:00+00:00`. Latest returned warning. SQL currently orders the ISO strings. | Normalize persisted snapshot times to fixed-width UTC, including existing M2 rows, before sorting or pruning. |
| Fake alert timestamps are not observation times | Two warning collections had distinct snapshot times but identical alert `created_at`. Fake alert objects are created at module import. | History uses the parent snapshot's timestamp as `observed_at`; it must not present the embedded alert timestamp as a new incident time. |
| Missing data looks freshly timestamped | `build_status_payload({})` emits the current time while its cards are offline. | With no fake snapshot, return `updated_at: null`; retain offline/unavailable cards. |
| Active HTTP reads can outlive storage | A paused localhost status request survived `ApiServer.stop()`. Closing storage then releasing it produced `ProgrammingError` and HTTP 500. | Drain active storage-using request handlers and join collection/maintenance work before closing SQLite. |

The probes used in-memory databases and temporary localhost servers. They did
not alter the device or any persistent project database. The passing baseline
does not cover these cases yet; they need regression tests in M3.

## Proposed implementation contract

These decisions form the narrow handoff. They supersede the older, broader
readiness sketches for M3; those sketches still describe possible later work.

### 1. Storage and existing databases

- Retain the existing snapshot columns and `save_snapshot` / `latest_snapshots`
  interfaces. Keep the stdlib connection and shared lock; no ORM or new
  database framework.
- Persist `updated_at` in UTC with six fractional digits and a `+00:00`
  suffix. Offset-aware inputs convert to UTC; naive inputs mean UTC, matching
  the existing `models.iso` convention. Return aware UTC datetimes on read.
  Apply the same normalization to query boundaries.
- Use a small, explicit schema version via SQLite `PRAGMA user_version`:
  existing M2 databases are version 0; the M3 schema is version 1. Migrate in
  one explicit transaction. Normalize old snapshot timestamps and backfill
  alert observations from their JSON before marking version 1.
- Preserve snapshot IDs, metrics, status, errors, and embedded alert JSON.
  Reopening version 1 must not repeat the migration or duplicate observations.
- An unreadable legacy timestamp or invalid alert payload must fail migration
  with a clear error and leave the version/data/schema transaction unchanged.
  Close the failed connection. Never silently drop records or recreate a DB.
  Reject an unsupported newer schema version rather than downgrading it.
- Add indexes appropriate for per-source timestamp/ID ordering and global
  timestamp cleanup. Keep these changes in `storage.py`.

### 2. Internal history queries

Expose storage methods, not new HTTP endpoints:

```text
snapshot_history(source_id, since, until=None, limit=1000)
alert_history(source_id, since, until=None, limit=1000)
```

- Exact source filter, inclusive `since`, exclusive `until` when supplied.
  Require `until > since`. Without `until`, impose no upper time boundary.
- Select at most the newest `limit` matching records, then return them in
  chronological order. Timestamp ties use increasing record ID.
- `limit` must be an integer from 1 through 1000; reject booleans, invalid
  types, and out-of-range values with `ValueError`. Empty matches return `[]`.
- Snapshot history returns `SourceSnapshot` objects. Alert history returns
  dictionaries containing `id`, `snapshot_id`, `source_id`, `severity`,
  `message`, and an aware UTC `observed_at` datetime. These are internal
  objects, not a new public JSON contract.

### 3. Alert observations, not incident tracking

- Add a minimal `alerts` table: `id`, `snapshot_id`, `severity`, `message`.
  Link it to snapshots with a foreign key and cascade deletion; enable foreign
  keys on the connection. Derive source and observation time from the parent.
- A snapshot and its alert observations commit atomically under the existing
  lock. A failed write leaves neither a partial snapshot nor partial alerts.
- Deduplicate identical `(severity, message)` pairs **within one snapshot**
  using a unique constraint on `(snapshot_id, severity, message)`.
  Repeated observations in later snapshots are deliberately retained; they
  are not evidence of separate incidents. Do not implement cross-poll incident
  deduplication, acknowledgements, resolutions, or escalation rules.
- Validate reported alerts as dictionaries with canonical status severity and
  a nonempty string message. Invalid alerts fail the write/migration instead
  of being silently discarded. Ignore their embedded source/time for indexing;
  retain their original JSON in the snapshot.
- Do not import or activate the draft daemon `alerts.py`. M3 persists reports
  already emitted by `fake`; it does not calculate new alerts or trends.

### 4. Retention and lifecycle

- Add `daemon.retention_hours = 72` and
  `daemon.cleanup_interval = 300` (seconds) to defaults/example TOML.
  Both must be positive integers, rejecting booleans. Old TOML configs keep
  working without these keys. No new environment-variable overrides are
  needed for M3.
- Provide `prune(now, retention_hours)` with an explicit clock argument for
  deterministic tests. Delete snapshots strictly older than
  `now - retention_hours`; keep the exact cutoff. Delete their alert rows
  atomically through the cascade. Return deleted snapshot/alert counts.
- Retention applies to every source, including its latest snapshot. No
  indefinite exemption for the last row. After all fake data expires, latest
  reads return no fake snapshot and status renders offline with a null timestamp.
- Run cleanup once at daemon startup after initial fake collection and before
  accepting HTTP requests. Then use one maintenance worker with
  `stop_event.wait(cleanup_interval)` between sweeps. Do not prune on every
  HTTP request or every collection, and do not add cron/service dependencies.
- Log periodic cleanup failures and continue the next scheduled sweep. A
  startup migration/cleanup failure must prevent the API from becoming ready.
- On stop, signal workers, stop accepting HTTP, drain active handlers, join
  collector/maintenance workers, then close storage. Setup/start failures must
  also release resources without hanging. An idle maintenance wait must wake
  promptly on stop. Keep the existing no-start shutdown test passing.
- Give accepted HTTP sockets a finite I/O timeout (3 seconds) so a client
  waiting mid-request cannot prevent handler draining indefinitely. This is
  lifecycle hardening for the reproduced race, not a new endpoint. Test an
  idle/partial-header client as well as an active storage read during stop.
- A `settings` table is unnecessary for this contract: cadence is in-process
  and startup always sweeps. Defer it until there is actual persistent state.

### 5. Status response

- Keep `/api/v1/status`, bearer authentication, top-level keys, card fields,
  and the three daemon card IDs/order unchanged.
- With a fake snapshot, `updated_at` is that snapshot's persisted UTC time.
  Reading or reopening storage cannot refresh it. A new collection after
  restart legitimately produces a new timestamp. With none, it is JSON `null`.
  This is the one intentional value-contract clarification; document it and
  check that existing Presto card parsing accepts the offline response.
- Do not add history/trend fields, new endpoints, retention diagnostics,
  or cloud quotas to the status API. History methods are tested internally.

## Acceptance checks for implementation

| Area | Required evidence |
| --- | --- |
| Migration | A version-0 fixture with mixed offsets and alerts upgrades without data loss; equivalent timestamps tie by ID; reopen is idempotent; invalid legacy data rolls back; newer versions are rejected. |
| History | Source isolation, both range boundaries, chronological ordering/ties, newest-N limiting, invalid limits/ranges, empty results, reopen persistence. |
| Alert writes | Parent-derived observation time, within-snapshot deduplication, repeated observations across snapshots, invalid-alert rejection, transaction rollback on an induced write failure. |
| Retention | Before/exactly-at/after the 72-hour cutoff; latest expired row removed; alerts cascade; repeated prune is harmless; retention changes are honoured. |
| Scheduling/config | Defaults and TOML overrides; invalid types; old config compatibility; startup sweep; periodic sweep with a controlled wait/clock; stop interrupts an idle wait; sweep failure does not kill collection. |
| Concurrency/shutdown | Collection, history/latest reads, and pruning can overlap without SQLite errors or orphaned alerts; use barriers/events for the active-request shutdown regression; an idle/partial-header client cannot hang shutdown. |
| API | Valid token returns 200; missing/wrong tokens return 401; stored update time survives reopen; empty/pruned data returns offline and null update time; no new routes or fields. |
| Executability | Import/compile check, all existing plus new tests, and a separate-process fake-only daemon smoke using a temporary file DB: status with/without auth, clean stop, restart on the same DB. |

Use temporary databases and controlled clocks/events. Do not sleep for a real
72-hour window or rely on timing luck in concurrency tests. No external services
or hardware are required. Do not promise a final test count before tests exist.

## Scope and implementation sequence

Expected runtime files: `storage.py`, `config.py`, `app.py`, `dashboard.py`,
and `api.py` only for safe handler shutdown. A narrowly required timestamp
helper may live in `models.py`. Add focused `daemon/tests/test_m3.py` coverage,
adjust affected M2 expectations, and update config/docs to match final behaviour.

Suggested sequence for Luna:

1. Storage migration, UTC ordering, bounded snapshot/alert history, atomicity.
2. Retention configuration and deterministic pruning tests.
3. Maintenance lifecycle, handler draining, and status timestamp clarification.
4. Full regression suite and separate-process smoke; report the diff and stop.

Each step builds on the previous tests. Avoid unrelated refactors. Keep real
sources disabled; leave future provider/alert/client drafts untouched. Do not
change Presto files, live polling, quota UI, packaging, command names, Docker,
tray code, trend calculations, alert rules, or external integrations. Do not
commit or deploy unless separately instructed.

The [future application monitoring checkpoint](future.md#application-usage-monitoring)
supports multiple configured apps after M5. It does not require an app registry,
application-specific tables, attribution adapters, budgets, or new cards in M3.
Keep source history generic; do not hardcode a book app or a single application.

Luna with High reasoning is the proposed implementation setting. The task now
has concrete contracts and deterministic checks; migration, atomicity, and
shutdown deserve a final diff review before any commit. If the existing schema
differs from this baseline, stop and report the discrepancy instead of inventing
a broader migration system.

## Handoff prompt for the next authorized implementation

> Implement M3 only using `docs/m3-readiness.md` as the contract. First verify
> the current baseline and preserve any unrelated working-tree changes. Keep
> the daemon fake-only and follow the four-step sequence. Prove migration,
> history, alert observations, retention, lifecycle, and status/auth behaviour
> with the specified tests and a temporary-DB process smoke. Update docs with
> actual results. Do not modify the Presto, enable draft integrations, add
> unrelated features, commit, or deploy. Stop with a reviewable diff and test
> summary; explain any deviation from the contract.
