# Milestone tracker

Status of each milestone in `presto_ai_dash_project_plan.md` §16.

| # | Milestone | Status | Notes |
| --- | --- | --- | --- |
| 0 | Repo skeleton | **done** | Repo shell, MIT licence, README, docs, config format, sample status JSON. |
| 1 | Static Presto demo | **done** | Three cards from static sample data, swipe/tap/long-press, LED colours, offline screen. |
| 2 | Daemon with fake source | **done** | Runnable stdlib daemon, authenticated `/api/v1/status`, fake source, logging, TOML config, minimal snapshot insert/read; 29 M2 tests. |
| 3 | SQLite history and retention | not started; readiness complete | [Luna handoff](m3-readiness.md): bounded history, alert observations, 72-hour cleanup, UTC migration, safe shutdown. |
| 4 | Ollama source | not started | Reachability, model/status data, optional measured stats, tokens/sec. |
| 5 | LiteLLM source | not started | Request/token/spend data, online/offline, warning state. |
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

- **Live device polling.** The Presto loop still uses static data. The scoped
  M2 delivery verifies the daemon API with fake data on localhost; wiring live
  polling into the device loop remains deferred. Milestone 1 proves the UI on
  static data first, as the plan's §19 build sequence requires.
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

## Test counts

| Suite | Tests |
| --- | --- |
| `test_cards.py` | 34 |
| `test_gestures.py` | 20 |
| `test_alerts.py` | 34 |
| `test_net.py` | 23 |
| `test_presto_structure.py` | 7 |
| `test_m2.py` | 29 |
| `test_ui.py` | 4 |
| `test_home.py` | 19 |
| **Total** | **170** |

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
  in M2. Config cannot enable them. Retention, alert history, thresholds and
  trends remain deferred; M3 is not complete.

Run with:

```bash
cd daemon
python -m unittest discover -s tests -v
```

## Planned checkpoints beyond M2

The [M3 readiness review](m3-readiness.md) is complete against `93f1a46`.
All 170 baseline tests pass. Four gaps were verified: mixed-offset ordering,
import-time fake alert timestamps, fabricated freshness without data, and
active request reads surviving API shutdown. The handoff defines fixes and
acceptance checks. This review does not authorize implementation; M3 remains
not started.

- **Device polling integration:** explicitly authorize and verify Wi-Fi plus
  authenticated daemon fake-data polling before real providers. This is a
  separate checkpoint; M2 does not claim it is complete.
- **M3–M5:** history/retention, then Ollama and LiteLLM integrations. v0 sources
  stay limited to `fake`, `ollama`, and `litellm`.
- **Application monitoring (after M5):** a separate future checkpoint for
  multiple configured apps with stable identities, LiteLLM attribution first,
  reusable summary/detail views, and optional app-defined budgets. Direct-call
  reporting follows later. See [the design and acceptance outline](future.md#application-usage-monitoring).
  No book-app special cases or M3 application schema are required.
- **Deployment targets:** v0 direct Python/pip/pipx; v0.1 optional Docker;
  v0.2 optional separate tray runner; v1 binaries/installers if useful.
  These targets are plans, not delivered features. The planned daemon command
  is `presto-ai-dash-daemon`; today's command is `presto-ai-dash`.
