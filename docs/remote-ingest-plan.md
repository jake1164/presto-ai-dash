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
| 7 | "Adding a source" guide and template | Not started (an earlier attempt was discarded) |
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

**Phase 5c (optional, Docker/remote): push the activity.** Add an optional
`activity` object to envelope v1 (same fields as `local_activity`, same caps,
unknown keys still rejected) and a collector mode that runs the reader on the
Claude machine and pushes it, so a containerised daemon gets activity too.

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
  daemon needs the collector to read and push it (extends Phase 5c).
- Update the report note and docs: quota arrives from terminal sessions and
  from Claude Code's usage cache; the panel refreshes the cache when `/usage`
  is opened or at irregular moments.

Open points for the author: confirm the per-profile path; how often Claude
Code refreshes the cache passively (watch the backups over a few days);
whether the included-credit line is wanted.

Done 2026-10-20: implemented with tests. The opt-in reader (`collectors/claude_usage_cache.py`, stdlib only) opens Claude Code's state file read-only, extracts only the fetch time, the two windows and the credit (found by shape, never by its obfuscated key), remembers a SHA-256 of the account id in memory only, and degrades to "unavailable" with a precise reason instead of raising; the newest reading per window wins over the terminal export, ties go to the terminal export, and every window, card and admin line names its source.

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
- No Dockerfile exists yet (planned for v0.1).
- TLS termination inside the daemon: out of scope; use a reverse proxy.

## Backlog and priority (set by the author, 2026-10-20)

In order, after Phase 9 and the provenance fixes land:

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
3. **Phase 7**, the source guide and template (pull sources now; add a
   "Running in Docker" section after the Docker work).
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
- Ollama Cloud reports activity totals only, no allowance; nothing to fix
  with its current API.

## Done means

- Default install: no new listener, local Claude export behaves as before.
- With ingest enabled, a remote collector updates Home within one poll, a
  heartbeat-only collector shows the right hint, and a stopped collector ages
  into `No report Nd` without ever showing a fresh 0%.
- All tests pass; PSL checked for each hint state; device synced and verified
  (`tools/presto_device.py sync` and `check` on the current port).
