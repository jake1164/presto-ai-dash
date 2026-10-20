# Presto AI Dash - Project Plan

## 1. Project summary

**Presto AI Dash** (`presto-ai-dash`): A tiny AI usage dashboard for Pimoroni Presto with support for local and cloud AI providers.

The daemon supports fake, local Ollama, LiteLLM, OpenCode Go and Codex quotas,
and Ollama Cloud activity, with UI-managed account settings and encrypted
SQLite API keys. Codex uses an existing provider-managed subscription login;
its dashboard login workflow remains pending. Claude's terminal exporter is
implemented and verified with live 5h/Weekly readings. The reusable quota contract and first
cloud adapters and expanding live carousel are implemented and deployed. The
Presto passes automated real-data/font/navigation checks; the user confirmed
the bars and all five cards look good. The static Home preview remains labelled DEMO.
See [milestones.md](milestones.md) for implementation status and
[subscription monitoring](subscription-monitoring.md) for the next phase.

It shows whether local and routed AI usage is normal, active, expensive, rate-limited, or broken. It is not trying to be a full analytics platform. The first version should be a useful desk demo that proves the Presto can act as a touch-enabled AI companion display.

## 2. Core idea

The Presto should sit on the desk and answer a few simple questions at a glance:

- Is local Ollama active?
- Is LiteLLM healthy?
- Am I using a lot of tokens today?
- Is anything near a limit?
- How much of each subscription allowance remains, and when does it reset?
- Is anything offline or broken?
- How fast is my local model running?

The device should show this through:

- swipeable cards
- tap-to-detail views
- RGB LED status cues
- optional sound alerts

## 3. Design principles

1. **Demo first**
   - Build something useful quickly.
   - Do not build a full observability platform.
2. **Presto-first**
   - The Presto UI is the product.
   - The daemon exists to make the Presto display simple, safe, and useful.
3. **Local-first**
   - The first useful sources are local Ollama and LiteLLM.
   - The next phase adds subscription monitoring through the local daemon.
   - Support multiple configured accounts without provider-specific UI code.
4. **Honest telemetry**
   - Show what is actually measured.
   - Do not pretend to see direct Ollama usage unless it is routed through a collector or shim.
5. **Expandable but not overbuilt**
   - Use a clean source interface.
   - Do not build a plugin marketplace or external package system in v0.
6. **No provider branding issues**
   - No copied logos, mascots, fonts, or provider-owned artwork in the core repo.
   - Plugin authors can provide their own assets later and are responsible for rights.
7. **Portable and headless first**
   - Core daemon targets Windows 10/11, macOS, and Linux.
   - Python 3.11+, SQLite, TOML, and `pathlib`; no systemd-only or Windows-only service assumptions.
   - Optional desktop conveniences stay outside the daemon core.

## 4. Topology and Telemetry Limits

Current usage pattern:

```text
Other devices -> toontown-int-srv1 -> LiteLLM -> jake-desktop -> Ollama
jake-desktop -> Ollama directly
```

Recommended v0 deployment:

```text
Pimoroni Presto
    ↓ polls HTTP
Presto AI Dash daemon on toontown-int-srv1
    ↓ reads
LiteLLM on toontown-int-srv1
    ↓ routes to
Ollama on jake-desktop

Optional future:
jake-desktop collector/shim -> reports direct Ollama samples -> daemon
```

LiteLLM only sees requests routed through LiteLLM. Direct calls from
`jake-desktop` to Ollama require a collector, shim, wrapper, or voluntary
metrics reporting to measure full usage. Health/model polling alone cannot
reconstruct request or token history. The desktop reporting path is not v0.

## 5. Scope / Non-goals

### Included in v0

The foundation includes:

- Python daemon
- SQLite snapshot storage
- HTTP status API
- fake/demo source
- LiteLLM source
- basic Ollama source
- Presto MicroPython app
- 3 swipeable cards
- tap-to-detail view
- LED status indicators
- optional configurable sound
- basic config file
- simple shared token for Presto-to-daemon access
- clear documentation

The next planned phase adds subscription quota monitoring for OpenAI/Codex,
Claude, and OpenCode's own Go subscription, ahead of alert polish and release.
It includes reusable account instances, actual quota windows and reset times,
freshness/unavailable states, and a scrolling Home summary with separate stacked
bars. Each adapter requires a verified reporting path; these are planned features,
not currently available sources. See milestones M6–M11 below.

### Not included in v0

These are future placeholders, not part of the first build:

- MQTT
- multi-daemon federation
- desktop browser opener
- provider logo packs
- external plugin package system
- provider marketplace
- Prometheus/Grafana export
- direct OpenAI/Anthropic API billing connectors
- other ChatGPT subscription limits beyond verified Codex reporting
- Ollama Cloud credit balance reporting beyond the currently verified activity API
- OpenCode application/session analytics beyond Go quota monitoring
- advanced analytics
- application usage monitoring (separate future checkpoint after M5)
- invoice generation, billing reconciliation, and chargeback accounting
- complex alert rule engine
- cloud-hosted dashboard
- user accounts
- desktop tray runner
- Docker image
- packaged binaries/installers
- multi-host collectors

Currently implemented source types are `fake`, `ollama`, and `litellm`.
The subscription adapters above are planned additions. M2's completed scope
remains fake-only; later work does not expand its acceptance requirements.

## 6. v0 data sources

### 6.1 Fake source

Purpose:

- test UI states
- demo normal/warning/critical/offline modes
- test LEDs and sound
- allow Presto work before real integrations are finished

Example states:

```text
normal
active
warning
critical
offline
rate_limited
near_limit
```

### 6.2 LiteLLM source

Purpose:

- show gateway/routed AI usage

Initial metrics:

```text
LiteLLM online/offline
requests today
tokens today
spend today, if available
top model, if available
recent errors, if available
rate limit warnings, if available
```

This source represents traffic that actually passes through LiteLLM.

### 6.3 Ollama source

Purpose:

- show local AI model health and performance

Initial metrics:

```text
Ollama reachable/unreachable
loaded or recently used model, if available
last measured tokens/sec, if available
last request prompt tokens, if available
last request output tokens, if available
last request duration, if available
```

Important limitation:

Direct calls to Ollama are only fully measurable if they pass through a collector, shim, wrapper, or tool that reports usage. Basic Ollama health can be detected without this, but full token history cannot be magically reconstructed.

## 7. Planned subscription sources

These sources are the next priority, not unscheduled placeholders. M6 first
verifies reporting, account coverage, authentication, and a real redacted quota
sample for each service. M7 defines shared data before M8–M10 implement adapters.
No API spend or token count may stand in for a subscription allowance.
See [the subscription monitoring contract](subscription-monitoring.md).

### 7.1 OpenAI / Codex

Monitor Codex subscription allowances using the documented app-server
`account/rateLimits/read` path and supported account login. Verify the installed
version, plan, reported buckets, window durations, reset times, and refresh behavior.
Codex reporting does not establish coverage of every ChatGPT model limit.
API billing is a separate future feature.

### 7.2 Claude

Use Claude Code's documented statusline quota fields as the candidate reporting
path for supported subscription plans, with a local helper exporting only needed
fields. Verify five-hour and seven-day windows against a real account, preserve
the user's existing statusline, and handle absent or stale readings honestly.
Do not scrape credentials or reverse-engineer private CLI state.

### 7.3 OpenCode Go

Monitor OpenCode's own Go subscription, not usage of OpenAI or Claude through
OpenCode. Its documented allowances include five-hour, weekly, and monthly
windows. [M6](m6-readiness.md) verified authenticated usage reads with the user's
key and three percentages matching Console. Nonzero readings, reset/recovery,
and other account/plan coverage need implementation acceptance checks. Do not
reconstruct quota percentages from session logs or infer per-model breakdowns.

### 7.4 Ollama Cloud

Ollama Cloud is separate from local Ollama. Its current account UI uses monthly
included credits, not session/weekly quota meters. The authorized reporting
read returns dated requests/tokens/USD activity without monthly allowance,
remaining balance, or reset. The implemented adapter reports that activity and
marks the monthly credit meter unavailable. Do not infer percentages from public
plan prices or scrape the account dashboard.

Accounts, keys, and refresh settings are managed in the local UI and stored in
SQLite, with encrypted keys. No provider TOML file is required at runtime.
See [the implemented contract and verification](quotas.md).

OpenCode application/session telemetry remains separate future work. Subscription
monitoring does not introduce a deep analytics parser.

## 8. Runtime Architecture

The complete connected system does not run on the Presto. M1's static demo
can run entirely on the device without Wi-Fi or a daemon. M2 verifies the
daemon independently with fake data; device polling is a separate checkpoint.

### v0 architecture

```text
Sources
  fake
  LiteLLM
  Ollama
      ↓
Python daemon
      ↓
SQLite
      ↓
HTTP API
      ↓
Presto MicroPython app
      ↓
Touch cards + LEDs + sound
```

### Daemon responsibilities

The daemon handles:

- source collection
- talking to LiteLLM and Ollama (M5 and M4 respectively)
- handling provider secrets/API keys
- status normalization
- SQLite storage
- alert calculation
- dashboard JSON output
- simple local security
- configuration

### Presto responsibilities

The Presto handles:

- Wi-Fi connection
- polling daemon endpoint
- rendering cards
- touch interaction
- LED behavior
- sound behavior
- offline/fallback display

The Presto should not store provider secrets or call AI provider APIs directly.

### Deployment Model

The core daemon is a normal Python 3.11+ app, runnable headlessly from source
or installed with pip/pipx. Portability targets Windows 10/11, macOS, and Linux
using SQLite, TOML, and `pathlib`. Service managers are optional deployment
wrappers; neither systemd nor Windows services are assumed.

| Release target | Deployment direction |
| --- | --- |
| v0 | Direct Python / pip / pipx; target command `presto-ai-dash-daemon` |
| v0.1 | Optional Docker image for server/headless use, particularly `toontown-int-srv1` |
| v0.2 | Optional separate `presto-ai-dash-tray` runner/wrapper |
| v1 | Packaged binaries/installers if the project proves useful |

The current installed command is `presto-ai-dash`, or run
`python -m presto_ai_dash.app`. The target command name, pipx validation,
Docker, tray, and binaries are plans; this docs update changes no packaging.
Docker must remain optional, and headless use must remain first-class.

## 9. API design

### Main endpoint

```text
GET /api/v1/status
```

Returns current dashboard state.

### Example response shape

```json
{
  "app": "presto-ai-dash",
  "version": "0.1.0",
  "updated_at": "2026-10-09T11:00:00-04:00",
  "overall_status": "ok",
  "cards": [
    {
      "id": "dash",
      "title": "AI Dash",
      "status": "ok",
      "primary": "128k tokens today",
      "secondary": "Ollama active · LiteLLM OK",
      "detail": [
        "LiteLLM: 42 requests today",
        "Ollama: 38 tok/s last run",
        "Alerts: none"
      ],
      "accent": "green",
      "alert": null
    },
    {
      "id": "ollama",
      "title": "Ollama",
      "status": "active",
      "primary": "38 tok/s",
      "secondary": "qwen model · last run 2m ago",
      "detail": [
        "Reachable: yes",
        "Prompt tokens: 812",
        "Output tokens: 1944",
        "Duration: 51s"
      ],
      "accent": "blue",
      "alert": null
    },
    {
      "id": "litellm",
      "title": "LiteLLM",
      "status": "ok",
      "primary": "42 requests",
      "secondary": "128k tokens today",
      "detail": [
        "Spend today: unavailable",
        "Top model: unavailable",
        "Errors: 0"
      ],
      "accent": "green",
      "alert": null
    }
  ]
}
```

## 10. Source interface

For v0, keep sources internal but structured.

```python
class Source:
    id: str
    name: str
    config_fields: tuple  # keys, labels, types, defaults, bounds
    secret_field: str | None

    def collect(self) -> SourceSnapshot:
        ...
```

Snapshot concept:

```python
class SourceSnapshot:
    source_id: str
    status: str
    updated_at: datetime
    metrics: dict
    alerts: list
    cards: list
```

v0 should avoid a complex plugin loader. The goal is to make adding a source obvious, not to support third-party plugins yet.

The configuration checkpoint discovers trusted registered modules in `sources/`
at startup. It uses these field descriptors to validate and render settings.
Connector type IDs and configured instance IDs are separate: multiple gateways
or accounts can share a type while retaining separate history and credentials.

## 11. Storage

Use SQLite.

### Minimal tables

```text
snapshots
alerts
source_instances
configuration_meta
```

### Snapshot retention

Default:

```text
72 hours
```

This is enough for basic trends without turning the project into analytics infrastructure.

### v0 trends

Only simple trend indicators:

```text
tokens up/down
requests up/down
last speed up/down
provider recently active/inactive
```

No complex charting in v0.

## 12. Presto UI plan

The current Home design preview groups cyan daily and purple weekly bars in
two stacked lanes per cloud provider, daily above weekly. Both show percentage
of their own period's limit, with numeric labels and `--` for unknown values.
OpenAI, Claude, Ollama Cloud, and OpenCode.ai values are fixed and labelled DEMO.
The list scrolls vertically with a pinned header; left/right swipes navigate
cards. This preview does not add cloud sources or change the current API contract.

M11 replaces preview values with verified subscription readings. Labels describe
actual windows (such as 5h, Weekly, and Monthly); Daily is used only for a real
daily window. Stack all reported bars in separate lanes under each account
heading. OpenCode Go's monthly allowance remains visible on Home. Swipe cards
expand to one per enabled account, including multiple accounts of one type.
Preserve the selected account by ID across refreshes and return Home on removal.
Show reset times and distinguish stale, unavailable, and unlimited readings;
never fall back to demo quotas in live mode. Multiple accounts use the same renderer.
Quota accounts lead the live Home list; ordinary local/gateway measurements
follow. The account cards below are examples, not a fixed three-screen sequence.

### Screen 1: AI Dash

Purpose:

- main ambient overview

Shows:

```text
overall status and configured account count
each cloud account's actual quota windows and percentages
stacked short-window / weekly / monthly bars
explicit stale, unavailable, and unlimited states
local/gateway activity below, with vertical scrolling
```

### Example account card: Ollama

Purpose:

- local model activity

Shows:

```text
Ollama status
model name
last tokens/sec
last request age
prompt/output token count, if available
```

### Example account card: LiteLLM

Purpose:

- routed AI usage

Shows:

```text
LiteLLM status
requests today
tokens today
spend today, if available
top model, if available
```

### Example account cards: OpenCode Go and Ollama Cloud

Go shows the account name, reported percentages, and UTC resets. Cloud shows
dated requests/tokens/USD activity and the unavailable monthly-credit meter.
New registered types receive a generic per-account card automatically; quota
reports reuse the shared renderer. The live sequence expands and contracts
with enabled source instances rather than the connector catalog itself.

### Interaction

```text
Swipe left/right: switch cards
Tap: detail view
Long press: silence current alert
Boot/user button: return to overview
```

### Offline behavior

If the daemon is unreachable:

```text
show "daemon offline"
flash red or yellow depending on config
do not crash
retry every 10-15 seconds
```

## 13. LED and sound behavior

### LED colors

```text
green: normal
blue: local model active
yellow: warning or nearing limit
red: critical, provider down, or limit hit
purple: collecting/updating
white: booting
off/dim: muted or idle
```

### Sound

Default recommendation:

```text
sound enabled but quiet
long-press to mute
config option to disable entirely
```

MVP sounds:

```text
short chirp: new warning
double chirp: critical alert
no repeating alarm unless configured
```

## 14. Security

### v0 security rules

- no provider secrets on Presto
- no provider API calls from Presto
- daemon binds to LAN only by default
- dashboard API uses a simple shared token
- managed provider secrets are encrypted in SQLite; the master key lives
  outside it, protected with Windows DPAPI or an owner-only Unix file, or
  supplied through the environment
- local configuration uses a loopback-only listener and a separate admin token
- do not expose the daemon publicly
- do not run paid test prompts unless explicitly enabled

### Example Presto request

```text
GET /api/v1/status
Authorization: Bearer <shared-local-token>
```

## 15. Repository layout

Single repo:

```text
presto-ai-dash/
  README.md
  LICENSE
  docs/
    architecture.md
    setup.md
    sources.md
    limitations.md
    future.md
  daemon/
    pyproject.toml
    src/presto_ai_dash/
      app.py
      config.py
      models.py
      dashboard.py
      api.py
      sources/
        __init__.py
        fake.py
        litellm.py               routed daily activity source (M5)
        ollama.py                local health and sample source (M4)
      alerts.py                  future draft; disabled in M2
      storage.py
  presto/
    main.py
    config.example.py
    net.py
    ui.py
    cards.py
    alerts.py
    gestures.py
    home.py                      static cloud-quota design preview
    launcher/ai_dash.py           firmware carousel entry
  examples/
    sample-status.json
    config.example.toml
```

## 16. Milestones

### Milestone 0: Repo skeleton

Goal:

Create the project shell.

Tasks:

- create repo
- add MIT license
- add README
- add basic docs
- add daemon and Presto folders
- define config format
- define sample status JSON

Acceptance criteria:

```text
Repo explains what the project is, what it is not, and how v0 will work.
```

### Milestone 1: Static Presto demo

Goal:

Make the Presto display fake data without a daemon.

Tasks:

- load sample JSON from file or embedded string
- render 3 cards
- implement swipe/tap navigation
- implement LED status colors
- implement offline/error screen mock

Acceptance criteria:

```text
Presto shows AI Dash, Ollama, and LiteLLM cards using fake data.
```

### Milestone 2: Daemon with fake source

Goal:

Make the Python daemon executable and testable with the fake source only.

Tasks:

- build Python daemon
- expose `/api/v1/status`
- implement fake source
- add shared token support
- add basic logging
- add config file
- keep minimal SQLite snapshot insert/read used by the status API
- run import/smoke checks and local daemon execution
- test config, fake source, auth, dashboard response shape, and storage
- exclude real Ollama/LiteLLM imports and runtime registration

Acceptance criteria:

```text
Daemon starts cleanly, loads config, and serves fake status JSON locally.
Missing/wrong shared-token auth returns 401; valid auth returns 200.
Import/smoke checks and tests pass, including SQLite timestamp round-trip.
```

M2 does not require real services or live Presto polling. A separately
authorized device integration checkpoint should verify Wi-Fi and authenticated
fake-data polling before real provider integration. M1 remains device-only.

### Milestone 3: SQLite snapshots

Detailed scope and acceptance contract: [M3 readiness / Luna handoff](m3-readiness.md).
Readiness and implementation are complete; see the implementation result in
the handoff for the final verification record.

Goal:

Persist simple history.

Tasks:

- extend the minimal SQLite snapshot storage delivered in M2
- query recent snapshot history
- store alerts
- add retention cleanup
- expose last updated time

Acceptance criteria:

```text
Existing M2 databases upgrade without data loss and use correct UTC ordering.
Bounded internal snapshot/alert history survives restart.
72-hour retention removes expired snapshots and dependent alert observations.
Status reports the persisted update time, or null when no data remains.
Concurrent reads/writes/cleanup and active-request shutdown tests pass.
```

History is internal in M3; no new HTTP endpoint, trend calculation, settings
table, incident engine, provider integration, or Presto changes are required.

### Milestone 4: Ollama source

Goal:

Show local Ollama status.

Tasks:

- check Ollama reachability
- collect available model/status data
- support optional measured request stats if available
- calculate tokens/sec when token counts and duration are available
- add Ollama card

Acceptance criteria:

```text
Presto shows whether Ollama is reachable and displays last measured performance when available.
```

### Milestone 5: LiteLLM source

Goal:

Show routed AI usage.

Tasks:

- connect to LiteLLM
- collect basic request/token/spend data where available
- show online/offline status
- add LiteLLM card
- add warning state for source errors

Acceptance criteria:

```text
Presto shows LiteLLM status and useful daily usage metrics.
```

### Milestone 6: Subscription reporting readiness

Review result: [M6 readiness](m6-readiness.md). Codex reporting is verified for
the observed account and Go quota reads match the user's Console meters. Claude
export remains pending. No adapters were implemented during this review.

Goal:

Verify supported quota reporting for Codex, Claude, and OpenCode Go before implementation.

Tasks:

- verify installed versions, subscription plans, account coverage, and authentication
- capture a real redacted quota reading and document window/reset semantics
- classify each path as ready, conditional, or blocked with concrete evidence
- document missing capabilities without substituting API spend or token counts

Acceptance criteria:

```text
Each provider has an evidence-backed readiness result and explicit blockers.
No model request is made solely to obtain quota telemetry.
The readiness deliverable ends at review; implementation needs explicit approval.
```

### Milestone 7: Reusable quota model

Goal:

Represent multiple subscription accounts and independently reported quota windows.

Tasks:

- define stable instance/account identity and a list of typed quota windows
- include percent used, actual duration/label, reset time, observation time, and freshness
- distinguish unavailable, stale, and unlimited states
- extend configuration, status API, and tests while preserving existing sources

Acceptance criteria:

```text
Two accounts of the same type work without new provider-specific UI code.
Optional windows, resets, missing data, and account replacement are tested.
Percentages are not summed and cumulative quota snapshots are not added together.
```

### Milestone 8: OpenAI/Codex subscription source

Reporting checkpoint: [existing-login adapter and verification](codex.md)
implemented on 2026-10-11. Real weekly quota reads work and all 269 tests pass.
Dashboard-managed login/cancellation/logout and real additional-account and
expired-login acceptance remain; M8 is not yet marked complete.

Goal:

Show verified Codex subscription allowances through supported account reporting.

Tasks:

- implement the M6-verified app-server reporting/login path
- normalize available quota buckets, durations, percentages, and resets
- handle authentication refresh and stale/unavailable readings

Acceptance criteria:

```text
Real account readings match the verified reporting contract and tests pass.
Plan-dependent or missing windows remain explicit; other ChatGPT limits are not implied.
```

### Milestone 9: Claude subscription source

Implementation checkpoint: [UI-managed terminal exporter](claude.md), direct
SQLite telemetry and encrypted recovery of the previous status line. All 285
tests pass. A real export during normal terminal use reports 5h 0% and Weekly
34%; authenticated daemon and Presto Home/card checks pass with six accounts
and seven cards. Terminal reporting is live verified. Graphical-panel coverage
and a separate comparison against Claude's usage page remain unverified.

Goal:

Show verified Claude subscription allowances from documented Claude Code exports.

Tasks:

- implement the M6-verified local export/helper path
- preserve existing statusline behavior and normalize available quota windows
- handle absent fields, inactivity, reset times, and freshness

Acceptance criteria:

```text
Supported account readings match real telemetry without credential scraping.
Missing five-hour or weekly readings remain unavailable rather than zero.
```

### Milestone 10: OpenCode Go subscription source

Goal:

Show OpenCode Go's own subscription allowances.

Tasks:

- implement only the supported retrieval/authentication path verified in M6
- normalize five-hour, weekly, and monthly allowances and resets
- document unsupported account or reporting cases

Acceptance criteria:

```text
All reported Go windows match real readings, with authentication and freshness tests.
If retrieval remains blocked, record the blocker and do not claim implementation complete.
```

### Milestone 10a: Ollama Cloud activity and credits

Goal:

Monitor Ollama Cloud separately from local Ollama through a UI-managed account.

Implemented: encrypted database credentials and refresh settings, fixed read-only
HTTPS reporting, dated request/token/USD activity, safe failures and stale reads.
The current response has no monthly allowance, remaining balance, or reset;
these remain explicitly unavailable until a supported reporting path is verified.
Do not invent a monthly percentage from public plan prices or activity totals.

Acceptance: show measured activity, preserve its reporting range, and label the
missing credit meter honestly. Account/key lifecycle must work entirely in the
UI, with credentials absent from public API responses and Presto configuration.

### Milestone 11: Live quota Home on Presto

Goal:

Make subscription allowances readable at a glance across multiple accounts.

Tasks:

- render scrolling account rows with a pinned header and separate stacked bars
- generate Home plus one swipe card per enabled account, preserving selection by ID
- label actual periods, show every reported window on Home and reset times in details
- preserve horizontal card navigation and show stale/unavailable states
- verify multiple accounts, overflow, account changes, and touch behavior on hardware

Acceptance criteria:

```text
Verified live quotas display clearly with no overlap or demo fallback.
OpenCode Go's monthly window remains accessible and actual periods are labelled correctly.
Adding another supported account requires configuration, not new UI code.
```

### Milestone 12: Alert polish

Status: **done (2026-10-20).**

The daemon now maps a quota percentage onto the status vocabulary, so every
provider behaves the same way: a warning at 80% of a window (yellow), a
critical state at 95% (red), and rate limited at 100% as before. The
thresholds are set in the admin page's Alerts section (applies to all
services) and travel to the device with the payload. Only a fresh,
available window alerts: a stale, unavailable or already-reset window keeps
today's stale presentation, and a window that resets within half an hour
does not alert either.

The device tracks every service on its own, so a second service turning bad
while another is already critical still speaks: one chirp for a new warning,
two for a critical, rate-limited or offline service, never repeated while it
lasts, silent on recovery and silent for the first reading after boot. The
LED shows the worst service, not only the open card. Quiet hours are judged
by the daemon's own clock and travel in the payload, so the device needs no
time zone; in quiet hours the device is silent and its light is dimmed.
Home can scroll to a service that has just got worse (presto/popup.py): the
alarm must hold for 30 seconds, a service moves the list at most once in ten
minutes, the list never moves while the user is touching it or has scrolled
recently, a new alarm must be worse than the one already shown, and the
user's own position comes back a minute later unless they have scrolled
since.

Goal:

Make the device feel like a physical status meter.

Tasks:

- define alert severity mapping
- add configurable thresholds for verified subscription quota windows
- implement LED animation modes
- implement quiet sound alerts
- implement mute/long-press behavior
- document config options

Acceptance criteria:

```text
Warnings and critical states are obvious from across the desk but not annoying.
Quota reset/recovery and unavailable readings do not create misleading alerts.
```

### Milestone 13: Demo release

Goal:

Make it usable by another Presto developer.

Tasks:

- write setup guide
- add screenshots/photos
- add sample config
- document limitations
- document future sources
- document supported subscription reporting paths, account setup, and blockers
- add simple troubleshooting section

Acceptance criteria:

```text
A Presto developer can clone the repo, run fake mode, and understand how to enable Ollama or LiteLLM.
They can configure supported subscription accounts and understand reporting limitations.
```

## 17. Future roadmap

### Deployment roadmap

- **v0:** direct Python/pip/pipx daemon; current sources are fake, Ollama, and
  LiteLLM. M6–M11 prioritize verified Codex, Claude, and OpenCode Go subscription
  monitoring before M12 alert polish and M13 release.
- **v0.1:** optional Docker image for server/headless use.
- **v0.2:** optional separate desktop tray runner.
- **v1:** small binaries/installers if the project proves useful.

### Future Features: optional tray

The tray is a runner/wrapper, never part of the daemon core or a second full
UI. Windows/macOS are the primary targets; Linux is best-effort because desktop
environments differ. `pystray` is the likely candidate; `rumps` is optional
later macOS polish, not a requirement. Future commands are
`presto-ai-dash-daemon` and `presto-ai-dash-tray`.

Keep the menu tiny: status, open dashboard/status endpoint, open config folder,
copy Presto endpoint URL, restart daemon, stop daemon, and about.

### Local source configuration checkpoint (after M5)

Approved and implemented separately from alert polish: a local web page adds,
edits, disables, and deletes reusable source instances. Provider keys are
authenticated ciphertext in SQLite, with the master key outside the database.
The first admin start imports TOML source settings once; subsequent changes
apply without restarting and preserve instance history. Separate admin and
device tokens keep editing off the device-facing API. No direct cloud usage
connectors or application attribution are implied by this checkpoint.

See [setup, encryption, and recovery](configuration.md).

### Application usage monitoring (separate future checkpoint)

A separate future checkpoint will monitor multiple configured applications,
such as a book app, regardless of their provider/model. Stable application IDs,
configurable attribution, normalized metrics, and a shared scrolling summary
and detail renderer make new apps a configuration change rather than new code.

Start with dedicated LiteLLM virtual keys per application and optional operation
labels. A later direct-call reporting adapter uses the same application identity
and metrics. App usage is a filtered view of gateway usage, so overlapping totals
and attribution must not count requests twice. Optional daily/weekly app budgets
are distinct from provider quotas; full weekly coverage needs upstream history
or daily aggregates beyond M3's 72-hour snapshots.

See [the application monitoring design](future.md#application-usage-monitoring)
for boundaries and multi-app acceptance checks. This is planned work, not an
implemented integration, and does not change M3 or the subscription milestones.
Billing reconciliation and invoice generation remain outside the project.

### Other future features (unscheduled)

Potential additions:

```text
desktop Ollama collector
basic local Ollama shim
better trend indicators
manual refresh action
configurable polling interval
simple web preview of the same status JSON
```

### Future providers and messaging (unscheduled)

Potential additions:

```text
OpenCode application/session telemetry
Ollama Cloud monthly allowance/balance reporting beyond the verified activity API
direct OpenAI / Anthropic API billing sources
other ChatGPT subscription limit research
MQTT output/input
multiple Presto devices
```

### Future expansion (unscheduled)

Potential additions:

```text
external source package interface
provider icon support
desktop browser opener helper
Prometheus export
advanced alert rules
multi-host collector model
```

## 18. Main risks

### Risk: scope creep

Mitigation:

Keep the delivered local/gateway foundation and the next subscription phase
focused on small quota summaries. API billing, deep analytics, and application
attribution remain separate future work.

### Risk: provider APIs are inconsistent

Mitigation:

Gate each subscription adapter on verified reporting and account coverage.
Normalize actual windows and freshness rather than inferring allowances from
token counts or spend. Record unavailable paths as blockers.

### Risk: direct Ollama usage is hard to measure

Mitigation:

Document the limitation. Add optional collector/shim later.

### Risk: Presto MicroPython UI gets fiddly

Mitigation:

Start with static fake data before building daemon complexity.

### Risk: legal/branding issues

Mitigation:

Use generic icons and original code only. Do not ship provider logos.

## 19. Recommended next build sequence

M0–M5 established the static demo, daemon, storage, and local/gateway sources.
Keep their completed milestone scope unchanged. M7, M10, and the activity part
of M10a are implemented. M11 is deployed with automated device checks and user
visual/touch confirmation complete. M12 is implemented: the daemon maps a
quota percentage onto the status vocabulary, the device tracks every service
on its own, and the alert settings live in the admin page. The remaining
sequence is:

```text
M8. OpenAI/Codex subscription source
M9. Claude subscription source
M10a. Ollama Cloud monthly allowance/balance, if a supported path is verified
M13. Documentation and demo release
```

This ordering is a plan, not approval to implement after a requested readiness
review. Unverified reporting paths remain blocked; MQTT and external plugins
remain outside this phase.

## 20. Definition of done for v0

v0 is done when:

- Presto connects to Wi-Fi
- Presto polls the daemon
- Presto shows 3 swipeable cards
- daemon has fake, Ollama, and LiteLLM sources
- daemon stores recent snapshots in SQLite
- verified Codex, Claude, and OpenCode Go allowances are displayed for supported accounts
- multiple accounts share reusable quota windows and display components
- actual period labels, reset times, and stale/unavailable states are clear
- LEDs reflect status
- sound can be enabled, disabled, and muted
- README explains setup and limitations
- no provider secrets are stored on the Presto
- the project is small enough for a Presto developer to understand

These subscription criteria are planned targets, not claims about current
delivery. An unresolved reporting blocker must be documented and reviewed;
it cannot be counted as a completed subscription integration.
