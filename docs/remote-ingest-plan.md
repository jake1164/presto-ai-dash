# Plan: optional observation ingest (local and remote collectors)

Status (2026-10-20): **Phases 1-5 and 9 are implemented; Phases 1-5 are
committed.** The database schema is v7. Next: 7. Phases 5b, 5c, 6 and 8 are
optional. Written for an implementing agent. Read
`AGENTS.md`, `docs/claude.md`, `docs/sources.md` and `docs/quotas.md` first. Do
not start a phase until the previous one passes its tests.

| Phase | What | State |
| --- | --- | --- |
| 1 | Observations storage (schema v6) and `ingest()` | Done |
| 2 | Standalone collector `collectors/claude_statusline.py` | Done |
| 3 | `POST /api/v1/ingest`, tokens (schema v7), admin controls | Done |
| 4 | Freshness hints and the Presto display | Done (2026-10-18) |
| 5 | Claude activity from local session logs (VS Code panel and CLI); 5b Stop hook and 5c remote push optional | Done (2026-10-19) |
| 5d | Claude usage cache reader: quota for VS Code panel users from `~/.claude.json` | Done (2026-10-20) |
| 6 | Codex over push | Optional |
| 7 | Documentation site on GitHub Pages (7a site and user docs, 7b developer docs and the adding-a-source guide and template, 7c in-app help links) | 7a, 7b and 7c done (2026-10-21) |
| 8 | OpenTelemetry activity detail | Optional, only if asked |
| 9 | Banked reset credits (Codex) | Done (2026-10-20) |

## Goal

1. Unchanged default: one daemon process. A person using Claude Code in a
   terminal on the same machine keeps today's local status-line export, with
   the same admin-UI setup button, and nothing new is listening.
2. Opt-in: an admin toggle (and an environment variable for containers) lets
   the existing daemon API accept pushed observations. A collector on another
   computer (Windows, macOS, Linux) sends normalized, credential-free readings
   to a daemon that may run in Docker.
3. The push format is provider-neutral. It carries everything the Claude
   export carries today and can carry Codex or a future provider's windows.
4. The display tells the user why Claude data is missing or old and what to do
   ("run claude in a terminal"), without calling the provider offline.
5. Document how to add a provider, with a template module.

**Simplicity rule for every phase:** the end user does at most one action per
computer. Locally that is the existing "Set up Claude export" button, which
installs everything this plan needs on that machine in one step. Remotely it is
one pasted snippet plus one token file. Anything that needs more (extra
services, environment variables, an OpenTelemetry collector) stays optional
and off by default.

## Hard rules

- Code shipped in this repository never reads, copies, mounts or forwards
  provider credentials, OAuth tokens, cookies, keychain items or
  `~/.claude/.credentials.json`, and never calls undocumented provider
  endpoints. Anthropic's policy forbids collecting or intermediating Claude.ai
  credentials or session tokens
  (<https://code.claude.com/docs/en/legal-and-compliance>). This rule is
  about what the project ships and documents. The ingest protocol is open;
  what someone builds outside the repository is their own responsibility, and
  the daemon does not try to police it. Its validation exists for data
  integrity, not to detect how a reading was collected.
- Never launch Claude Code or make model requests to generate data.
- The whole app is read-only toward providers (see AGENTS.md). Never call any
  provider method that spends or changes anything, for example Codex
  `ConsumeAccountRateLimitResetCredit`. The ingest endpoint only stores
  observations: it cannot trigger a provider call, a login, a settings edit or
  any other action, and nothing a remote collector sends can cause one.
- Never store prompts, transcripts, tool contents, session IDs, account email
  or account UUIDs. Hashes of progress markers are allowed (as today). The
  Phase 5 activity reader opens local session files, which contain prompts,
  but extracts only the fields listed in Phase 5 (timestamps, model, token
  counts) and never stores, logs or sends anything else.
- Missing, expired or old readings are never shown as a fresh 0%.
- **Provenance and no loss of detail (decided 2026-10-20).** Every number or
  state shown to the user says where it came from: a quota reading from the
  terminal status line (with its time and host), activity from local session
  logs, or a reading pushed from another computer. A change must not remove an
  existing detail, explanation or capability unless it replaces it with
  something at least as informative; when adding a new source of information,
  label it rather than blending it into an existing line. Applies to the
  Presto screens, the detail cards, the admin page, the API payload and the
  docs.
- Keep `AGENTS.md` rules: tests via the repo `.venv`, PSL visual check, and
  device sync after any `presto/` change.

## Facts the plan relies on (verified in the code on 2026-10-18)

The first six bullets describe the code **before** Phase 1 and are kept for
the reasoning. Today: `Storage.save_observation` replaced
`save_claude_export`, `claude_exports` became the `observations` table, the
helper builds an envelope and calls `ingest()`, and the schema is v7.

- The daemon API (`api.py`) already listens on `daemon.host:daemon.port`
  (default `0.0.0.0:8787`) with a bearer shared token, GET
  `/api/v1/status` only (Phase 3 added the ingest POST). The Presto device
  holds that shared token.
- The admin page (`admin.py`) binds `127.0.0.1` and checks the `Host` header
  against `127.0.0.1`/`localhost`. **It is unreachable from outside a Docker
  container.** See "Not covered" below.
- Claude today: `claude_export.py` is the status-line command. It normalizes
  `rate_limits.five_hour`/`seven_day`, computes evidence hashes (progress
  markers: `cost.total_api_duration_ms`, context token totals) and writes
  straight to SQLite via `Storage.save_claude_export`, which keeps a window's
  old `observed_at` when its evidence hash is unchanged. It also chains the
  user's previous status line, stored encrypted in SQLite (`claude_bridges`).
  This only works on the machine that holds `dash.db`.
- Codex today: `sources/codex.py` spawns a local `codex app-server` using the
  local Codex login. Inside Docker that would need Codex and its login in the
  container. Push solves this the same way it solves Claude (optional phase).
- Source discovery skips modules whose names start with `_`, so
  `sources/_template.py` will not register.
- Storage schema was `PRAGMA user_version = 5` before Phase 1 (now 7). A
  migrated database cannot be opened by code older than the migration; back up
  `dash.db` before running new code against it.

## Collection methods considered

Researched 2026-10-17/18 against Anthropic's docs. "User effort" is what an
end user does once per computer.

| Method | Provides | Quota | Surfaces | User effort | Decision |
| --- | --- | --- | --- | --- | --- |
| Status line (`statusLine` command) | 5h and weekly `used_percentage`, `resets_at`; response-progress markers | **Yes, the only supported source** | Terminal CLI only; the VS Code panel does not run it (public issues; the author's own install received nothing for a week) | One button locally; one snippet remotely | **Core (Phases 1-3)** |
| `Stop` hook (`hooks` setting) | That a reply finished; time of last activity | No | Terminal and IDE extensions per hook docs | Installed by the same button/snippet | **Phase 5**: makes the "use the CLI" hint exact |
| OpenTelemetry (`CLAUDE_CODE_ENABLE_TELEMETRY`, OTLP) | `api_request`: timestamp, model, duration, tokens, cost | No | CLI documented; VS Code and headless implied | Several environment variables per computer; always sends account email | **Phase 8, optional** |
| Local session logs (`<profile>/projects/**/*.jsonl`) | Per-request time, model, token counts | No | **CLI and VS Code panel** (verified) | One checkbox; reads only timestamps, model and token counts, never text | **Phase 5**: the only always-on activity signal for panel users. Unofficial file format; this machine only |
| `/usage` dialog, claude.ai usage page | Full usage bars | Viewable only | Panel, browser | Manual | **Not collectable**; mention in docs as the manual check |
| Anthropic Admin / Console usage API | API-key spend | No (different product) | n/a | API key | **Out of scope** for subscriptions |
| OAuth usage endpoint, browser cookies, `/usage` screen scraping (as used by CodexBar) | Quota | Yes | n/a | Credential access or CLI launches | **Not shipped** (credential policy, undocumented endpoints) |

## Decisions (with reasons)

1. **Reuse the existing API port, add a route. No second port.** Docker then
   publishes one port, there is one bind setting, and one server to harden.
   The route is disabled unless turned on. A separate port only helps if
   someone wants to firewall ingest differently from Presto reads; add
   `ingest.port` later if that need appears.
2. **Per-account ingest tokens, never the Presto shared token.** A leaked
   ingest token can only write readings for one account. Tokens are generated
   by the daemon, shown once, stored as SHA-256 hashes, and can be rotated.
3. **The daemon owns time.** `observed_at` is the daemon's receive time. The
   client's clock is informational only. This removes clock-drift bugs on
   remote machines.
4. **One ingest function, two transports.** `ingest(instance_id, envelope)`
   is called in-process by the local status-line helper (SQLite, works while
   the daemon is stopped, as today) and by the HTTP route for remote
   collectors. One validator, one freshness rule.
5. **Latest observation wins per window.** Several computers using one Claude
   account report the same account-wide quota; the newest reading is the
   best one. Different accounts are different source instances.
6. **The daemon decides user-facing hints.** The device stays simple: it draws
   a short ASCII hint string supplied in the payload.

## Envelope v1 (the generic protocol)

`POST /api/v1/ingest` with `Authorization: Bearer <ingest token>` and
`Content-Type: application/json`. The token identifies the source instance;
the body never names one.

```json
{
  "schema": "presto-ai-dash.observation/1",
  "collector": {"name": "claude-statusline", "version": "1", "host": "laptop"},
  "provider_version": "2.1.283",
  "client_time": "2026-10-18T14:02:11Z",
  "windows": [
    {"id": "five_hour", "label": "5h", "period_minutes": 300,
     "used_percent": 12, "resets_at": "2026-10-18T18:00:00Z", "state": "available"},
    {"id": "seven_day", "label": "Weekly", "period_minutes": 10080,
     "used_percent": null, "resets_at": null, "state": "unavailable"}
  ],
  "evidence": {"five_hour": "<sha256 hex>", "seven_day": "<sha256 hex>"},
  "progress": "<sha256 hex or null>"
}
```

- `windows` absent means a heartbeat: the collector ran but had no quota
  (Claude before its first response). Heartbeats update collector
  connectivity only and never touch quota. An empty `windows` list is
  rejected; a collector with nothing to report omits the key.
- `collector` must contain exactly `name`, `version` and `host`.
- `windows[*]` must contain exactly `id`, `label`, `period_minutes`,
  `used_percent`, `resets_at` and `state`. They follow the existing
  `quota_window` rules from `quotas.py`: `state` is one of the existing quota
  states; `used_percent` is null unless measured; `resets_at` is ISO-8601 with
  an offset.
- `client_time` is validated but informational. The daemon stamps every stored
  time (`observed_at`, `collector_seen_at`, `last_progress_at`) with its own
  receive time.
- `evidence` keeps today's "a cached redraw is not new data" rule: a window
  whose hash matches the stored one keeps its previous `observed_at`.
- `progress` is an opaque hash that changes only when the provider did real
  work (Claude: API duration and token totals). When it changes, the daemon
  sets `last_progress_at` to now. This is the "last response seen" signal.
- `host` is a user-chosen label, at most 32 ASCII characters, used only to
  show which computer reported last.
- Optional `reset_credits` (added by Phase 9; see there): spendable early
  resets the provider reports. Absent means "not reported", which is different
  from a count of 0.

Validation (reject with 400, store nothing, on any failure):

- Body at most 16 KiB; UTF-8 JSON object; `schema` exactly as above.
- Unknown top-level or window keys are rejected, not ignored.
- At most 8 windows; window `id` matches `[a-z0-9_]{1,32}` and, if the source
  declares `push_window_ids`, must be in that list; no duplicate ids.
- `used_percent` null or a finite number 0 to 1000; `period_minutes` null or
  an integer 1 to 527040; `resets_at` null or within 400 days of now.
- Strings ASCII-printable with length caps; hashes are 64 lowercase hex.

Responses: `204` accepted, `400` invalid, `401` bad token, `404` when ingest
is disabled (do not reveal the route), `413` too large, `429` when the same
token posts again within 2 seconds. The response body never echoes input.

## Phases

### Phase 1: storage and the ingest function (daemon only)

Done 2026-10-18: implemented with tests. The `ingest_tokens` table was built in
Phase 3 as part of schema v7, not in v6.

- Schema v6 migration: table `observations(instance_id PK REFERENCES
  source_instances ON DELETE CASCADE, report TEXT, evidence TEXT,
  collector_seen_at TEXT, collector_host TEXT, collector_version TEXT,
  last_progress_at TEXT, progress TEXT, last_activity_at TEXT)`. Create
  `last_activity_at` now (unused until Phase 5) to avoid a second migration. Copy rows from `claude_exports`,
  then drop it.
- New module `ingest.py`: `validate(envelope, source_class)` and
  `ingest(storage, instance_id, envelope, now)`. Move the evidence/freshness
  logic out of `Storage.save_claude_export` into a generic
  `Storage.save_observation`.
- `Source` gains `accepts_push = False` and `push_window_ids = None`. Claude
  sets `accepts_push = True` and `push_window_ids = ("five_hour", "seven_day")`.
- `ClaudeSource.collect()` reads from `observations` and adds
  `collector_seen_at`, `collector_host`, `collector_version` and
  `last_progress_at` to its quota report. The `hint` is added in Phase 4, when
  the status payload is built.
- `claude_export.py` keeps its CLI and local behaviour but builds an envelope
  and calls `ingest()` in-process. Its normalization moves to the shared
  collector module (Phase 2) so both transports use the same code.
- Tests: migration from a v5 database with a Claude export; validator
  rejections (each rule above); heartbeat does not alter quota; unchanged
  evidence keeps `observed_at`; changed progress sets `last_progress_at`;
  the newest reading wins; deleting the instance deletes its observation.

### Phase 2: standalone collector

Done 2026-10-18: implemented with tests; normalization moved into the collector module and `claude_export.py` reuses it.

- New file `daemon/src/presto_ai_dash/collectors/claude_statusline.py`,
  **stdlib only, Python 3.8+**, runnable as a script and importable by the
  package. It is the single copy of Claude normalization.
- Script mode for remote machines:

  ```text
  python3 claude_statusline.py --url http://server:8787 \
      --token-file ~/.config/presto-ai-dash/claude.token \
      --host laptop -- <previous status line command, optional>
  ```

  - Reads status-line JSON from stdin (max 64 KiB), builds the envelope,
    POSTs with a 1.5 s total timeout, never raises, always exits 0.
  - Chains the previous command given after `--` by passing it the same stdin
    and printing its stdout; otherwise prints a short status text.
  - Token comes from a file, never the command line, so `settings.json` can be
    shared or synced without leaking it.
  - Client throttle state in a per-user directory (`%LOCALAPPDATA%` or
    `$XDG_STATE_HOME`/`~/.local/state`, owner-only, symlinks refused): skip a
    heartbeat if one was sent under 60 s ago; skip an identical envelope sent
    under 60 s ago; after a network failure, skip all posts for 5 minutes. The
    status line must never stall on an unreachable server.
  - Redirects are refused (a redirect counts as a failed post), so the token
    is never forwarded to another host.
- Windows: Claude Code runs status lines through Git Bash if installed, else
  PowerShell. Paths in the command use forward slashes. Provide one
  PowerShell-safe and one POSIX snippet; test both quoting paths the way
  `helper_command` is tested today.
- Tests: synthetic status-line JSON fixtures (fresh, missing fields, partial
  windows, no `rate_limits`), chaining, throttle and backoff, timeout against
  a local test server that never answers, and exit code 0 on every failure.

### Phase 3: HTTP route and admin controls

Done 2026-10-18: implemented with tests; the round trip from the collector script to `/api/v1/status` passes end to end.

- `api.py`: add `POST /api/v1/ingest` behind `ingest_enabled`. Tokens live in
  `ingest_tokens` (schema v7) as SHA-256 hashes; the presented token is hashed
  and looked up, the instance is checked to be enabled and `accepts_push`, then
  `ingest()` runs. Authentication happens before the body is read. Apply the
  size, rate and response rules above. Keep the existing 3 s socket timeout.
- Enabling: a global setting stored in SQLite and toggled in the admin page,
  plus `DASH_INGEST_ENABLED=1` (and `[daemon] ingest_enabled` in TOML) for
  containers. The environment variable overrides the stored setting. Off by
  default. Changing it takes effect without a restart.
- Admin page, per push-capable account: "Create remote token" (shown once,
  with copy buttons for the token and both settings snippets), "Rotate",
  "Revoke", and a read-only row showing collector last seen, host and last
  response seen. Offer the collector file for download from the admin page so
  a remote machine does not need the repository.
- Docs: `docs/claude.md` gets a "Remote computers" section. `docs/setup.md`
  gets the environment variable and the security note: ingest is plain HTTP;
  on an untrusted network put it behind TLS (reverse proxy) or a private
  network such as Tailscale. The body holds no secrets; the token is
  write-only for one account.
- Tests: disabled returns 404; bad token 401; revoked token 401; disabled
  instance rejected; 413 and 429; a full round trip from the Phase 2 script
  to `/api/v1/status` output; the Presto shared token is refused for ingest.

### Phase 4: freshness and "run the CLI" hints (daemon and Presto)

Done 2026-10-18: implemented with tests. `claude_hint()` in `quotas.py`
computes the hint when the payload is built (`present_quota`), using the
source's configured `stale_after_seconds`; `dashboard.py` adds the
`Data age` / `Last reply` / `Reports` trail lines to detail cards for reports
that carry a collector trail; `presto/ui.py` and `presto/home.py` draw the
hint on Home and `--` with empty bars for expired windows. Rule 3 also fires
for a migrated database with quota rows but no `collector_seen_at` when both
resets have passed.

The daemon computes one hint per Claude account when the status payload is
built (so it ages between polls, not once at collect time) and puts it in the
quota report as `hint` (ASCII, at most 28 characters). First match wins:

| Condition | Hint |
| --- | --- |
| No observation, or no collector report and no window ever measured | `Run claude in a terminal` |
| Collector seen, but no window ever measured (heartbeats only) | `Waiting for a Claude reply` |
| Measured windows exist and every one's reset has passed | `Awaiting refresh` |
| Measured windows exist, none fresh, collector seen within the stale threshold | `Reply in claude to refresh` |
| Measured windows exist, none fresh, collector quiet | `No report 6d - run claude` |
| At least one window is fresh and measured | no hint |

- The stale threshold is the source's configured `stale_after_seconds`
  (default 900), not a hardcoded number.
- `<age>` in `No report <age>` is the age of the newest signal of any kind
  (the later of `collector_seen_at` and the newest window `observed_at`),
  formatted `%dm` under 60 minutes, `%dh` under 48 hours, else `%dd`.
- A migrated database can have quota data but no `collector_seen_at` (the
  author's real install). Treat the collector as never seen and use the last
  row with the quota's own age.
- Never mark the provider `offline` for these cases; use `warning` (keep any
  worse status that already applies).
- Presto Home: when a quota row has a hint and no fresh window, draw the hint
  in dim ink in place of the reset note (the hint wins over a long name). A
  window whose reset has passed shows `--` with an empty bar (its old value
  only on the detail card), never a dimmed number and never `0%`.
  Stale-but-unexpired windows keep today's dimmed value. Rows with no hint
  behave exactly as today.
- Detail card, at most 6 lines (drop the lowest-priority line first, never a
  window reset line): the per-window reset lines, then `Data age: <age>`,
  `Last reply: <age> ago` or `Last reply: unavailable` (from
  `last_progress_at`), and `Reports: <host> <age> ago` or `Reports: never`.
  Reset times stay UTC (the device has no time zone). Other providers' cards
  do not change.
- Tests: each hint row above with an injected clock, the migrated no-collector
  case, the age boundaries, the 28-character ASCII cap, expired windows never
  rendering a percentage on Home, and layout checks. Visually check PSL with
  fixture payloads for each hint (never the live preview: it reads
  `presto/config.py`), then sync and verify the device per `AGENTS.md`.

### Phase 5: Claude activity from local session logs (VS Code panel and CLI)

**Why.** The author uses Claude almost only in the VS Code panel, which (per
several public Claude Code issues, and the author's own install receiving no
reading for a week) does not run the status line, the only source of quota. The
goal is to show that **Claude is in use but its quota is stale**, accurately,
without any quota from the panel. Quota percentages from the panel are not
obtainable by any supported means; this phase does not pretend otherwise.

**Verified 2026-10-18 on the author's machine (field names and numbers only):**
the panel writes the same session files as the CLI under
`<profile>/projects/<project>/<session>.jsonl`. Each assistant entry has
`timestamp` (ISO UTC), `entrypoint`, `requestId`, and `message.model` plus
`message.usage` with `input_tokens`, `output_tokens`,
`cache_creation_input_tokens` and `cache_read_input_tokens`. Anthropic's own VS
Code docs say the panel's `/usage` breakdown is computed from these local
sessions.

**Assumptions (decided 2026-10-18; change them here, not in code):**

- A. The reader is a **pull source inside the daemon** (no hook, no settings
  edit, no push), because the daemon runs on the machine where Claude Code
  runs. Remote/Docker use is Phase 5c.
- B. It is **opt-in**: one checkbox on the Claude account, off by default.
- C. It reads **only** `timestamp`, `entrypoint`, `requestId`, `message.id`,
  `message.model` and the four token counts. It never stores, logs, returns or
  sends message text, tool inputs, paths or session ids, even though the files
  contain them.
- D. When the reader is enabled and working, stale quota with recent activity
  **does not raise the status to warning** (no permanently yellow LED). The
  text hint carries the staleness. If the reader is off or failing, Phase 4
  behaviour is unchanged.
- E. The session file format is **not an official interface**. The docs must
  say so; the reader degrades to "activity unavailable" instead of raising when
  the format changes.
- F. Activity is a **lower bound**: use on claude.ai, a phone or another
  machine never appears in these files.
- G. No percentage is ever derived from tokens (limits are not published).
  Tokens are shown as load, not as quota.

**Reader (`claude_activity.py`, stdlib only, daemon-side).**

- Root: the parent directory of the account's `settings_path`, plus `projects`
  (so each Claude profile/account gets its own activity). Never follow a
  symlink out of that tree.
- Pre-filter lines cheaply (only lines containing `"usage"`), then parse; skip
  lines over 1 MiB, malformed lines and a partial last line.
- **Dedupe**: verify on the real file shape (names only) whether one API
  response appears on several lines, and count each response once by
  `requestId`/`message.id`. State the finding and the rule in the module
  docstring.
- **Incremental**: remember per file (size, mtime, offset); on each poll read
  only appended bytes; rescan a file that shrank or was replaced. The first
  scan skips files not modified in the last 8 days and is bounded (cap total
  bytes). Keep only the last 7 days of (timestamp, tokens, model, entrypoint)
  tuples in memory.
- **Output** (`local_activity` in the quota report, only when enabled):
  `last_request_at`, `last_model`, `last_entrypoint` (`VS Code`, `CLI`, or the
  raw value sanitised to ASCII, at most 8 characters), `requests_5h`,
  `tokens_5h`, `tokens_7d`, where tokens are input + output + cache creation
  (document the choice; cache reads are excluded as they are cheap against
  limits), and `available` (false with a short reason on any error).
- Never raises; a missing directory, permission error or unreadable file sets
  `available: false`.
- Config: `activity_logs` boolean on `ClaudeSource` (check how
  `config_fields` types and the admin form handle booleans; add support if
  missing), label "Read local Claude Code activity (timestamps and token
  counts only)". The 5 minute "in use" window is a setting
  (`active_seconds`, default 300).

**Status and hints (computed when the payload is built, like Phase 4).**

- Status: a last request within `active_seconds` gives status `active` (solid
  marker, blue accent) unless something worse already applies. Otherwise the
  source's normal status. Assumption D applies.
- With the reader available, the hint becomes (at most 28 ASCII characters; if
  too long, drop " old"):

| Situation | Hint |
| --- | --- |
| In use, quota readings exist | `In use - quota 6d old` |
| In use, no quota ever | `In use - no quota data` |
| Used recently (under 24 h), quota old | `Used 3h ago - quota 6d old` |
| Used recently, every reset has passed | `Used 3h ago - quota expired` |
| Quota fresh | no hint (the marker shows use) |
| No activity in 24 h or more | Phase 4 hints unchanged |

- Reader off or unavailable: Phase 4 hints and status unchanged.

**Estimated weekly reset (detail card only, labelled as an estimate).** The
weekly cycle is fixed. From the newest real `seven_day` reset time R (even if
passed), the next reset is R plus 7 days, repeated until it is in the future.
Show `Wk reset ~4d (est)`. Do not estimate the 5h window (its start rule is not
documented). Never show the estimate as a measured reset, and show nothing when
no real reading has ever existed.

**Detail card.** At most 6 lines, in this priority (drop from the bottom): the
existing per-window reset lines, `Wk reset ~<age> (est)`,
`Last used: <age> ago (<surface>)` (replaces `Last reply` when activity is
available), `Tokens 5h/7d: 120k/2.1M` (compact `k`/`M`), `Data age`,
`Reports`.

**Presto.** Home needs no layout change: it already draws the hint in the note
slot and a solid marker for `active`. If device code does change, follow
`AGENTS.md` (PSL fixtures, sync, verify). Check every hint row above and the
`active` marker with fixture payloads, never the live preview.

**Tests.** JSONL fixtures in a temp directory with **sentinel prompt text**:
assert the sentinel never appears in the report, stored rows, API payload or
captured logs. Plus: dedupe; partial and malformed lines; oversized line;
missing and unreadable directory; a symlink out of the tree is ignored;
incremental append picks up new lines and reads only the tail (assert the bytes
read); a shrunk file triggers a rescan; 5h and 7d window math and the `active`
threshold with an injected clock; status rule D; every hint row and the
28-character cap; the weekly estimate across several cycles and with no
reading; detail-card order and the 6-line cap; opt-in off means no
`local_activity` and unchanged Phase 4 behaviour; the admin checkbox round
trip. Docs: `docs/claude.md` section "Local activity (VS Code panel and CLI)"
covering what is read, what is not, assumptions E and F, and how to enable.

Done 2026-10-19: implemented with tests. The opt-in boolean setting, the
stdlib-only activity reader, the in-use status and hints, the estimated weekly
reset and the new detail lines are in place; `presto/` needed no change.

Amended 2026-10-19: the reader reads in bounded chunks under a per-refresh
byte budget, probes the tail of the newest files so the last use is right on
the first poll, and reports a `complete` flag; while that flag is false the
detail card shows `Tokens 5h/7d: scanning` instead of lower-bound numbers.

**Phase 5b (optional, only if 5 is unacceptable): `Stop` hook.** Hooks are
documented to run in IDE extensions. A `Stop` hook running the collector in
activity mode sends a heartbeat and sets `last_activity_at`. It needs a
settings edit (appended to any existing `hooks.Stop`, restored on removal) and
a manual check that it fires in the panel before building. It gives less data
than Phase 5.

**Phase 5c (amended 2026-10-20; needed for Docker/remote): push the local
readers.** Status today: the downloadable collector
(`collectors/claude_statusline.py`) carries ONLY the terminal status-line
quota and heartbeats. The activity reader (Phase 5) and the usage cache reader
(Phase 5d) run inside the daemon and read files on the daemon's own machine,
so a daemon on another machine (Docker) gets neither. This phase closes that:
- The collector gains two optional modes that run the existing standalone
  readers (`claude_activity.py`, `claude_usage_cache.py`, both stdlib-only and
  already free of package imports) on the Claude machine and push the result.
  One checkbox on the account (or the "Set up for VS Code" preset) tells the
  user what to enable, and the admin page's snippet generator includes the
  modes.
- Envelope v1 gains optional keys: `activity` (the `local_activity` fields,
  same caps) and `usage_cache` (the extracted windows, `fetched_at`, the
  optional credit, and the reader's availability and reason). Unknown keys are
  still rejected.
- **Timestamp hazard (must be designed, not assumed).** The daemon owns time:
  a pushed window is stamped with the receive time. A usage-cache reading may be
  hours old when pushed, so stamping it "now" would present stale numbers as
  fresh. The envelope therefore carries the reading's own `fetched_at`; the
  daemon accepts it only for the `usage_cache` object, caps it at the receive
  time, rejects values older than 7 days or in the future, and uses it as the
  windows' `observed_at` and for the source label "usage cache" (the existing
  merge then lets the newest reading win).
- Privacy is unchanged: the same field whitelist as the daemon-side readers,
  never message text, account ids or paths beyond the reader's own path hint
  (which stays on the collector; only a short reason code and a message without
  the path is pushed).
- The reader's log messages are written where the reader runs, so on a remote
  machine they appear in the collector's own output; the daemon's Status tab
  shows the pushed availability and reason.
- Verification needs a second machine: until it has been run for real, the
  admin page and docs must say this path is untested on a remote host.

### Phase 5d (proposed 2026-10-20): Claude usage cache reader (panel quota)

**Finding (verified read-only on the author's machine, numbers and key names
only).** Claude Code writes the data behind its `/usage` dialog to
`~/.claude.json` under `cachedUsageUtilization`: `fetchedAtMs`,
`utilization.five_hour` and `.seven_day` (`utilization` percent and
`resets_at`), a `limits[]` list (`kind` session / weekly_all, `percent`,
`resets_at`, `severity`), and extras (an included credit with `limit_dollars`,
`used_dollars` and `resets_at`; a weekly breakdown by surface; spend and extra
usage state). The numbers equalled the author's VS Code `/usage` popup
exactly (12% / 66%, same resets). The cache refreshes at irregular moments,
not on every response: observed fetches on 10-18 20:23 (a terminal session),
10-19 20:10, 10-20 05:29 and 05:40 (the moment `/usage` was opened, which
forces a refresh). Claude Code's own backups of the file
(`~/.claude/backups/.claude.json.backup.*`) preserve the history. The banked
"Full reset" is NOT in the file. This corrects the earlier conclusion that the
VS Code panel cannot supply quota: it cannot via the status line, but its data
is on disk.

**Proposed design (a third quota observation source).**
- A pull reader inside the daemon, opt-in (its own checkbox, off by default,
  separate from the activity reader because it opens a more sensitive file).
  It reads `<profile>/.claude.json` (default `~/.claude.json`; confirm the
  per-profile location for `CLAUDE_CONFIG_DIR`), checks mtime first, parses
  the file in memory, and extracts ONLY `fetchedAtMs`, the `five_hour` and
  `seven_day` utilization and `resets_at`, and optionally the `limits[]`
  kind/percent/resets_at. It never stores, logs or returns any other field
  (account id, email, organisation, projects, tokens). It must not use the
  obfuscated codename keys (they will change).
- Output becomes a normal quota observation (windows five_hour and seven_day)
  through the existing observation path, with `observed_at` = `fetchedAtMs`
  (a provider timestamp on the same machine, capped at now) and a provenance
  label "usage cache" next to the existing "terminal export". The newest
  reading per window wins, whatever its source; each card and admin line says
  which source produced the number it shows.
- Unofficial format (assumption E again): any parse problem degrades to
  "unavailable" and never raises.
- Hints: with a fresh cache reading, the hint disappears; with an old one, the
  hint tells the user that opening `/usage` in the panel refreshes it (the
  documented trigger).
- Detail card, within the six-line cap and the provenance rule: the optional
  included credit line ("Credit: $100 of $100 (exp 11-05)") only if room
  remains after the existing lines.
- Docker/remote: the file lives on the Claude machine, so a containerised
  daemon needs the collector to read and push it. NOT built: see the amended
  Phase 5c (including the timestamp hazard of pushing an old reading).
- Update the report note and docs: quota arrives from terminal sessions and
  from Claude Code's usage cache; the panel refreshes the cache when `/usage`
  is opened or at irregular moments.

Open points for the author: confirm the per-profile path; how often Claude
Code refreshes the cache passively (watch the backups over a few days);
whether the included-credit line is wanted.

Done 2026-10-20: implemented with tests. The opt-in reader (`collectors/claude_usage_cache.py`, stdlib only) opens Claude Code's state file read-only, extracts only the fetch time, the two windows and the credit (found by shape, never by its obfuscated key), remembers a SHA-256 of the account id in memory only, and degrades to "unavailable" with a precise reason instead of raising; the newest reading per window wins over the terminal export, ties go to the terminal export, and every window, card and admin line names its source.

Follow-up done 2026-10-20: the hints of a cache reader name `/usage` instead of a terminal, the log messages quote the real checkbox label, the Edit form has a **Set up for VS Code** button, and the daemon records in memory how often the cache refreshes (one admin line, no new storage).

Follow-up done 2026-10-20: the cache readings have their own stale threshold
(`usage_cache_stale_seconds`, 300 to 604800 seconds, default 7200), because the
export's 900 seconds marked good cache readings stale; the terminal export
keeps its own threshold, and a merged report judges every window by the source
it came from.

### Phase 6 (optional): Codex over push

Write `collectors/codex_appserver.py` that runs on the developer machine,
reads Codex windows through the supported app-server call already used by
`sources/codex.py`, and pushes them with the same envelope. Set
`accepts_push` on `CodexSource`, which then prefers a recent pushed reading
over spawning a local app-server. This is what makes Codex usable with a
containerized daemon without putting a Codex login in the container.

### Phase 7: "Adding a source" guide and template

Not started. An earlier attempt was discarded, so none of these files exist.

- `docs/adding-a-source.md`: what a source is; the `collect()` contract
  (never raise, return a `SourceSnapshot`, `offline` only when unreachable);
  `config_fields` and `secret_field` (vault-encrypted, redacted from errors);
  quota reports via `quota_report`/`quota_window`; status mapping; how cards
  and Home rows are derived (no device change needed for a quota source);
  pull versus push (`accepts_push`, `push_window_ids`, writing a collector);
  the hard rules above, including allowed data sources; tests to copy; and
  the checklist for PSL and device verification.
- `daemon/src/presto_ai_dash/sources/_template.py`: a commented minimal
  pull source with one config field, one secret, one quota window and
  error handling. The leading underscore keeps it out of discovery.
- `daemon/tests/test_template_source.py`: imports the template explicitly,
  registers it in a test-only registry and runs it through
  `collect_safely`, the dashboard payload and `source_rows`, so the template
  cannot rot.
- Update the short "Adding a source" paragraph in `docs/sources.md` to link
  the guide.

### Phase 8 (optional, only if asked): OpenTelemetry activity detail

Adds model, token and request-time detail; still no quota. Keep it inside the
same daemon process and the same ingest toggle; do not add a collector
service.

- Route `POST /v1/logs` on the API port, OTLP **http/json only** (no
  protobuf, no gRPC), authenticated with the same per-account ingest token via
  `OTEL_EXPORTER_OTLP_HEADERS=Authorization=Bearer <token>`.
- Accept only `claude_code.api_request` log records. Keep `event.timestamp`,
  `model` and the four token counts; discard every other attribute and record
  immediately, including `user.email`, account and session identifiers, and
  any prompt fields. Store only the latest values per account; no history.
- Admin shows the environment variables to set (`CLAUDE_CODE_ENABLE_TELEMETRY=1`,
  `OTEL_LOGS_EXPORTER=otlp`, `OTEL_EXPORTER_OTLP_PROTOCOL=http/json`,
  `OTEL_EXPORTER_OTLP_LOGS_ENDPOINT`, `OTEL_METRICS_EXPORTER=none`) for the
  user's `settings.json` `env` block. Repo-level settings files are ignored by
  Claude Code for these variables.
- Tests: field whitelist (assert email and prompt attributes never reach
  storage), wrong event names ignored, size cap, token auth.

### Phase 9: banked reset credits (Codex now, others if they gain it)

**Done 2026-10-20** (parse, card, Home badge, docs; the envelope part is
deferred to Phase 6). Do this after Phase 4. Added 2026-10-18, after Phases
1-3 were already being built. It is deliberately a separate phase and an
additive change: do not rework the envelope or validator from Phases 1-3 in
place.

**Amendments (2026-10-20, decided before the work started).**

- The ingest envelope and its validator are **not** changed now. No push
  source needs `reset_credits` before Phase 6, and the validator rejects
  unknown keys, so widening the protocol now would buy nothing.
- Claude gets **no** `Resets: not reported` line. Its detail card is full and
  the rule is to remove nothing. Only reports that carry `reset_credits`
  (Codex) show reset lines.

Verified facts (Codex CLI 0.162.0-alpha.17.2, schema generated offline with
`codex app-server generate-json-schema`; no login, no requests): the
`account/rateLimits/read` response we already call carries
`rateLimitResetCredits { availableCount, credits[] }`, where each credit has
`expiresAt` (Unix seconds or null), `status` (`available`, `redeeming`,
`redeemed`, `unknown`), `title`, `resetType`. `credits` is null when only the
count is known. It also carries `credits { balance, hasCredits, unlimited }`
and `planType`. All are nullable; confirm against a live response before
relying on them, and treat any absent or malformed field as "not reported".
Claude has no documented source for its banked reset (the figure exists only
on the claude.ai usage page); do not scrape it.

- **Normalization.** `quota_report` gains an optional
  `reset_credits: {"available": <int 0..99 or null>, "expires_at":
  [<ISO UTC, at most 8, earliest first>]}`. `available` null means not
  reported. Count only rows with `status == "available"`; if `credits` is null,
  keep the count and an empty list.
- **Envelope.** Add `reset_credits` to the allowed top-level keys with the same
  shape and caps. The validator currently rejects unknown keys, so this must
  be added there explicitly. Optional `credits_balance` (string, at most 16
  characters) and `credits_unlimited` (bool) may follow the same path; they
  feed one detail line only. **Deferred to Phase 6** by the 2026-10-20
  amendment above: the envelope and the validator are untouched.
- **Codex source.** In `CodexSource.parse` read the fields from the response
  already fetched; no extra call. Never call the consume method.
- **Daemon.** Expose the object in the Codex account's quota report so it
  reaches the status payload for free. Detail card lines, within the existing
  six-line limit (drop the `Period:` style filler first, never a window line):
  `Resets avail: 3`, `Next exp: 10-22 14:18Z (4d)`, and, when present,
  `Credits: 0`. When `available` is null: `Resets: not reported`.
- **Dashboard (Presto Home).** One small badge after the account name, ASCII
  only: `R3` in the dim ink colour when `available > 0`. When the earliest
  expiry is within 72 hours it turns amber and adds the time left: `R3 2d`.
  `R0` is not drawn, and nothing is drawn when `available` is null, so unknown
  is never shown as zero. Draw it only if it fits after the name without
  overlapping the reset note; otherwise skip it (the detail card is the source
  of truth). It is informational: no colour implies spending one.
- **Tests.** Parse with count only, count plus rows, expired or `redeemed`
  rows ignored, null everywhere, malformed values; validator accepts the new
  keys and rejects a bad shape; card lines and the six-line limit; Home badge
  states (hidden when null or 0, dim, amber near expiry, skipped when it does
  not fit). Check in PSL with fixtures for each state, then sync and verify the
  device per `AGENTS.md`.

**What was built (2026-10-20).** `CodexSource.parse` reads
`rateLimitResetCredits` and the bucket's `credits` from the response it
already has: `reset_credits` (the provider's `availableCount` when it is a
real integer in 0..99, plus the expiries of the rows whose status is exactly
`available`, at most eight, earliest first) and `credits` (the balance
normalised with `Decimal`, so `0E-10` becomes `0` and `12.50` becomes `12.5`,
plus `unlimited` only when it is a real boolean). Missing or malformed data
means "not reported", never zero. The Codex detail card adds `Resets avail: N`,
`Next exp: MM-DD HH:MMZ (<age>)` and `Credits: <balance>` when reported, and
ends with `Source: local Codex app-server`; the window lines are never
dropped. The Presto Home row draws the `R3` badge, amber with the time left
within 72 hours, computed against the payload's `updated_at`. The
admin-page provenance work (the "Provenance and no loss of detail" rule)
labels the same numbers on the account row. The envelope and validator are
untouched, as the amendment requires.


## Not covered (needs its own decision)

- **Admin page in Docker.** It binds `127.0.0.1` and checks the `Host`
  header, so it cannot be opened from outside a container. Either add a
  configurable bind address plus an explicit allowed-host list, or document
  `docker exec` and the environment variable for every setting. Docker is not
  usable for this project until that is decided.
  **Decided and built 2026-10-20** (backlog item 6): the admin page has a
  bind address (`admin.host`, `DASH_ADMIN_HOST`, loopback by default) and an
  allowed-host list (`admin.allowed_hosts`, `DASH_ADMIN_ALLOWED_HOSTS`). A
  non-loopback bind needs an explicit admin token and a non-empty list, or
  the daemon refuses to start the admin page. See `docs/docker.md`.
- No Dockerfile exists yet (planned for v0.1).
  **Built 2026-10-20**: `Dockerfile`, `compose.yaml` and `.dockerignore`,
  with the container tests. See `docs/docker.md`.
- TLS termination inside the daemon: out of scope; use a reverse proxy.

## Backlog and priority (set by the author, 2026-10-20)

In order (item 0 is an urgent defect and goes first):

0. **Presto connectivity failure (MAJOR; next; added 2026-10-20).** The physical
   Presto intermittently shows "Daemon offline" while the daemon is healthy and
   the PSL preview is fine. A device restart fixes it only sometimes and it
   fails again within minutes. Evidence so far (read-only measurements on the
   author's machine): 124 device-style requests over 75 s all succeeded (median
   0.10 s, worst 0.19 s); the device's configured address matches this PC; the
   daemon answers on every LAN address; the status payload has grown to 10.3 KB
   (device cap 64 KiB), about half of it data the device never reads, with each
   source's quota report sent twice (under `quota` and `metrics.quota`). In the
   device code, `StatusClient.fetch_async` turns ANY exception during the fetch
   or parse, including `MemoryError`, into `NetworkUnavailable`, which the
   screen shows as "Daemon offline / Check daemon and LAN address"; the offline
   screen only appears after 30 s without a good fetch. The strongest
   hypothesis is device memory (heap fragmentation or exhaustion while reading
   and parsing a payload that has grown) or slow reads on the device Wi-Fi.
   It is a hypothesis: the device does not report why a fetch failed, so none of
   this is confirmed. Work, in order, each step shippable on its own:
   - **A. Slim the status payload (daemon only; can run now).** Send the device
     only what `presto/` reads (about 5 KB measured, half of today's), and send
     each quota report once. The admin page does not use this payload. Proof by
     a differential test: draw Home and every card from the full and the slim
     payload and require identical output. Add a payload size budget test
     (fixture with every source type and both readers on; fail above 8 KB) so it
     cannot quietly grow back, and document the budget.
   - **B. Make the device say why it failed (needs presto/ changes; after the
     Home overflow strip agent finishes, which edits the same files).**
     Distinguish `MemoryError`, timeout, connection reset and incomplete body
     instead of one generic message; show the error type, bytes received and
     free memory on the offline screen detail (ASCII, short); call
     `gc.collect()` before parsing and read the body without repeated
     concatenation (preallocated buffer or fewer, larger chunks); keep a failure
     counter and the last error.
   - **C. Device-to-daemon diagnostics header.** The device sends on every
     request `X-Presto-Client` (its `DEVICE_NAME`), `X-Presto-Revision` and
     `X-Presto-Diag` (for example `err=MemoryError;n=3;free=41200;up=312`, at
     most 120 ASCII characters; untrusted input, sanitised, memory only). The
     daemon logs a change of diagnostics once and the Status tab (admin page
     reorganization) shows each client's last error and free memory. This gives
     visibility without a serial cable.
   - **D. Decision gate for the protocol** (see "Device protocol options"
     below): move to summary plus lazy cards if the failure persists at about 5
     KB, or when more than about 12 services are expected; to fully independent
     per-source fetches only if per-device source selection becomes a
     requirement.
   - **Acceptance.** The device stays connected for 24 hours with the daemon
     running; any failure is visible with its reason on the device and in the
     daemon; the payload for the author's setup is at most 5 KB and a test
     enforces the budget. Record the outcome here.
   **UPDATE 2026-10-20 (later): the failure is the Presto's Wi-Fi link, not the
   payload or the daemon.** After slimming the payload to 4.4 KB (step A, 58%
   smaller, identical screens; shipped to the daemon) the device still dropped
   offline after running for a while. Measurements from the PC while it was
   failing: the Presto's connection attempts reached the daemon but stuck in
   `SYN_RECEIVED` (3 of 3 in 90 s: the daemon answered and the device never
   completed the handshake); the PC's neighbor (ARP) entry for the Presto was
   `Unreachable`/`Incomplete` with no MAC address; pings to the Presto were 3 of
   12 answered (latency to 1.9 s), then 0 of 60, while the gateway answered 6 of
   6 and the daemon answered 124 of 124 local requests. A power cycle brings it
   back for a short time. So the device's network stack stops answering at the
   link layer; "Daemon offline" is the app's generic label for that. A soft
   relaunch of the app (the sync tool) does not reset the Wi-Fi chip, which would
   explain "a restart fixes it only sometimes". The cause on the device is still
   unproven; candidates, most likely first: Wi-Fi power-save (the default mode
   sleeps the radio and can stop answering ARP and incoming packets); weak
   signal or interference at the device's position; DHCP lease renewal failing;
   the radio wedging after errors with the app still believing it is connected
   (`wifi.isconnected()` stays true, so `poll_once` never reconnects).
   Revised steps (A is done): **B2** turn off Wi-Fi power-save after connecting
   (`wlan.config(pm=0xa11140)`, in try/except) and re-apply it after every
   reconnect; **B3** a network watchdog in the app: after N consecutive failed
   fetches cycle the radio (disconnect, deactivate, activate, reconnect), and
   after M minutes of continuous failure reset the device in a way that returns
   to AI Dash (check how the launcher restarts apps first); **B4** report Wi-Fi
   state with the failure on the offline screen and in the diagnostics header
   (isconnected, status code, RSSI, IP, uptime, failure count); **B1** (error
   type, bytes and free memory) stays. PC-side checks to repeat when it fails:
   `Get-NetNeighbor` for the device address, ping loss, `netstat` for
   `SYN_RECEIVED` from the device. Also check the router's client list for the
   Presto's signal strength and the DHCP lease time.
   **Rollback test (2026-10-20): the device app code is cleared.** The app from
   before Phase 9 (revision 6d41106e5122, commit dfa1d05) was synced to the
   Presto and ran against the slimmed payload. It dropped offline again with the
   same signature (ARP `Incomplete`, 0 of 12 pings answered, gateway 4 of 4,
   a `SYN_RECEIVED` connection from the device stuck on the daemon). So neither
   the Phase 5-9 payload growth nor the recent app changes cause it. Since
   Wi-Fi power-save has always been on, the open question is what changed in
   the environment (router, channel, signal, a new device or lease on the
   network, the PC's address or adapter, load on the radio), not the app.
   Step B1/B4 shipped: the offline screen and serial log now show the
   exception type, where the fetch stopped (conn/send/head/body/parse), the
   failure count, uptime, free memory and Wi-Fi state.
   **Decision (2026-10-20): item 0 is closed as a network problem, not an app
   defect.** The router log shows the Presto repeatedly joining and leaving the
   2.4 GHz network (sessions of 3 s to 8 min, almost no traffic, no disconnect
   reason, signal -64 to -65 dBm, 0% client retries), and the same happens on
   the pre-Phase-9 app. It is handled outside this project (router and Wi-Fi
   settings). What stays: the payload slimming (shipped), and the offline screen
   and serial log that show the failure stage, count, uptime, memory and Wi-Fi
   state, so a future failure explains itself. A radio watchdog and Wi-Fi
   power-save off remain optional experiments, not scheduled.
   **Accepted for later: summary plus lazy detail (option 2 below).** Home polls a
   small summary (the Home row data and each card's title, status and
   primary/secondary lines); a card's detail lines are requested only when the
   user opens that card, from a per-source path, so one source's detail can grow
   without enlarging every poll. Measured now: the device payload is about 4.1
   KB, of which card detail lines are roughly 0.9 KB, so the saving today is
   modest; it pays off as sources are added or a source's detail grows (for
   example a longer activity breakdown). Do it when a source needs more detail
   than fits the poll, or beyond about 12 services. Keep `/api/v1/status` working
   for old devices and add a versioned path.
   **Device protocol options** (author's idea to send each source
   independently, assessed 2026-10-20): (1) slim single payload: one request,
   about 5 KB peak, scales to roughly 12 services; (2) summary plus lazy
   cards: Home polls a small summary (the Home row data only), the detail
   lines of a card are fetched only when the user swipes to it; one request per
   poll plus one for the visible card, peak about 3 KB; (3) fully independent
   per-source fetches: best isolation and peak memory (about 1-1.5 KB) but
   N+1 requests per poll (31 with 30 services; at an estimated 0.1-0.3 s per
   Wi-Fi connection that is several seconds per cycle, not measured on the
   device), partial-update rules, and the most device code. Any change keeps
   `/api/v1/status` working for old devices and adds a versioned path.
1. **M12 alert polish.** Needs decisions first (LED colour for "in use" versus
   a warning, default thresholds, whether a stale reading may ever alert,
   sounds and quiet hours). Also in scope (author, 2026-10-20): **an alarm
   pop-to-top**, where Home scrolls to the alarmed service. It needs
   debouncing so it never bounces: act only after the alarm has persisted for
   a set time; at most once per service per cool-down period; never while the
   user is touching the screen or has scrolled recently; return to the user's
   previous position after a timeout; and a new alarm must be worse than the
   one already shown. The Home overflow strip's "worst hidden status" is the
   building block that tells the device which hidden service is alarmed.
   **M12 follow-ons (added 2026-10-20), in this order, each after the one
   before it:**
   - **M12b, per-service settings and a master switch.** One "Alerts on/off"
     master; per service: alerts on/off and optional warning/critical percent
     (blank = use the global value). With the master off every per-service
     control is disabled (not hidden), keeps its stored value, and a banner
     says so; each affected card shows "alerts off (global)". "Off" for a
     service means no chirp, no LED escalation, no pop-to-top and no part in
     the overall accent, but it never hides data: the row, numbers and true
     status colour are unchanged, and the admin card says "alerts off". The
     prompt is in the conversation record; it builds on M12.
     **Done 2026-10-20.** The master switch is the `enabled` key of the
     global `[alerts]` settings (admin-stored > dash.toml > default, with the
     origin in the effective configuration). The per-service settings are NOT
     source config fields: they are their own rows in SQLite keyed by the
     instance id, so a toggle never rebuilds the source and never restarts
     its readers; they survive an edit, a rename and a rebuild, and they are
     deleted with the service. The resolution (master AND service enabled;
     own threshold else inherited; an unusable pair falls back to the global
     pair with one INFO line and "override ignored" in the admin page) is one
     pure function in `alert_policy.py`. Each source in the payload carries
     `"alerts": true|false` and the top-level `alerts` object carries the
     master; the device never receives a threshold. "Alerts off" removes the
     service from the chirp, the LED escalation, the pop-to-top and the
     overall accent, and hides nothing. The admin page has the master at the
     top of the Alerts section, disables every control while it is off
     (keeping the values, with the reason in words), shows the banner
     "Alerts are off for all services" in the Alerts section and on the
     Services tab, has an Alerts group per service with the inherited global
     value as the placeholder, and lists every service with alerts off on the
     Status tab.
   - **M12c, a bell per service on the Presto (author's idea).** The control
     is on each service's card, not on the Home rows (rows are 18 and 46 px
     tall: too small to tap reliably, and a tap on a row already opens its
     card). A large target (about 48 px) with an "ALERTS ON/OFF" pill and a
     bell drawn from shapes (bitmap fonts and ASCII only); with the master
     off it reads "ALERTS OFF (ALL)" and a tap shows a short message saying why it
     cannot be turned on ("Alerts are off for all services; turn them on in the
     admin page"), never a silent no-op. Home shows only a
     small non-interactive "alerts off" mark on a row. This is the first write
     from the device: a new authenticated endpoint
     (`POST /api/v1/alerts/<source id>` with `{"enabled": true|false}`, the
     shared token), which sets a state and never toggles (a double tap or a
     retry is harmless), changes only that one setting of an existing service,
     is rate-limited and logged at INFO, and is not a provider action. The
     daemon stays the source of truth: the device shows the new state at once,
     holds it until the next poll confirms it, and reverts with a short message
     if the write fails. The shared token travels over plain HTTP on the LAN, so
     the worst a LAN eavesdropper could do is flip one service's alerts; say so
     in the docs. The read-only rule in `AGENTS.md` is about provider accounts
     (author, 2026-10-20: for example resetting a weekly quota); the Presto
     changing a setting on its own daemon is not a provider action, and
     `AGENTS.md` now says so.
     **Done 2026-10-20.** The route is `POST /api/v1/alerts/<source id>` on
     the device port with the shared bearer token (constant-time compare): the
     body is exactly `{"enabled": true|false}`, at most 256 bytes, JSON, no
     chunked body; it sets a state and never toggles; it changes only that
     one value and goes through the same code as the admin page's per-service
     control, so both surfaces agree and the last write wins; it has its own
     rate limit (30 a minute per address) and logs one INFO line with the
     sanitised client name and never the token. The device writes it with a
     bounded async `post_alert` in `presto/net.py` (no redirect, a capped
     answer, the same exceptions as a fetch) and runs it as a task, so a
     touch never blocks. The bell is drawn in the top-right of a service's
     card from rectangles: the status colour when on, a grey bell with a
     slash when off, a grey crossed bell when the master is off, with the
     label "ALERTS ON" / "ALERTS OFF" / "ALERTS OFF (ALL)" next to it and the
     title cut so it never reaches it. A tap on the bell applies the new
     state at once and holds it until a payload confirms it or 20 seconds
     pass; a failed write reverts and says "COULD NOT SAVE - DAEMON OFFLINE"
     for four seconds, and a tap with the master off says "ALERTS OFF FOR ALL
     - SEE ADMIN". A tap elsewhere on the card still opens its detail, and a
     swipe over the bell is never a tap. Home shows a small dim bell next to
     the name of a service whose alerts are off, and the row is unchanged
     otherwise. The device observes only the services whose `alerts` is true:
     a missing or junk value counts as true, so an older daemon behaves as
     before.
   - **Mute that expires.** Long-press mute is a toggle that can be forgotten.
     Add an expiry (default 4 hours, settable in the Alerts section, 0 = never)
     and an on-screen indicator while muted, so "I did not realise it was off"
     cannot last. The expiry is the daemon's or the device's, decided when it
     is built (the device has no wall clock, so a duration from the moment of
     muting is simplest).
     **Done 2026-10-21.** The device counts the mute: the daemon sends
     `mute_hours` (0..48, default 4, admin-stored > dash.toml > default) and
     the device measures it with its own tick counter, wrap-safe, so the
     expiry is the device's. A long press starts the mute, a long press while
     muted unmutes at once, the expiry lifts it silently (the statuses were
     tracked while it was on, so nothing chirps for an alarm that began
     during the mute), a restart clears it, and changing `mute_hours` while a
     mute runs applies to the next mute. The indicator is a small chip in the
     bottom-right corner of Home and of every card - `MUTED 3H`, `MUTED 45M`,
     or `MUTED` alone when the mute has no end - in a fixed slot that never
     covers the title, the bell, the status chips, the STALE line or the
     dots. The dimmed accent stays.
   - **Only if needed: per-window-kind thresholds.** One pair per service
     applies to all of its windows. If a short window (Claude's 5 hours) at 80%
     is routine and keeps alerting, add a separate pair for short and long
     windows rather than muting the service.
   **Done 2026-10-20 (the base M12; the follow-ons above are still open).**
   (M12b, M12c and the mute that expires are done; only the optional
   per-window-kind thresholds are still open.)
   The decisions are as taken: warning (status `near_limit`, yellow) at 80%,
   critical (status `critical`, red) at 95%, `rate_limited` at 100% as
   before, both thresholds settable in 1..100 with the warning below the
   critical one, and a source's status is its worst window's. A window that
   resets within 30 minutes does not alert, and only a fresh, available
   window alerts at all: a stale, unavailable or already-reset window keeps
   today's stale presentation and never raises an alert. Claude's usage
   cache readings keep `warn_on_stale` false.

   The mapping lives in the daemon (`alert_policy.py`, applied in
   `dashboard.build_instance_payload`), because the daemon decides the
   user-facing status and the device stays simple. The LED shows the worst
   service (red > yellow > blue > green), and "in use" is never shown over a
   warning. The sound is tracked per service: one chirp when a service
   becomes worse, two for critical, rate limited or offline, never repeated
   while it lasts, silent on recovery, and a silent baseline for the first
   reading after boot.

   Quiet hours are off by default. The daemon judges them with its own local
   time and sends the flag in the payload, so the device needs no clock or
   time zone; while it is true the device is silent and its light is dimmed.
   The long-press mute is unchanged.

   The pop-to-top is in `presto/popup.py`, a pure module with an injected
   clock: the alarm must hold for 30 s, a service moves the list at most
   once in ten minutes, the list never moves within 20 s of a touch or a
   scroll, a new alarm only acts when it is worse than the worst one
   already shown, and the user's own position comes back 60 s later unless
   they have scrolled since. It runs on Home only, never on a card or a
   detail view.

   The settings live in the admin page's new **Alerts** section ("Applies to
   all services"): warning percent, critical percent, sound on/off, quiet
   hours (on/off, start, end) and pop-to-top on/off. Storage and precedence
   follow `overflow_rows`: the admin-stored value beats `dash.toml`
   `[alerts]`, which beats the default; there is no environment variable.
   The payload gained `"alerts": {"quiet": bool, "sound": bool,
   "pop_to_top": bool, "mute_hours": int}`, and the device validates every key
   and falls back to sound off, quiet off and pop-to-top on (the sound is off
   until the user turns it on: a desk device must not beep unannounced, and
   the mute length falls back to four hours).
1a. **Home overflow strip (decided 2026-10-20, runs before Phase 5d).** The
   Home list shows full rows in the saved order until the space runs out. The
   rows that are not fully visible appear as names, each with a status marker,
   in a strip at the bottom; `+N` takes the worst hidden status colour. As the
   user scrolls, rows move between the strip and the list. The strip setting
   in the admin page is the MAXIMUM number of lines, 0 to 3, default 1 (0
   turns it off): the strip uses only as many lines as the hidden names need,
   up to that maximum, and shows `+N` only when the maximum is not enough. The
   line count is decided from the layout (rows, names, setting), never from
   the scroll position, so the list does not jump while scrolling; each
   extra line costs 18 px of list height. The strip disappears entirely when
   the whole list fits.
   **Done 2026-10-20**: the strip, the `overflow_rows` setting (environment,
   stored value, `dash.toml`, default 1) and the payload key are in place.
1b. **Phase 5d**, the Claude usage cache reader (high value: quota for VS Code
   panel users). Runs after 1a; its prompt is written.
1c. **Admin page reorganization (added 2026-10-20; before M12, which adds
   alert settings that need a home).** The page mixes per-service status,
   per-service actions, global settings and documentation text on one long
   page, so it is hard to tell what applies to everything and what applies to
   one service. Decisions so far (author, 2026-10-20): collapsed account cards
   that still show at a glance that an account is working; a Status tab; no
   single huge scrolling page; global settings grouped in sections. **Three
   tabs, not four** (the author asked for a Status tab; Help is merged into
   Settings to avoid tab overkill; confirm before an agent prompt is written):
   - **Services** (configure and arrange). The account list with order
     arrows, Enabled, Edit and Add source. Each account is a card that is
     collapsed by default and shows, on one line: a status dot, the name, its
     key numbers (for example `5h 6% / Wk 59%`, `in use 3m`, `12 req`), and
     small chips for freshness (`fresh 3m`, `stale 2d`, `no data`) and for
     where the numbers come from (`terminal export`, `usage cache`, `local
     logs`). Expanding a card shows everything the page shows today (window
     bars, the labelled Quota / Activity / Usage cache / Status note lines)
     and that account's own actions (Claude export setup and removal, remote
     token create, rotate and revoke, Codex sign-in and sign-out). Per-account
     settings stay in that account's Edit form. A scope badge ties anything on
     this tab to one account.
   - **Status** (is everything working; read-only). Clients: every device that
     fetches `/api/v1/status` (one or several Prestos, and the PSL preview),
     each with its name, its address, when it last fetched, how often, and the
     app revision it reports; a client not seen for three poll intervals is
     shown as not connected. See "Clients" below. The daemon: version, database schema
     version, uptime, poll interval. A health row per account: last collection
     time, status, and the last problem in plain words. The readers and
     collectors: the local activity reader (ok, scanning or the reason it is
     unavailable), the usage cache reader (fetched time or the reason), and
     remote collectors (last seen per account and host). A **Problems** list
     of recent WARNING and ERROR messages from the daemon (an in-memory ring
     buffer of the last 50, de-duplicated, never containing file contents or
     secrets), which is where the clear "format changed" and "access denied"
     messages become visible without reading a log file.
   - **Settings** (global, in collapsible sections; the first open, the rest
     collapsed; each heading says "Applies to all services"). Home display
     (the overflow strip maximum). Remote reports (the toggle, the collector
     download, the plain-HTTP warning). Alerts (added by M12: thresholds,
     sounds, quiet hours, mute). Effective daemon configuration, read-only,
     showing each value and where it came from (environment, `dash.toml`,
     database), for poll interval, retention, ports and the timezone the
     process uses; editing these stays in `dash.toml` or the environment
     because they need a restart. About and help: the explanatory text now at
     the bottom of the page (cloud and subscription sources, the Fake demo
     source, the order note), where the logs and the diagnostics commands are.
   - Rules: nothing is removed (the "no loss of detail" rule); no change to the
     existing admin API behaviour, CSRF or sessions (new read-only endpoints are
     added for Status: the last-poll time, the reader states and the problems
     buffer); the active tab is in the URL hash (`#services`, `#status`,
     `#settings`) and remembered in `localStorage` with a try/catch fallback;
     the ARIA tab pattern, keyboard operable; plain HTML and JS with no
     dependencies; all report-derived text uses `textContent`; the page works at
     phone width; verified in a real browser against a throwaway admin server on
     a temporary database.
   - Decided (author, 2026-10-20): the Problems buffer is memory only and is
     lost on restart; the Status tab refreshes itself every 10 seconds while
     it is visible and the browser tab is in the foreground. Still to
     confirm: three tabs rather than four.
   - **Done 2026-10-20**: three tabs (Services, Status, Settings), the ARIA
     tab pattern with the keyboard, the active tab in the URL hash and in
     `localStorage` (in a try/catch), collapsed account cards that show a
     status dot, the name, the key numbers, a freshness chip and a source
     chip, and everything the page showed before inside the opened card.
     The Settings tab has Home display, Remote reports, the read-only
     effective configuration with the origin of every value, and the About
     and help text. The Status tab shows the daemon, the clients, the
     account health, the readers and collectors, and the Problems list; it
     refreshes every 10 seconds while it is visible. Two new read-only
     endpoints, `GET /api/admin/status` and `GET /api/admin/config`; the
     existing admin API, CSRF and sessions are unchanged. The daemon now
     remembers one record per (name, address) for the clients that fetch
     `/api/v1/status`, in memory only, at most 16, with hostile header
     values replaced by "unknown"; the device sends `X-Presto-Client` and
     `X-Presto-Revision` on every request, and the PSL preview sends "psl".
     Verified in a real browser against a throwaway admin server on a
     temporary database, at phone width and with the keyboard. Codex
     sign-in and sign-out are not on the page today and are not part of this
     change.
   - **Clients (several Prestos and the PSL).** The daemon has one shared
     token, so it cannot tell clients apart by authentication, and the
     admin page must not pretend it can. Design: each client declares a
     display name in a request header, `X-Presto-Client` (device setting
     `DEVICE_NAME` in `presto/config.py`, default `presto`; the PSL preview
     sends `psl`), plus an optional `X-Presto-Revision` (its app revision).
     The daemon keeps, in memory only, one record per (name, address) with
     first seen, last seen, a fetch count and the reported revision, at most
     16 records (the least recently seen is dropped). Header values are
     untrusted: ASCII printable, at most 32 characters, anything else is
     replaced with `unknown`, and the page uses `textContent`. The address is
     best effort: behind Docker Desktop, a reverse proxy or NAT every client
     may share one address, so the name is the primary key and two clients
     that use the same name and address are indistinguishable (the page says
     so). Nothing about clients is stored or sent anywhere else.
   - **Not now: per-device tokens and IDs.** A token per device (created and
     revoked in the admin page, like the ingest tokens) would give real
     identity, revocation of one lost device, and per-device settings (which
     accounts to show, its own order and strip size). That is the unscheduled
     "Multiple Presto devices" item in `docs/future.md`; it costs a setup
     step per device and a token in each device's `secrets.py`. Build it when
     per-device settings are wanted, reusing the ingest-token storage and
     admin patterns. The client header above is forward compatible with it.
1d. **Cards v2 and Home bar legibility (added 2026-10-20; after M12c, because
   it edits the same presto/ files and the bell lives on the card).** Review
   of the cards (author and assistant, 2026-10-20): below the headline number
   a card is a column of same-size grey capitals with no hierarchy; it does not
   use Home's window colours or bars; "(stale)" is repeated on every line;
   resets are absolute UTC times; "unavailable" lines say nothing; one line is
   clipped (OpenCode Go weekly/monthly); values are raw ("$0.5000",
   "Database: true"); everything stops about 70% of the way across and the
   local-service cards are 40% empty. Work, cheapest first, each shippable:
   - **Home bars (do first; small).** Measured: the bar area is 11 px split
     over the windows, so Claude's two bars are 5 px and OpenCode Go's three are
     3 px; the unfilled track (28,31,38) is 1.13:1 against the background, nearly
     invisible; stale windows all turn the same grey, so the three Go bars cannot
     be told apart; a 3% value on a 3 px bar is about 6 px. Fix: track about
     (56,60,72) (about 1.7:1) with an optional end cap; bar area 14 px (four-px
     bars with one-px gaps for three windows, five or six for two); a stale
     window keeps its own hue at about 45% brightness instead of grey; any
     measured value above 0 fills at least 3 px; 0% shows no fill. Check the
     row spacing in the preview (2 px between rows). Tests: bar heights per
     window count, track contrast above 1.5:1, stale colour is a dimmed hue,
     minimum fill, no overlap between rows.
   - **Tier 1, no data change.** Dim labels and bright values; a larger font for
     values; "stale" once, as a chip beside the status; hide "unavailable" lines
     or fold them into one dim "no secondary window"; "$0.50" and "DB ok";
     relative resets ("resets in 1d12h") with the absolute time dim.
   - **Tier 2, reuse Home's quota rows** at the top of a quota card: each window
     with its colour, big numerals, bar, reset countdown, and (after M12) tick
     marks at the warning and critical thresholds. The top right of the card
     holds the status chip and, after M12c, the alarm bell.
   - **Tier 3, tiles.** A short list of `{label, value, tone}` tiles per card, built
     by the daemon (at most six; label up to 8 characters, value up to 10), drawn
     as a two-column grid, for example Claude: last used, tokens 5h and 7d,
     credit; LiteLLM: requests, tokens, spend, errors, top model; Ollama: model,
     speed, models. Tiles add about 1.5 KB over seven cards, past the 5 KB
     payload budget, so this tier needs the lazy card detail request (see the
     accepted decision above): a card's tiles are fetched when the card is opened.
     The trigger for lazy detail ("a source needs more room") is met here.
   - Rules: bitmap fonts and ASCII only; no loss of detail (everything now on a
     card stays reachable); provenance lines stay; draw tests check fit and
     overlap; PSL look at every card with fixture payloads (not live); device sync.
    - **Done 2026-10-20.** The Home bars are fourteen pixels high (four-pixel
      bars with one-pixel gaps for three windows, six for one or two), the
      track is (56,60,72), a stale window keeps its own hue at 45% brightness,
      any measured value above zero fills at least three pixels, and an expired
      window stays empty. The card bell is twenty pixels wide with the same
      48-pixel tap area, and the Home "alerts off" mark is a small crossed
      bell. A card now has a hierarchy: dim labels, bright values, staleness
      said once as a chip beside the status, the unavailable lines folded into
      one dim line, money with two decimals, booleans as words, and the reset
      as "resets in 1d12h" with the absolute time dim after it. A quota card
      draws its windows like Home draws them, with the warning and critical
      percentages as tick marks (the daemon sends them as `sources[].marks`;
      the device draws them and decides nothing). The daemon builds the tiles
      (`card_tiles.py`), and a card's tiles and lines are fetched when the card
      is opened: `GET /api/v1/cards/<id>` with the shared token, and
      `GET /api/v1/status?cards=lazy` for the small summary. The old status
      response is unchanged for a client that does not ask for lazy cards.
2. **Small changes (all):**
   - **Home follows the saved account order.** The swipe cards already do.
     Home currently groups cloud rows first and puts local services in a
     bottom strip, which overrides the order. Fix: rows in the saved order;
     a local service is a compact one-line row at its own position (marker,
     name, status or usage), with scrolling that handles mixed row heights.
     Runs after Phase 9 because both change `presto/home.py` and `ui.py`.
     **Done 2026-10-20**: Home rows follow the saved order, the bottom strip
     is gone, a local service among quota accounts is a compact one-line row,
     an all-local list keeps its two-line rows, and the list scrolls by its
     total height.
   - Lock the ingest rate limiter (two lines).
     **Done 2026-10-20**: `check()` runs under a lock, with a stress test.
   - Make `handler_class` keyword-only in `ApiServer`.
     **Done 2026-10-20**: every argument after `status_provider` is
     keyword-only, in `ApiServer.__init__` and in `make_handler`.
   - Decide the weekly-reset estimate across the Nov 1 clock change (ignore
     it, or add a timezone setting; a container defaults to UTC).
     **Done 2026-10-20**: the estimate is not changed; `docs/claude.md` now
     says what it assumes and when it must be checked against a real reading.
   - Re-check the one intermittent `test_management` failure seen once.
     **Done 2026-10-20, not reproduced**: 30 runs of `test_management` and 5
     full-suite runs (612 tests each) all passed with no ERROR or FAIL, so
     nothing was changed. The suspects stay open: the admin handler answers a
     rejected CSRF/Origin request before reading its body, and closing a
     socket with an unread body is what makes Windows reset the connection
     (the same quirk the 413 path already works around). That is a
     hypothesis, not a confirmed cause.
3. **Phase 7, the documentation site (expanded 2026-10-20). 7a and 7b done
   2026-10-20 (site, user docs, developer docs, source template); 7c not
   started.** User and developer
   documentation as a website on GitHub Pages, built from Markdown, with the
   adding-a-source guide and template as part of it, and help links from the
   admin page into it. It copies the stack the author already runs for
   M3Undle (`D:\github\IPTV\M3Undle`: `mkdocs.yml`, `docs/requirements.txt`,
   `.github/workflows/docs.yml`): MkDocs with the Material theme, navigation
   tabs, search, light and dark palettes, `docs_dir: docs/user`, a strict build
   so a broken internal link fails, a Markdown link check (lychee) for external
   links, a PR preview artifact, and deploy through `actions/deploy-pages` on a
   push to main. One-time setting for the author: repository Settings > Pages >
   Source = GitHub Actions. Site URL `https://jake1164.github.io/presto-ai-dash/`.
   - **Published versus internal.** Only `docs/user/` is published. The
     working notes (this plan, the readiness files, milestones, the project
     plan, agent prompts) stay in the repository and are never in the site.
     Examples use placeholder addresses (`192.168.1.50`), never real ones; two
     existing files contain a real LAN address (`milestones.md`, `setup.md`).
     User-facing content that exists today (setup, configuration, sources,
     claude, codex, quotas, limitations, subscription monitoring) moves into
     `docs/user/` so there is one source of truth, not two copies that
     drift; each move updates every reference to the old path (README,
     AGENTS.md, code comments, any test that reads a doc) in the same change.
   - **7a, the site and the user documentation.** Navigation: Getting started
     (what it does, install the daemon, set up the Presto, add the first
     source), Concepts (sources, quotas, where every number comes from, stale
     readings, remote reports), Guides (one per source: Claude including the
     status-line export, the activity logs and the usage cache; Codex; OpenCode
     Go; Ollama Cloud; Ollama; LiteLLM; Home display; remote collectors; alerts
     after M12; Docker after the Docker work), Troubleshooting ("Daemon offline"
     and Wi-Fi drops with the router-side checks learned on 2026-10-20, no
     Claude data, the VS Code panel, stale readings), Reference (configuration
     keys and environment variables, ports and endpoints, the status payload),
     Privacy ("what it reads, what it never does, what leaves the machine":
     the read-only rule in plain words). Acceptance: the strict build and link
     check pass in CI; the site is live; every admin setting is documented.
   - **7b, developer documentation and the adding-a-source guide.** A
     Developers tab: architecture, running the tests, the preview and the
     device sync, how a source is registered (`config_fields`, `collect()`,
     provenance labels, the read-only rule, the device payload whitelist and
     its guard test), and a worked example. A source template (a commented
     source class and its test file, kept working by a test that
     instantiates it) so a new source starts from running code. The guide's
     example must be run, not just written.
   - **7c, in-app help links (after the admin page reorganization lands).** A
     small "?" next to each admin section and setting links to its page and
     anchor on the site, opening in a new tab. One base URL constant. The
     existing in-page explanations stay (no loss of detail; the page must
     still help a LAN-only user). Nothing identifying goes into a link: no
     install id, names, hosts, tokens or accounts. A test fails if an admin
     help link points to a page or anchor that does not exist in
     `docs/user/`. The Settings tab says that Help opens the public docs site.
     **Done 2026-10-21.** One base URL constant and one map (help key ->
     page and anchor) in `web/admin.js`; every link is a plain anchor with
     `target="_blank"`, `rel="noopener noreferrer"`, `referrerpolicy=
     "no-referrer"`, an `aria-label` like "Help: Alerts", a visible focus
     ring and a 24-pixel target. The links cover the three tabs, the
     Services tab and its account cards, the per-source setup (the Claude
     export, the remote token and the guide of every source type in the Add
     source form), the Status tab and every Settings section, including the
     sound and the mute. The URL carries nothing identifying: no query
     string, no install id, no host, no token, no account. A test
     (`daemon/tests/test_admin_help_links.py`) reads the map out of
     `admin.js` and checks every page and anchor with a pure-Python slug
     function; `tools/check_doc_anchors.py` checks the same anchors in the
     built site. The page was checked in a real browser against a throwaway
     admin server on a temporary database: every link opens the right target,
     the keyboard reaches them, there is no horizontal overflow at 390
     pixels, and the sound and the mute length behave. The in-page
     explanations all stay.
   - **Parked (author, 2026-10-20): usage tracking.** Not part of this phase.
     Recorded for later: any analytics goes only on the website, never in the
     app or daemon, and a link marker such as `?app=true` (or `?ref=admin`)
     can say "clicked from the app" without sending any data from the install.
     Choices when it is picked up: Google Analytics (M3Undle already does this
     through `extra.analytics`; it needs a consent banner for EU visitors,
     which Material supports, and ad blockers will hide many self-hosters),
     or a cookieless counter such as GoatCounter or Plausible (no banner).
     Passive adoption signals that need nothing on the site: GitHub traffic,
     clones and referrers, release download counts, container image pulls,
     stars and forks.
4. **Phase 8**, OpenTelemetry, as extra detail (cost, duration, errors). First
   verify with a throwaway local listener that the VS Code panel emits it.
5. **M8 acceptance**: sign-in, cancel and sign-out accepted live; expired-login
   recovery; second account or profile if one exists (else mark untested);
   other buckets via fixtures; a contract test that regenerates Codex's
   schema and checks every field read still exists.
6. **Docker**, everything: admin bind address and allowed hosts, Dockerfile,
   compose file and container test, encryption key by environment variable
   (the Windows protected key file cannot be used in a container), Phases 5c
   and 6, TLS or reverse-proxy guidance, a real collector on a second machine.
   **Partly done 2026-10-20**: the admin bind address and the allowed-hosts
   list (`admin.host`, `admin.allowed_hosts`, `DASH_ADMIN_HOST`,
   `DASH_ADMIN_ALLOWED_HOSTS`; a non-loopback bind needs an explicit admin
   token *and* a non-empty list, or the daemon refuses to start the admin
   page and says why), the `Dockerfile` (pinned slim base, non-root user, a
   `/data` volume, no secrets in the image), `compose.yaml` (device port on
   the LAN, admin port on 127.0.0.1, named volume, restart policy, `TZ`),
   `.dockerignore` with a test that fails if a private name is missing, the
   unauthenticated `GET /healthz` (it answers `{"ok": true}` and nothing
   else), the time zone (`TZ` and the effective-configuration row), and the
   documentation in `docs/docker.md`. The image was built and run: 94 MB,
   non-root, the data in a volume, the health check and the token routes
   verified. **Still open**: Phases 5c and 6 (the activity and usage-cache
   readers over push, and Codex over push), and a real collector on a
   second machine.
7. **M13 demo release**, last.

## Known leftovers (from reviews of Phases 1-3)

- The ingest rate limiter (`api.py`) had no lock while the server is
  multi-threaded. **Fixed 2026-10-20**: `check()` now runs under a lock,
  and a stress test covers the map bound and the one-accept-per-interval
  rule.
- `ApiServer.__init__` had gained arguments before `handler_class`, so a
  caller that passed `handler_class` positionally would have broken.
  **Fixed 2026-10-20**: every argument after `status_provider` is
  keyword-only, in `ApiServer.__init__` and in `make_handler`.
- Never exercised: a real collector on a second computer, the Windows
  PowerShell snippet from a real remote Claude session, and the POSIX
  collector paths (state directory permissions, `O_NOFOLLOW`); the tests for
  those are skipped or simulated on Windows.
- The VS Code panel almost certainly does not run the Claude status line, so
  panel users get no quota from it (Phase 5 adds activity, not quota).
- The admin page cannot be reached inside a container (see above).
  **Fixed 2026-10-20**: it takes a bind address and an allowed-host list
  now, so a container can publish it on 127.0.0.1 of the host.
- Ollama Cloud reports activity totals only, no allowance; nothing to fix
  with its current API.

## Done means

- Default install: no new listener, local Claude export behaves as before.
- With ingest enabled, a remote collector updates Home within one poll, a
  heartbeat-only collector shows the right hint, and a stopped collector ages
  into `No report Nd` without ever showing a fresh 0%.
- All tests pass; PSL checked for each hint state; device synced and verified
  (`tools/presto_device.py sync` and `check` on the current port).
