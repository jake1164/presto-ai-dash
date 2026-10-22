# Milestone tracker

## Current status (2026-10-21)

**Built:**

- **M0 to M12**: the repository, the static and connected Presto app, the
  daemon with SQLite history, the Ollama and LiteLLM sources, local
  configuration with encrypted keys, the reusable quota model, Codex,
  Claude, OpenCode Go and Ollama Cloud, the live quota Home, and alert polish
  (M12 with M12b per-service alerts and a master switch, M12c the bell on
  each card, and the mute that expires). M6 was a readiness review; M8 has
  acceptance work left (below).
- **Phases 1 to 5d, 7 and 9** of the
  [remote ingest plan](remote-ingest-plan.md): pushed observations and remote
  tokens, freshness hints, the Claude activity reader, the Claude usage cache
  reader, the documentation site with in-app help links, and banked reset
  credits. Also the Home overflow strip, the admin page's three tabs, cards
  v2 with tiles and lazy card detail, and tapping rows and page dots.
- **Docker**: `Dockerfile`, `compose.yaml`, the admin bind address and
  allowed hosts, the health check.
- **The documentation site**: <https://jake1164.github.io/presto-ai-dash/>,
  built from `docs/user/`.

**Open:** Phase 5c (push the Claude activity and usage-cache readers, for a
containerised daemon), Phase 6 (Codex over push), Phase 8 (OpenTelemetry;
researched, decision gate pending), device diagnostics (`X-Presto-Diag`),
several Presto devices, M8 acceptance (real expired-login recovery, a second
account, sign-in controls on the admin page), and M13, the demo release. The
live list is the backlog in [the remote ingest plan](remote-ingest-plan.md).

The daemon suite has **1409 tests** (3 skipped) as of 2026-10-21. The sections
below are the historical record; dated corrections mark statements that are
no longer true.

## Milestones

Status of each milestone in `presto_ai_dash_project_plan.md` §16.

| # | Milestone | Status | Notes |
| --- | --- | --- | --- |
| 0 | Repo skeleton | **done** | Repo shell, MIT licence, README, docs, config format, sample status JSON. |
| 1 | Static Presto demo | **done** | Three cards from static sample data, swipe/tap/long-press, LED colours, offline screen. |
| 2 | Daemon with fake source | **done** | Runnable stdlib daemon, authenticated `/api/v1/status`, fake source, logging, TOML config, minimal snapshot insert/read; 31 M2 tests. |
| 3 | SQLite history and retention | **done** | UTC migration, bounded snapshot/alert history, atomic observations, 72-hour cleanup, safe HTTP shutdown; 15 M3 tests. |
| 4 | Ollama source | **done** | Version, local model inventory and loaded state; optional observed sample and tokens/sec; no fabricated usage history. |
| 5 | LiteLLM source | **done** | Authenticated daily request/token/spend activity, health/readiness, partial-data warnings; fake and live modes are mutually exclusive. |
| — | Local source configuration | **done** | Separate loopback admin page, multiple source instances, encrypted SQLite keys with external master key, live edits, seven-day remembered browser sessions; 24 management tests. |
| — | Connected Presto | **done** | Async authenticated polling, dynamic per-account cards, live source-instance Home, bounded retry/stale/offline states; 13 live tests, USB deployment, and actual Wi-Fi/auth/recovery checks. |
| 6 | Subscription reporting readiness | **review complete; terminal paths verified** | [M6 findings](m6-readiness.md): real Codex Plus weekly quota and authenticated Go rolling/weekly/monthly quotas verified; user confirmed Go matches Console. The later [Claude checkpoint](claude.md) verifies its terminal export. Graphical-panel coverage remains unverified. |
| 7 | Reusable quota model | **done** | Multiple accounts/windows, provider periods/resets, freshness, unavailable/unlimited states, versioned API projection; [contract and verification](quotas.md). |
| 8 | OpenAI/Codex subscription source | **login workflow implemented; acceptance pending** | [Codex source](codex.md) reads live weekly quota; UI/database settings, bounded app-server, bucket/period handling, failure recovery, and dashboard-managed browser/device-code sign-in, cancellation, and logout. Actual daemon and Presto verified with five accounts/six cards. Real expired-login and second-account acceptance remain. Other ChatGPT limits require separate evidence. |
| 9 | Claude subscription source | **terminal implemented; live verified** | [UI-managed terminal exporter](claude.md), direct SQLite telemetry, encrypted prior status line, 5h/Weekly windows and idle freshness; 16 focused tests. Real 0%/34% readings and resets verified through the daemon and Presto. Graphical-panel coverage and separate usage-page comparison unverified. |
| 10 | OpenCode Go subscription source | **implemented; live verified** | Authenticated 5h/Weekly/Monthly percentages/resets, UI-managed encrypted keys. Current account reads 0%; nonzero/reset/failure recovery tested with fixtures. |
| 10a | Ollama Cloud source | **activity implemented; credits unavailable** | Separate cloud account/key in UI/database; read-only dated requests/tokens/USD. Current response has no monthly allowance, balance, or reset; no invented credit percentage. |
| 11 | Live quota Home on Presto | **done** | Grouped stacked account bars, actual periods, per-account swipe cards/reset details, bounded carousel dots, scrolling and stale/unavailable states. Real-data/font/navigation checks pass; user confirmed bars and all five cards look good. |
| 12 | Alert polish | **done** | Daemon-side percent → status mapping (warning 80%, critical 95%, rate limited 100%) with configurable thresholds, per-service chirps, LED priority, quiet hours judged by the daemon's clock, mute, the alarm pop-to-top with its debounce rules, and the admin Alerts section. The sound is off until it is turned on, and a long-press mute expires (four hours by default) with a chip that says how long is left. |
| 13 | Demo release | not started | Setup guide, screenshots, sample config, supported subscription paths, limitations, and troubleshooting. |

Corrections (2026-10-21):

- **Local source configuration:** the admin page binds to loopback by
  default, but since 2026-10-20 it can bind elsewhere (`admin.host`,
  `DASH_ADMIN_HOST`) with an explicit token and allowed-host list, for Docker.
- **M8:** the browser and device-code sign-in, cancellation and logout exist
  as daemon admin API routes, but the admin page has no controls for them;
  sign in with Codex's own login flow.
- **M9:** graphical-panel coverage was settled on 2026-10-20: the VS Code
  panel does not run the status line. Panel users get quota from the usage
  cache reader (Phase 5d) and activity from the local logs reader (Phase 5).
- **M13:** the setup guide, troubleshooting, limitations and the supported
  paths now exist on the documentation site, and the README and the site
  carry photographs of the real device. The release itself has not started;
  its checklist is in [the remote ingest plan](remote-ingest-plan.md#m13-demo-release).

The 2026-10-11 planning update prioritizes **subscription allowances** for
OpenAI/Codex, Claude, and **OpenCode's own Go subscription** before polish and
release. Former M6/M7 are now M12/M13; completed M0–M5 retain their scope.
OpenCode Go, Ollama Cloud activity, and Codex reporting are now implemented;
Claude's terminal reporting is live verified; Codex's dashboard login workflow
and Claude's graphical-panel coverage remain pending. See
[subscription monitoring](subscription-monitoring.md) for verified documentation,
readiness gaps, and acceptance criteria. API billing and application usage
monitoring remain separate work. Stop after a requested readiness/review
deliverable unless implementation is explicitly requested.

Correction (2026-10-21): Claude's graphical-panel question is settled (see
M9 above), and Codex's login routes exist in the admin API without controls
on the page; M8's acceptance work is what remains.

## Milestone 6 readiness result — 2026-10-11

The [review](m6-readiness.md) records installed versions, bounded read-only
checks, a minimized real Codex reading, official reporting contracts, and
account-level gaps. Codex's observed primary window is weekly, with no secondary
window; the UI must not assume primary means daily or five-hour usage. Claude
needed a real export at review time; the later Claude checkpoint verifies it.
A user-provided Go key now returns HTTP 200 with
three real quota windows and resets. The user confirmed all three 0% readings
match the subscribed account in Console. Nonzero/reset/recovery checks remain
implementation acceptance work; per-model attribution is not implied.

M6 itself changed no runtime/device files. The subsequently authorized cloud
implementation completes M7 and implements M10 plus Ollama Cloud activity;
M8 reporting is now implemented for an existing login; its dashboard login
workflow remains pending. M9's terminal exporter and real-data path are now verified;
graphical-panel coverage remains unverified.

## Cloud implementation checkpoint — 2026-10-11

The [quota contract and verification](quotas.md) cover multiple accounts,
provider periods and resets, truthful stale/unavailable states, and UI-managed
encrypted credentials. Fresh Go reads succeed with standard JSON/User-Agent
headers and show three 0% meters. Ollama Cloud reports dated activity; monthly
allowance and balance are absent from the current response. Both accounts
were imported into SQLite and verified after reopening; temporary key intake
files were removed. A consistent database backup was retained.

All 256 host tests pass. Frontend smoke checks pass for cloud source options,
readings, create/edit/key replacement/delete, refresh settings, and login
restoration. The dynamic carousel and grouped quota Home were backed up and
deployed on COM3. Real-data/font/navigation checks pass on-device, with four
accounts and five cards. The user confirmed bars and all five swipe cards look
good. No Codex or Claude runtime
adapter is claimed by this checkpoint.

After restarting, the actual daemon returns authenticated HTTP 200 with quota
schema version 1 and healthy Go/Cloud sources; missing auth returns 401. Its
source settings now come only from SQLite; obsolete private bootstrap source
sections were removed while preserving daemon connection settings.

## Codex live checkpoint — 2026-10-11

After the user restarted the VS Code daemon, Codex reports healthy with a fresh
14% Weekly reading and provider reset time; its optional secondary window
remains unavailable. Actual status authentication checks return 401/200.
The Presto fetches five accounts and six cards, including Codex's Home meter
and account card. Automated real-font layout, swipe/wraparound, selection
preservation, and clipped-scroll checks pass. The app remains running and COM3
is released. The [Codex checkpoint](codex.md) records remaining M8 login work.

## Claude implementation checkpoint — 2026-10-11

The [Claude source and UI-managed setup](claude.md) are implemented. A database
backup preceded schema v4 migration. The real account and status-line exporter
are configured. After the user's normal Claude Code terminal response, the daemon
reports fresh **5h 0% / Weekly 34%** readings with provider reset times. Actual
status auth passes 401/200. The Presto fetches the same readings across six
accounts/seven cards; real-font layouts, swipe/wraparound, selection preservation,
and clipped Home scrolling pass. The app is running and COM3 is released.
All 285 tests and frontend setup/removal checks pass. Graphical-panel coverage
and separate usage-page comparison remain unverified. Display polish is deferred.

## Presto refresh recovery checkpoint — 2026-10-11

An unusable refresh previously replaced recent working cards with a full-screen
**Bad data** error. Failed refreshes now retain the last good Home and cards,
marked **STALE - retrying**, until their age reaches `OFFLINE_AFTER_S` (default
30 seconds). Invalid polls never advance the last-good timestamp. First boot
without usable data, expired last-good data, and rejected authentication still
show clear errors. Incomplete HTTP transfers count as connection interruptions;
malformed replies log a fixed diagnostic reason without response contents or tokens.

All **288 host tests pass**, including interrupted socket transfers, repeated
invalid refreshes, bounded staleness, and recovery. Both changed device code
files were backed up, deployed and byte-verified. On-device failure injection
verified stale Home/card rendering, persistent-error behavior, and real recovery;
20 subsequent live refreshes passed across six accounts/seven cards. The app
is running and COM3 released. The user's underlying intermittent response failure
was not reproduced during either 20-poll probe; its cause remains unconfirmed.

## Housekeeping checkpoint — 2026-10-17

Fixed the one failing host test. On Windows the Claude status-line helper located
Git Bash by assuming `git.exe` sat in `cmd/`; with `ucrt64/bin/git.exe` first on
PATH it fell back to PowerShell, where a quoted command string was never invoked,
so a chained prior status line produced no output. `_git_bash()` now walks up from
`git.exe` to find `bin/bash.exe`, and the PowerShell fallback runs the command with
`&`. All **288 host tests pass**. Removed ignored build cruft (stray
`%SystemDrive%` directories created by a bad path expansion, temp databases,
`build/`, `.uv-cache/`, `__pycache__`). Local data, keys, backups, and the virtualenv
were kept. README and limitations refreshed to match the current state.

## Account ordering checkpoint — 2026-10-17

The admin account list now supports drag grips, up/down buttons, and arrow
keys on a focused grip. Order saves atomically in SQLite schema v5, survives
restarts, and follows through to both Presto Home and the account carousel on
the next successful poll. Disabled accounts retain their position, new ones
append, and stale reorder requests return 409. Ordering leaves source revisions,
credentials, and in-flight observations intact. Older databases retain their
existing sequence during migration.

All **299 host tests pass**. Browser checks verified dragging, keyboard moves,
and persistence after a page reload; the live PSL showed the reordered Home
and first account card. Test moves were restored to the original order. Physical
device deployment remains with the user; the updated `presto/home.py` is needed
for Home to follow the saved order instead of grouping quota accounts first.

The admin list also has an Enabled checkbox on each row. Toggling it saves
immediately without opening the editor and retains the source's settings,
key, history, and position. Browser checks verified disabling, persistence
after reload, and re-enabling; PSL hid the source on its next poll. All 299
host tests pass.

Duplicate-daemon follow-up: a background verification daemon remained running
when the VS Code profile started another. Both could bind 8787/8788 because
Windows permits overlapping listeners with `SO_REUSEADDR`. The extra background
process was stopped, preserving the VS Code daemon and PSL. API/admin servers
now use exclusive Windows socket binding and disable shared-port reuse. Two
regression tests reject duplicate status/admin listeners; all **301 tests pass**,
including restart/session persistence checks. PSL continues receiving live data.

## Shared app and explicit USB sync checkpoint — 2026-10-17

PSL already ran the real device App and renderer. The remaining drift was the
physical device's uploaded copy. One public source inventory now drives the
PSL loader and USB deployment; PSL executes a frozen source snapshot, bypassing
bytecode caches, and shows its revision plus the dated last USB verification.
Source changes invalidate a prior matching check. New modules join automatically;
removed helpers are detected and retired from the device import path.

VS Code tasks **Sync Presto** and **Check Presto sync** use an explicit USB port.
Sync stages and hashes all app files before replacement, rolls back ordinary
write/rename failures, verifies installed hashes, and relaunches AI Dash. Device
config/secrets and firmware `/main.py` are excluded. OS locking prevents overlapping
USB operations. No automatic save-to-device behavior was added.

All **318 host tests pass**, including source selection, snapshot loading, drift,
private-file preservation, failed staging, rollback, relaunch failures and locking.
The actual hash-only COM3 check succeeded and found only `home.py` different;
no app files were uploaded. AI Dash was relaunched. The revision/drift footer and
live Home were visually verified. Explicit deployment remains with the user.

## Milestone 1 detail

Acceptance criterion from the plan:

> Presto shows AI Dash, Ollama, and LiteLLM cards using fake data.

Delivered:

- `presto/cards.py` — card model, status vocabulary, plan card ordering
- `presto/gestures.py` — tap / swipe / long-press recogniser
- `presto/alerts.py` — status → LED colour and sound cue, mute, no-repeat
- `presto/net.py` — HTTP client plus static and embedded sample payloads
- `presto/ui.py` — PicoGraphics drawing
- `presto/main.py` — the only module that imports `presto`/`machine`
- 113 host-side tests, all passing

Not yet done, and why:

- **Live device polling at M1.** M1 proved the UI on static data first, as the
  original build sequence required. The later connected checkpoint below
  implements daemon polling separately from M1/M2.
- **Remaining hardware checks.** LED colours, sound, Wi-Fi, and power behaviour
  still need manual checks. The static carousel/display checkpoint below has
  now been exercised on a real Presto. See `docs/limitations.md`.

### Static hardware checkpoint — 2026-10-10

- Installed alongside the existing firmware carousel on a USB-connected
  Presto running MicroPython 1.29.0; the device's original launcher was backed
  up and left in place.
- Fixed text wrapping/overlap using the real bitmap-font pixel measurements.
  On-device checks verify single-line rows on all three 240×240 cards.
- Fixed the MicroPython namespace collision between the entry script and its
  helper folder. The entry is `/ai_dash.py`; helpers live in `/ai_dash_files`.
- User confirmed launch from the firmware carousel stays open and all three
  cards look good after swiping through them.
- This checkpoint uses static fake data. Live daemon polling remains deferred.

### Home quota design preview — 2026-10-10

The on-device Home preview uses fixed daily/weekly cloud quotas labelled DEMO.
Real-font layout, combined daily/weekly meters, missing-quota rendering, and
existing card navigation pass checks on the Presto. This preview adds no cloud
integrations or daemon polling and is separate from M2.

It groups two stacked bars per provider: cyan daily above purple weekly, on
the same 0–100% scale with a gap. Both percentages are shown against their own
period's limit. The initial overlay was hard to distinguish on the panel;
the stacked revision is deployed for user review.

Added a fourth demo row, OpenCode.ai (27% daily / 54% weekly), to exercise
overflow. Vertical dragging follows the finger within bounds, with a pinned
header/legend, clipped rows, and a scrollbar. Horizontal swipes keep their
carousel role; drags cannot trigger long-press mute. Removing continuous idle
redraws improved card-switching feel. User confirmed scrolling works and looks
good, then confirmed switching looks good after that refinement. All 170 host
tests and the on-device clipped-layout/navigation checks pass.

### Connected Presto checkpoint — 2026-10-10

- `STATIC_DEMO = False` enables bounded asyncio HTTP polling with the device
  shared token; Wi-Fi attempts use existing firmware support and root
  `/secrets.py`. Touch/rendering continues during network waits.
- Live Home lists configured source instances and measured usage, preserving
  the three-card carousel. Unknown usage stays unavailable; cloud quota demo
  values are confined to static mode. Config edits appear on the next poll.
- Errors distinguish Wi-Fi, daemon reachability, rejected token, and bad data.
  Last good data is visibly stale for a bounded interval; reconnect restores
  the selected card. HTTP sockets close on success, errors, and cancellation.
- 12 focused live tests cover localhost daemon/auth, fragmented HTTP, timeout
  and retry, cooperative scheduling, stale/offline recovery, multiple-instance
  scrolling, and honest missing data. All 228 host tests pass.
- Backed up the installed app and firmware launcher in ignored
  `.local-backups/presto-live-before/`, deployed the new app on COM3, and left
  firmware `/main.py` untouched. Real-font checks pass for all three cards and
  a five-instance scrolling Home. The bounded live loop handles missing Wi-Fi
  without exiting to the carousel.
- The local daemon serves authenticated fake data at `192.168.1.50:8787`.
  Its shared token is persisted in ignored `dash.toml` and `presto/config.py`.
  User-provided Wi-Fi settings were deployed privately to root `/secrets.py`;
  the Presto joined the LAN and fetched the daemon's fake source over HTTP.
- On-device checks pass for rejected-token handling and recovery, an unreachable
  test endpoint with stale/offline transition, selected-card recovery, and
  Wi-Fi disconnection/reconnection. Fixed lazy root `secrets.py` lookup after
  the carousel changes into `/ai_dash_files`. A disconnected cold-start live UI
  also joins Wi-Fi and renders authenticated daemon data. The app is left running.
- User confirmed the FAKE Home display and swiping through Home, Ollama, and
  LiteLLM work while daemon values update.

### Configuration login follow-up — 2026-10-11

- Browser sessions survive daemon/database restarts and fresh startup admin
  codes for seven days. SQLite stores credential hashes; logout revokes the
  current session. Cookie-authenticated edits require an exact Origin and CSRF
  token. The existing device-token API remains unchanged.
- Local Ollama is labelled **local API · no key required**. Fake remains an
  explicitly labelled demo/test connector. OpenAI/Codex, Claude, and OpenCode
  Go appear as disabled planned choices, with no runtime adapter path.
- All 235 host tests pass, including session expiry/revocation, code rotation,
  and schema migration preserving encrypted keys/history. Direct frontend
  smoke checks pass for reload, restart restoration, source save, labels,
  and logout. Visual browser verification was unavailable in this session.

## Test counts

The subsequent [Codex reporting checkpoint](codex.md) adds 13 tests and the
[Claude exporter checkpoint](claude.md) adds 16, bringing
the host suite to 285 passing tests. Three subsequent refresh-recovery tests bring
the current complete suite to **288 passing tests**. The prior cloud/device suite
breakdown is below; its original checkpoint count is preserved.

Correction (2026-10-21): these counts are historical. The suite now has
1409 tests (3 skipped).

| Suite | Tests |
| --- | --- |
| `test_cards.py` | 35 |
| `test_gestures.py` | 20 |
| `test_alerts.py` | 34 |
| `test_net.py` | 23 |
| `test_presto_structure.py` | 7 |
| `test_m2.py` | 31 |
| `test_m3.py` | 15 |
| `test_sources.py` | 13 |
| `test_management.py` | 24 |
| `test_quotas.py` | 18 |
| `test_live.py` | 13 |
| `test_ui.py` | 4 |
| `test_home.py` | 19 |
| **Total** | **256** |

## Milestone 2 detail

- Python 3.11+ daemon starts from the module or installed console script.
- TOML defaults and environment overrides load and validate; missing tokens
  are generated once per process and required for status access.
- Fake source cycles through all seven statuses. The `dash`, `ollama`, and
  `litellm` cards all display explicitly labelled fake metrics.
- SQLite snapshot insert/read is minimal support for the API. Timestamp
  strings are decoded with `datetime.fromisoformat` before rendering.
- Import/compile checks, all 142 host tests, and a separate-process localhost
  smoke check pass: missing/wrong auth returns 401, valid auth returns 200.
- Real-source modules are disabled drafts with no imports or registration
  in M2. Config cannot enable them. M3 now adds internal alert history and
  retention; thresholds, alert rules, and trends remain deferred.

Run with:

```bash
cd daemon
python -m unittest discover -s tests -v
```

## Planned checkpoints beyond M2

The [M3 readiness review](m3-readiness.md) was completed against `93f1a46`.
It reproduced four gaps: mixed-offset ordering, import-time fake alert
timestamps, fabricated freshness without data, and active request reads
surviving API shutdown. The approved implementation now addresses these cases;
see the handoff's implementation result for validation.

The M3 implementation added 15 focused tests; its handoff suite had 185
passing tests on Windows / Python 3.13. M4/M5 add 12 source tests, including a
localhost HTTP fixture that exercises both provider clients without connecting
to real providers. The configuration checkpoint adds 17 management tests;
the connected Presto checkpoint adds 12 live tests. The current complete suite
has 229 passing tests after the LiteLLM warning-policy regression check.

LiteLLM's daily failure count now remains visible as history without keeping
a healthy source in warning all day. Current authentication and permission
failures still warn; liveness failures remain offline or critical. HTTP 403
now identifies missing usage permissions rather than a rejected key.

The [local configuration checkpoint](configuration.md) adds reusable source
instances, a separate loopback-only admin listener/token, encrypted credentials
in SQLite schema v2, and live updates. The encryption master key stays outside
SQLite; generated Windows key files are protected with DPAPI. Existing history
is preserved, key replacement keeps instance IDs, and keys are never returned
by API reads. This does not implement direct cloud-account connectors,
application attribution, or live Presto polling.

Validation: all 216 host tests pass on Windows/Python 3.13, including 17
management tests. A separate-process fake-data daemon passes status auth
checks (401/200), serves admin assets, and supports encrypted key creation,
replacement, and deletion over localhost HTTP. CLI/import checks, JavaScript
syntax, and wheel asset imports pass. Visual browser verification remains
pending because no browser surface was connected in the implementation session.

- **Device polling integration:** authorized and implemented in the separate
  checkpoint below. M2's original scope remains unchanged.
- **M4–M5:** Ollama and LiteLLM sources are implemented. The daemon uses fake
  data by default; enabling a real source requires explicitly disabling fake.
  Ollama has no historical request endpoint, so usage remains unavailable
  unless an external collector writes the optional sample file.
- **Application monitoring (after M5):** a separate future checkpoint for
  multiple configured apps with stable identities, LiteLLM attribution first,
  reusable summary/detail views, and optional app-defined budgets. Direct-call
  reporting follows later. See [the design and acceptance outline](future.md#application-usage-monitoring).
  No book-app special cases or M3 application schema are required.
- **Deployment targets:** v0 direct Python/pip/pipx; v0.1 optional Docker;
  v0.2 optional separate tray runner; v1 binaries/installers if useful.
  These targets are plans, not delivered features. The planned daemon command
  is `presto-ai-dash-daemon`; today's command is `presto-ai-dash`.
  Correction (2026-10-21): the optional Docker image is delivered
  (`Dockerfile`, `compose.yaml`, see
  [Install with Docker](https://jake1164.github.io/presto-ai-dash/getting-started/install-with-docker/)).
  The tray runner, binaries and the `presto-ai-dash-daemon` name are still
  plans.
