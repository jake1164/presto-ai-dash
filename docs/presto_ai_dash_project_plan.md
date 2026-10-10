# Presto AI Dash - Project Plan

## 1. Project summary

**Presto AI Dash** (`presto-ai-dash`): A tiny AI usage dashboard for Pimoroni Presto with support for local and cloud AI providers.

Local integrations are planned for v0; direct cloud sources are future work.
Current delivery: M1 static device demo and M2 fake-source daemon. The Home
design preview shows labelled, fixed cloud quotas; it is not cloud telemetry.
See [milestones.md](milestones.md) for implementation status.

It shows whether local and routed AI usage is normal, active, expensive, rate-limited, or broken. It is not trying to be a full analytics platform. The first version should be a useful desk demo that proves the Presto can act as a touch-enabled AI companion display.

## 2. Core idea

The Presto should sit on the desk and answer a few simple questions at a glance:

- Is local Ollama active?
- Is LiteLLM healthy?
- Am I using a lot of tokens today?
- Is anything near a limit?
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
   - Cloud provider support can come later.
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

The MVP includes:

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

### Not included in v0

These are future placeholders, not part of the first build:

- MQTT
- multi-daemon federation
- desktop browser opener
- provider logo packs
- external plugin package system
- provider marketplace
- Prometheus/Grafana export
- OpenAI/Codex direct usage support
- Claude direct usage support
- Ollama Cloud support
- OpenCode status support
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

v0 sources remain `fake`, `ollama`, and `litellm`; M2 enables only `fake`.

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

## 7. Future sources

### 7.1 OpenAI / Codex

Future goal:

```text
show official usage/cost/status where supported by official APIs
```

Rules:

- use read-only or restricted keys where available
- do not make paid model calls just to test status unless explicitly enabled
- do not scrape dashboards in v0

### 7.2 Claude

Future goal:

```text
support official organization/admin usage or Claude Code telemetry where cleanly available
```

Rules:

- avoid credential scraping
- avoid reverse-engineering private CLI state
- support only official or user-exported telemetry paths

### 7.3 Ollama Cloud

Future goal:

```text
show cloud usage/quota/status if there is a clean supported access path
```

Rules:

- research before implementation
- do not rely on brittle scraping as a default

### 7.4 OpenCode

Future goal:

```text
show occasional OpenCode status
```

Preferred approach:

```text
OpenCode or helper writes JSON -> daemon reads JSON -> Presto shows card
```

Do not build a deep OpenCode analytics parser in v0.

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

## 11. Storage

Use SQLite.

### Minimal tables

```text
snapshots
alerts
settings
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
cards. This
preview does not add cloud sources or change the v0 API contract; the screens
below describe the planned connected v0 cards.

### Screen 1: AI Dash

Purpose:

- main ambient overview

Shows:

```text
overall status
total tokens today, if available
LiteLLM status
Ollama status
current alert, if any
```

### Screen 2: Ollama

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

### Screen 3: LiteLLM

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
- provider secrets live in daemon config or environment variables
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
        litellm.py               future M5 draft; disabled in M2
        ollama.py                future M4 draft; disabled in M2
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
Readiness is complete; implementation still requires explicit approval.

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

### Milestone 6: Alert polish

Goal:

Make the device feel like a physical status meter.

Tasks:

- define alert severity mapping
- implement LED animation modes
- implement quiet sound alerts
- implement mute/long-press behavior
- document config options

Acceptance criteria:

```text
Warnings and critical states are obvious from across the desk but not annoying.
```

### Milestone 7: Demo release

Goal:

Make it usable by another Presto developer.

Tasks:

- write setup guide
- add screenshots/photos
- add sample config
- document limitations
- document future sources
- add simple troubleshooting section

Acceptance criteria:

```text
A Presto developer can clone the repo, run fake mode, and understand how to enable Ollama or LiteLLM.
```

## 17. Future roadmap

### Deployment roadmap

- **v0:** direct Python/pip/pipx daemon; fake, Ollama, and LiteLLM sources only.
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

### Application usage monitoring (after M5)

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
implemented integration, and does not change M3 or the current v0 milestones.
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
OpenCode JSON source
Ollama Cloud research/source
OpenAI usage source
Codex / Claude sources
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

Keep v0 to fake, Ollama, LiteLLM, 3 cards, LEDs, sound, SQLite.

### Risk: provider APIs are inconsistent

Mitigation:

Start with local and gateway sources. Treat cloud providers as later plugins.

### Risk: direct Ollama usage is hard to measure

Mitigation:

Document the limitation. Add optional collector/shim later.

### Risk: Presto MicroPython UI gets fiddly

Mitigation:

Start with static fake data before building daemon complexity.

### Risk: legal/branding issues

Mitigation:

Use generic icons and original code only. Do not ship provider logos.

## 19. Recommended first build sequence

Build in this order:

```text
1. sample-status.json
2. Presto static UI
3. fake daemon endpoint
4. SQLite snapshots
5. Ollama source
6. LiteLLM source
7. LED/sound polish
8. documentation
```

Do not start with Claude, Codex, OpenAI, Ollama Cloud, OpenCode, MQTT, or external plugins.

## 20. Definition of done for v0

v0 is done when:

- Presto connects to Wi-Fi
- Presto polls the daemon
- Presto shows 3 swipeable cards
- daemon has fake, Ollama, and LiteLLM sources
- daemon stores recent snapshots in SQLite
- LEDs reflect status
- sound can be enabled, disabled, and muted
- README explains setup and limitations
- no provider secrets are stored on the Presto
- the project is small enough for a Presto developer to understand
