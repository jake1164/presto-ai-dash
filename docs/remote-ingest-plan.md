# Plan: optional observation ingest (local and remote collectors)

Status (2026-10-21): **Phases 1-5, 5d, 7 and 9 are done**, and so are the
backlog items that grew around them (M12 with M12b, M12c and the mute that
expires; the Home overflow strip; the admin page reorganization; cards v2
with lazy card detail; Docker; the small changes). The database schema is v7.
Open: Phase 5c, Phase 6, Phase 8 (researched, decision gate pending), device
diagnostics, several Presto devices, M8 acceptance and the M13 release.

The detailed record of everything finished (designs, amendments, what was
built, measurements and the closed connectivity investigation) moved
verbatim to [history/remote-ingest-done.md](history/remote-ingest-done.md)
on 2026-10-21. That file also holds the original Goal, the facts the plan
relied on, the collection methods table and the full Envelope v1
specification. The user-facing description of the protocol is on the
documentation site:
[Writing a collector](https://jake1164.github.io/presto-ai-dash/developers/writing-a-collector/).

Written for an implementing agent. Read `AGENTS.md`, `docs/claude.md`,
`docs/sources.md` and `docs/quotas.md` first. Do not start a phase until the
previous one passes its tests.

| Phase | What | State |
| --- | --- | --- |
| 1 | Observations storage (schema v6) and `ingest()` | Done (2026-10-18) |
| 2 | Standalone collector `collectors/claude_statusline.py` | Done (2026-10-18) |
| 3 | `POST /api/v1/ingest`, tokens (schema v7), admin controls | Done (2026-10-18) |
| 4 | Freshness hints and the Presto display | Done (2026-10-18) |
| 5 | Claude activity from local session logs (VS Code panel and CLI) | Done (2026-10-19) |
| 5b | `Stop` hook | Optional, only if 5 is unacceptable; not started |
| 5c | Push the local readers (activity, usage cache) for Docker and remote daemons | Open; needed for a containerised daemon |
| 5d | Claude usage cache reader: quota for VS Code panel users from `~/.claude.json` | Done (2026-10-20) |
| 6 | Codex over push | Open (optional; needed for Codex with a containerised daemon) |
| 7 | Documentation site on GitHub Pages (7a site and user docs, 7b developer docs and the source template, 7c in-app help links) | Done (7a and 7b 2026-10-20, 7c 2026-10-21) |
| 8 | OpenTelemetry activity detail | Closed 2026-10-21: the VS Code panel does not emit (probe result in the backlog item) |
| 9 | Banked reset credits (Codex) | Done (2026-10-20); the envelope part is deferred to Phase 6 |

## Hard rules

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

## Decisions

### Accepted: the ingest protocol (2026-10-18)

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

Also accepted: TLS termination inside the daemon is out of scope; use a
reverse proxy or a private network.

### Accepted and built

- **Summary plus lazy card detail** (accepted 2026-10-20 as "option 2" of the
  device protocol options, built with cards v2 on 2026-10-20). Home polls
  `GET /api/v1/status?cards=lazy`; a card's tiles and lines come from
  `GET /api/v1/cards/<id>` when the card is opened. The old status response
  is unchanged for a client that does not ask for lazy cards.
- **Presto connectivity failure closed as a network problem** (2026-10-20).
  The router log showed the Presto joining and leaving the 2.4 GHz network,
  and the pre-Phase-9 app did the same. Kept: the slim device payload and the
  offline screen and serial log that show the failure stage, count, uptime,
  memory and Wi-Fi state. The full investigation is in the archive.
- **The Presto may change one setting on its own daemon** (M12c,
  2026-10-20): a service's alerts on/off through `POST /api/v1/alerts/<id>`.
  It is not a provider action; `AGENTS.md` says so.

### Open

- **Phase 8 gate**: build an OpenTelemetry receiver only if the probe shows
  that the VS Code panel emits `claude_code.*` telemetry (see Phase 8 below).
- **Phase 5c timestamp rule**: how a pushed usage-cache reading keeps its own
  `fetched_at` (see Phase 5c below). It must be designed, not assumed.

### Parked

- **Fully independent per-source fetches** (option 3 of the device protocol
  options, 2026-10-20): best isolation and peak memory but N+1 requests per
  poll. Only if per-device source selection becomes a requirement.
- **Per-window-kind alert thresholds** (from M12, 2026-10-20):

   - **Only if needed: per-window-kind thresholds.** One pair per service
     applies to all of its windows. If a short window (Claude's 5 hours) at 80%
     is routine and keeps alerting, add a separate pair for short and long
     windows rather than muting the service.

- **Wi-Fi experiments** (from the closed connectivity item, 2026-10-20): a
  radio watchdog (after N failed fetches cycle the radio; after M minutes
  reset the device back into AI Dash) and Wi-Fi power-save off after every
  connect (`wlan.config(pm=0xa11140)`). Optional experiments, not scheduled.
- **Usage tracking on the documentation site** (from Phase 7, 2026-10-20):

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

## Open backlog

### Phase 5c: push the local readers (needed for Docker and remote daemons)

**Built 2026-10-22.** The collector's `watch` mode reads the activity and the
usage cache on a timer and pushes them (`activity` and `usage_cache` envelope
keys, cleaned by `ingest_extras.py`). The usage cache keeps Claude Code's own
`fetched_at`, capped at the receive time and refused when over seven days old
or in the future, as designed below. The readers live in the collector as a
word-for-word embedded copy, refreshed by `tools/build_collector.py`. Still
open: running it on a second real computer. The "untested on a remote host"
notice the plan asks for is on the admin page beside the collector settings and
in the collector guide.

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

### Phase 5b (optional, only if 5 is unacceptable): `Stop` hook

**Phase 5b (optional, only if 5 is unacceptable): `Stop` hook.** Hooks are
documented to run in IDE extensions. A `Stop` hook running the collector in
activity mode sends a heartbeat and sets `last_activity_at`. It needs a
settings edit (appended to any existing `hooks.Stop`, restored on removal) and
a manual check that it fires in the panel before building. It gives less data
than Phase 5.

### Phase 6 (optional): Codex over push

**Built 2026-10-22**, in the collector's `watch` mode rather than a second
file (the plan's `codex_appserver.py` became a mode of `presto_collector.py`).
`CodexSource` accepts push: a pushed reading under five minutes old replaces
the local app-server, and an old one is shown, ageing, when this machine cannot
read Codex. `reset_credits` and `credits` are allowed envelope keys with the
same shape and caps as the daemon's own parsing, which now lives in
`collectors/codex_report.py` and is shared. The original design follows.

Write `collectors/codex_appserver.py` that runs on the developer machine,
reads Codex windows through the supported app-server call already used by
`sources/codex.py`, and pushes them with the same envelope. Set
`accepts_push` on `CodexSource`, which then prefers a recent pushed reading
over spawning a local app-server. This is what makes Codex usable with a
containerized daemon without putting a Codex login in the container.

Phase 9's envelope part lands here: add `reset_credits` (and the optional
`credits_balance` and `credits_unlimited`) to the allowed envelope keys
explicitly, with the same shape and caps; the validator rejects unknown keys
today. See Phase 9 in the archive.

### Device diagnostics

Shipped (2026-10-20): the offline screen and the serial log show the
exception type, where the fetch stopped (conn/send/head/body/parse), the
failure count, uptime, free memory and Wi-Fi state; the device sends
`X-Presto-Client` and `X-Presto-Revision` on every request and the Status tab
lists each client.

Shipped (2026-10-25): the `X-Presto-Diag` header, for example
`err=OSError 110;st=conn;n=3;free=41;up=312;rssi=-61;drops=1`, at most 120
ASCII characters. The device builds it from the same probe function that
feeds the offline screen, so the two cannot drift apart; it never carries the
text of an exception, only the type and the errno. The daemon treats it as
untrusted input: it is parsed against a fixed whitelist, a pair it does not
know or that is out of range is dropped rather than clamped, and a rejected
value is never echoed into a log, a response or the page. The daemon logs a
change of diagnostics once, and the Status tab shows each client's last
error, failure count, free memory, signal and uptime.

Memory only: the daemon keeps at most 16 client records, each with at most
seven short values, and forgets them when it restarts. Nothing is written to
the database and no new endpoint or table was added.

The one limitation: a device that cannot reach the daemon cannot report the
failure, because the request that failed never arrived. The header therefore
carries what an earlier failure left behind, and the Status tab's "Last seen"
column is what says a device is silent at all.

### Several Presto devices

The daemon already serves several clients and the Status tab lists each one
by its declared name and address. What is not built is real per-device
identity and per-device settings (from the admin page reorganization,
2026-10-20):

   - **Not now: per-device tokens and IDs.** A token per device (created and
     revoked in the admin page, like the ingest tokens) would give real
     identity, revocation of one lost device, and per-device settings (which
     accounts to show, its own order and strip size). That is the unscheduled
     "Multiple Presto devices" item in `docs/future.md`; it costs a setup
     step per device and a token in each device's `secrets.py`. Build it when
     per-device settings are wanted, reusing the ingest-token storage and
     admin patterns. The client header above is forward compatible with it.

`docs/future.md` keeps the unscheduled "Multiple Presto devices" entry.

### Phase 8 (optional, only if asked): OpenTelemetry activity detail

The original design (written 2026-10-18):

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

The research and the gate (2026-10-21; backlog item 4):

4. **Phase 8, OpenTelemetry (CLOSED 2026-10-21: the VS Code panel does not emit).** Official and
   documented (code.claude.com/docs/en/monitoring-usage): `CLAUDE_CODE_ENABLE_TELEMETRY=1`
   plus `OTEL_*` variables; metrics `claude_code.cost.usage` (USD),
   `claude_code.token.usage` (input, output, cacheRead, cacheCreation, by model),
   `claude_code.active_time.total`, session, commit and lines-of-code counts; events
   `api_request` (`cost_usd`, `duration_ms`, token counts, `model`), `api_error`,
   `tool_result` and others. Protocols: gRPC, HTTP/JSON, HTTP/protobuf; there is no
   default, so one must be set. What it would add to what exists: cost in USD
   (notional on a subscription), request latency and API error counts. Quota percent
   and token totals already come from the usage cache and the session logs, so the
   gain is detail, not coverage.
   - **Privacy (the real cost).** `user.email` is always attached to every metric and
     event, with no switch to turn it off, plus `user.id`, `organization.id`,
     `user.account_uuid` (switchable) and `workspace.host_paths`. Prompts, responses,
     tool inputs and raw API bodies appear only when the user opts in. Any receiver
     must keep an ALLOWLIST (metric and event names, model, token type, numeric
     cost, duration, token and status fields) and drop everything else before
     anything is stored, logged or returned; tests with hostile payloads (email,
     account ids, prompts, paths) must prove nothing else survives. Telemetry goes
     only where `OTEL_EXPORTER_OTLP_ENDPOINT` points; the daemon would only listen
     on an opt-in port.
   - **Setup is the user's own act** (shell environment or `~/.claude/settings.json`
     `env`); the app never edits it. Project settings cannot set the exporter
     variables, the values are read once at startup, and VS Code needs a restart.
   - **Open question: does the VS Code panel emit at all?** Two GitHub reports say it
     may not: anthropics/claude-code #35105 (the extension's pipe/stream-json mode
     exports nothing) and #72261 (the extension stopped reading its environment
     settings after 2.1.193, June 2026). Both were closed by a bot for inactivity,
     not fixed or disproved, so nobody can say. `tools/otel_probe.py` (a loopback
     listener that prints only names, counts, harmless values and PRESENT markers,
     never the email, ids or prompts; `daemon/tests/test_otel_probe.py`) answers it.
   - **Result (2026-10-21): negative for the panel.** The probe was listening, the
     variables were confirmed inside VS Code (`echo` showed the endpoint) and VS Code was
     restarted with them. A message sent through the Claude panel produced no request and
     no stray contact. The control, `claude -p` from a terminal with the same variables,
     produced 3 requests (2 logs, 1 metrics) with `app.entrypoint` = `sdk-cli`, so the
     probe and the variables work and the panel simply emitted nothing. That matches
     #35105 and #72261. What the CLI carries, for the record: metrics
     `claude_code.cost.usage`, `token.usage` (input, output, cacheRead, cacheCreation),
     `active_time.total`, `session.count`; events `api_request` (cost_usd, tokens with
     5-minute and 1-hour cache creation, duration_ms, ttft_ms, model, effort, speed),
     `assistant_response`, `user_prompt`, `mcp_server_connection`, `plugin_loaded`,
     `managed_settings_resolved`; every record has the email, ids and (redacted) prompt
     and response attributes present. The control cost about 25,000 tokens (mostly
     cached context), $0.09 at API rates. Phase 8 is closed: the terminal CLI alone is
     not worth a receiver for a user who rarely runs it. Revisit only if the probe,
     re-run after a major extension update (its banner lists the steps), shows
     `app.entrypoint` = `claude-vscode`.
   - **Decision gate.** If the probe sees `claude_code.*` from the panel with
     `app.entrypoint` = `claude-vscode`: design an HTTP/JSON receiver on an opt-in
     port, allowlist first, summarised by hour and day, shown as labelled
     "OpenTelemetry" detail. If nothing arrives: close Phase 8 for the panel (the
     terminal CLI alone is not worth a receiver for a user who rarely runs it).

### M8 acceptance (Codex)

5. **M8 acceptance**: sign-in, cancel and sign-out accepted live; expired-login
   recovery; second account or profile if one exists (else mark untested);
   other buckets via fixtures; a contract test that regenerates Codex's
   schema and checks every field read still exists.

**Controls added 2026-10-25** (the paragraph below records the gap): each Codex
account's Setup section now has Sign in with browser, Sign in with device code,
Cancel sign-in and Sign out of Codex buttons, and the help text points to them.
Not yet tried against a real Codex login.

Also found on 2026-10-21: the admin API has the Codex sign-in, cancel and
sign-out routes (`/api/admin/sources/<id>/codex-login`,
`/api/admin/codex-logins/<id>`, `.../cancel`), but the admin page has no
controls that call them, while the Codex help text in `web/admin.js` says
"available through the Codex login controls". Either add the controls (a
setup action the user starts, allowed by `AGENTS.md`) or correct that text.

### Docker: what is still open

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
   documentation in `docs/user/getting-started/install-with-docker.md`. The image was built and run: 94 MB,
   non-root, the data in a volume, the health check and the token routes
   verified. **Still open**: Phases 5c and 6 (the activity and usage-cache
   readers over push, and Codex over push), and a real collector on a
   second machine.

### M13 demo release

7. **M13 demo release**, last.

The milestone's scope (from `docs/milestones.md`): setup guide, screenshots,
sample config, supported subscription paths, limitations and troubleshooting.
Checklist as of 2026-10-21:

- [x] Setup guide, supported subscription paths, limitations and
  troubleshooting: the documentation site (Phase 7).
- [x] Photographs of the real device: README and the docs site.
- [x] Sample configuration: `examples/config.example.toml`,
  `presto/config.example.py`, `.env.example`.
- [ ] The manual hardware checklist that tests cannot automate (the table in
  `docs/limitations.md`, "What cannot be automated in tests"): pixels,
  touch, the seven LED colours, the piezo sound, Wi-Fi association,
  backlight and battery, the boot button.
- [ ] A version and a tag (the package is `0.1.0`; no tag exists), and
  release notes.
- [ ] A final private-data scan of the repository and the built site.

### Loose ends

- Never exercised: a real collector on a second computer, the Windows
  PowerShell snippet from a real remote Claude session, and the POSIX
  collector paths (state directory permissions, `O_NOFOLLOW`); the tests for
  those are skipped or simulated on Windows.
- The one intermittent `test_management` failure (seen once) was not
  reproduced in 30 runs of the file and 5 full-suite runs on 2026-10-20. The
  suspect stays a hypothesis: the admin handler answers a rejected
  CSRF/Origin request before reading its body, and closing a socket with an
  unread body makes Windows reset the connection.
- The weekly-reset estimate does not account for the clock change;
  `docs/claude.md` says what it assumes and when to check it against a real
  reading.
- Phase 5d open points for the author (2026-10-20), not recorded as answered:
  confirm the per-profile path of the usage cache for `CLAUDE_CONFIG_DIR`, and
  how often Claude Code refreshes the cache passively.
- Ollama Cloud reports activity totals only, no allowance; nothing to fix
  with its current API.

## Done means

For the ingest work (Phases 1-6):

- Default install: no new listener, local Claude export behaves as before.
- With ingest enabled, a remote collector updates Home within one poll, a
  heartbeat-only collector shows the right hint, and a stopped collector ages
  into `No report Nd` without ever showing a fresh 0%.
- All tests pass; PSL checked for each hint state; device synced and verified
  (`tools/presto_device.py sync` and `check` on the current port).
