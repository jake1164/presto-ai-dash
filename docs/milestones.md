# Milestone tracker

Status of each milestone in `presto_ai_dash_project_plan.md` §16.

| # | Milestone | Status | Notes |
| --- | --- | --- | --- |
| 0 | Repo skeleton | **done** | Repo shell, MIT licence, README, docs, config format, sample status JSON. |
| 1 | Static Presto demo | **done** | Three cards from static sample data, swipe/tap/long-press, LED colours, offline screen. |
| 2 | Daemon with fake source | **done** | Runnable stdlib daemon, authenticated `/api/v1/status`, fake source, logging, TOML config, minimal snapshot insert/read; 31 M2 tests. |
| 3 | SQLite history and retention | **done** | UTC migration, bounded snapshot/alert history, atomic observations, 72-hour cleanup, safe HTTP shutdown; 15 M3 tests. |
| 4 | Ollama source | **done** | Version, local model inventory and loaded state; optional observed sample and tokens/sec; no fabricated usage history. |
| 5 | LiteLLM source | **done** | Authenticated daily request/token/spend activity, health/readiness, partial-data warnings; fake and live modes are mutually exclusive. |
| — | Local source configuration | **done** | Separate loopback admin page, multiple source instances, encrypted SQLite keys with external master key, live edits; 17 management tests. |
| — | Connected Presto | **done** | Async authenticated polling, live source-instance Home, bounded retry/stale/offline states; 12 live tests, USB deployment, and actual Wi-Fi/auth/recovery checks. |
| 6 | Alert polish | not started | Severity mapping, LED animation, quiet sound, mute/long-press, config docs. |
| 7 | Demo release | not started | Setup guide, screenshots, sample config, limitations, troubleshooting. |

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
  plan's §19 build sequence requires. The later connected checkpoint below
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
- The local daemon serves authenticated fake data at `192.168.1.240:8787`.
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

## Test counts

| Suite | Tests |
| --- | --- |
| `test_cards.py` | 34 |
| `test_gestures.py` | 20 |
| `test_alerts.py` | 34 |
| `test_net.py` | 23 |
| `test_presto_structure.py` | 7 |
| `test_m2.py` | 31 |
| `test_m3.py` | 15 |
| `test_sources.py` | 12 |
| `test_management.py` | 17 |
| `test_live.py` | 12 |
| `test_ui.py` | 4 |
| `test_home.py` | 19 |
| **Total** | **228** |

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
has 228 passing tests.

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
