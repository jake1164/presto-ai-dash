# Milestone tracker

Status of each milestone in `presto_ai_dash_project_plan.md` §16.

| # | Milestone | Status | Notes |
| --- | --- | --- | --- |
| 0 | Repo skeleton | **done** | Repo shell, MIT licence, README, docs, config format, sample status JSON. |
| 1 | Static Presto demo | **done** | Three cards from static sample data, swipe/tap/long-press, LED colours, offline screen. |
| 2 | Daemon with fake source | not started | Python daemon, `/api/v1/status`, fake source, shared token, logging, config file. |
| 3 | SQLite snapshots | not started | Snapshots, alerts, retention cleanup, last-updated time. |
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

- **Live daemon polling.** That is Milestone 2. Milestone 1 deliberately
  proves the UI on static data first, exactly as the plan's §19 build sequence
  requires.
- **Anything needing physical hardware.** Pixels, touch, LEDs, sound and Wi-Fi
  need a real Presto. See `docs/limitations.md`.

## Test counts

| Suite | Tests |
| --- | --- |
| `test_cards.py` | 34 |
| `test_gestures.py` | 16 |
| `test_alerts.py` | 34 |
| `test_net.py` | 23 |
| `test_presto_structure.py` | 6 |
| **Total** | **113** |

Run with:

```bash
cd daemon
python -m unittest discover -s tests -v
```
