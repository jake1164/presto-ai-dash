# Implementation Readiness

Status of the readiness pass against `presto_ai_pulse_project_plan.md`.
Written before Milestone 0 code. Records what was verified, what was decided,
and what is genuinely blocked.

**Bottom line: nothing blocks Milestones 0–2.** All three external contracts
(Presto MicroPython, LiteLLM, Ollama) were verified against current official
sources. Two plan-internal contradictions were found and resolved below.

---

## 1. Decisions genuinely required before Milestone 0/1

**None.** Milestone 0 (repo shell, docs, config format, sample JSON) and
Milestone 1 (static Presto UI on fake data) are fully determined by the plan.
Every choice below is an internal technical decision, not a product decision,
and each is recorded here with its rationale so it can be challenged.

| Decision | Choice | Why |
| --- | --- | --- |
| Daemon config format | TOML (`daemon/config.example.toml`) | Plan sketches `examples/fake-config.yaml`. TOML is parsed by stdlib `tomllib` (Python 3.11+), so the daemon has **zero runtime dependencies**. PyYAML would add one. The Presto app keeps `config.example.py` (plain constants) exactly as the plan specifies, because MicroPython has no TOML/YAML parser. |
| HTTP framework | stdlib `http.server.ThreadingHTTPServer` | Zero dependencies. The API is one GET endpoint. FastAPI/Flask would be scope creep against "small enough for a Presto developer to understand". |
| Test framework | stdlib `unittest` | Zero dev dependencies. `python -m unittest` works everywhere. |
| Python floor | 3.11 | Required for `tomllib`. |
| Presto resolution | `Presto()` default (240×240 framebuffer) | Verified: `Presto()` defaults to `full_res=False`, giving a 240×240 PicoGraphics surface on the 480×480 panel. Faster, and ample for a status card. `full_res=True` remains available. |
| Status vocabulary | canonical `ok`, alias `normal` | See contradiction C1. |
| Card ids / order | `pulse`, `ollama`, `litellm` | Matches the plan's example response and §12 screen order. |
| Daemon scheduling | one background thread per source | Simplest thing that satisfies "source collection". No cron, no async framework. |
| Shared token when unset | generate at startup, log once, warn | Fail-closed on security (token always required) without blocking a first run. |

---

## 2. Safe to decide during implementation vs. must-verify external contracts

### Safe to decide during implementation (internal, no external dependency)

- Module layout inside `daemon/src/presto_ai_pulse/`
- SQLite column types and indices
- Log format
- Exact trend comparison window and "insufficient data" wording
- Fake-source scenario rotation order and cadence
- Alert severity thresholds and their config keys
- JSON field naming inside `metrics` / `detail` (the plan fixes only the top-level shape)

### External contracts — verified against current official sources

#### Pimoroni Presto (MicroPython)

Verified from `pimoroni/presto` `main` (`modules/py_frozen/presto.py`,
`modules/py_frozen/touch.py`, `docs/presto.md`, `examples/*`) and the product page.

| Fact | Value |
| --- | --- |
| Panel | 4" square **480×480** IPS, capacitive touch |
| Default framebuffer | **240×240** (`full_res=False`); 480×480 with `full_res=True` |
| RGB LEDs | **7** × SK6812, `Presto.NUM_LEDS = 7`, data pin GPIO 33 |
| Piezo | `Buzzer(43)`; `set_tone(freq)`; freq < 50 switches output off |
| User button | boot button, GPIO 46 (`Pin(46, Pin.IN)`) |
| Wi-Fi | RM2 / CYW43439; `presto.connect()` reads `secrets.py` (`WIFI_SSID`, `WIFI_PASSWORD`) |
| Display API | `presto.display` (PicoGraphics), `presto.update()`, `presto.partial_update(x,y,w,h)` |
| Touch API | `presto.touch.poll()`, `presto.touch.state`, `.x`, `.y`; `presto.touch_a`, `touch_b`, `touch_delta`, `touch_poll()` |
| Touch buttons | `from touch import Button`; `Button(x,y,w,h)`, `.is_pressed()`, `.x/.y/.w`, `.bounds` |
| LEDs | `presto.set_led_rgb(i,r,g,b)`, `presto.set_led_hsv(i,h,s,v)`, `presto.auto_ambient_leds(bool)` |
| HTTP | `import requests`; `requests.get(url, headers=..., timeout=...)`; `resp.status_code`, `.json()`, `.text`, `.close()` |

Two consequences that shaped the design:

1. **`requests` supports custom headers.** Verified in micropython-lib
   `python-ecosys/requests/requests/__init__.py`: `request()` takes `headers=`
   and writes each `k: v` pair onto the wire. The Presto can therefore send
   `Authorization: Bearer <token>` as the plan requires. No mock was needed.
2. **`Button.is_pressed()` is level-sensitive, not edge-sensitive.** `touch.py`
   hit-tests the *current* touch position against each registered `Button` on
   every `poll()`. There is no built-in tap/swipe/long-press detection. The
   gesture state machine in `presto/gestures.py` is therefore ours to write, and
   it is written as a pure function of `(state, x, y, timestamp)` so it is
   host-testable without hardware.

#### LiteLLM proxy

Verified from `docs.litellm.ai/docs/proxy/health`,
`docs/proxy/metrics`, `docs/api_stability_policy`.

| Endpoint | Auth | Contract |
| --- | --- | --- |
| `GET /health/liveliness` (alias `/health/liveness`) | none | 200 `"I'm alive!"`, or 503 `{"status":"shutting_down"}` |
| `GET /health/readiness` | none | 200 `{"status":"healthy","db":...}`; 503 when DB unreachable. `db` ∈ `connected` / `disconnected` / `Not connected` |
| `GET /user/daily/activity?start_date=&end_date=` | Bearer key | `results[].metrics.{spend,prompt_tokens,completion_tokens,total_tokens,api_requests,successful_requests,failed_requests}`; `results[].breakdown.models`; `metadata.total_*` |
| `GET /health` | valid key | **Runs a real test request per model and costs tokens.** Not used by default. |

Constraints that shaped the design:

- **`GET /daily_metrics` was removed.** Daily spend/usage is now
  `GET /user/daily/activity`. Anything still calling the old path is stale.
- **Only OpenAPI-spec endpoints are a public contract.** LiteLLM's stability
  policy states everything else is private and may change without notice. The
  LiteLLM source is therefore written **fail-soft**: an unexpected status code,
  a missing field, or a 404 degrades that metric to `unavailable` and never
  raises. This is also what the plan's "honest telemetry" principle demands.
- **Spend tracking requires a database.** Without `DATABASE_URL`/Postgres,
  `spend` is not merely zero — it is *unavailable*. The card must say
  "unavailable", never "$0.00".

#### Ollama

Verified from `ollama/ollama` `docs/api.md`.

| Endpoint | Contract |
| --- | --- |
| `GET /api/version` | `{"version": "0.5.1"}` |
| `GET /api/tags` | `{"models":[{name, model, modified_at, size, digest, details{...}}]}` |
| `GET /api/ps` | `{"models":[{name, model, size, digest, details, expires_at, size_vram}]}` — currently loaded |
| `POST /api/generate` `{"model","prompt","stream":false}` | `total_duration`, `load_duration`, `prompt_eval_count`, `prompt_eval_duration`, `eval_count`, `eval_duration` — **all in nanoseconds** |

- tokens/sec = `eval_count / eval_duration * 1e9` (stated verbatim in the docs).
- **There is no usage-history endpoint.** This confirms the plan's §6.3
  limitation rather than contradicting it.

---

## 3. Smallest concrete architecture

```text
daemon/                          Python 3.11+, stdlib only, zero runtime deps
  pyproject.toml
  src/presto_ai_pulse/
    __init__.py
    config.py                    TOML load, defaults, env overrides, validation
    models.py                    Status, SourceSnapshot, Card, Alert
    http_client.py               urllib GET/POST + JSON, hard timeout
    sources/
      __init__.py                Source base + registry
      fake.py                    deterministic scenario cycler
      ollama.py                  /api/version, /api/tags, /api/ps, samples file
      litellm.py                 /health/*, /user/daily/activity (fail-soft)
    storage.py                   SQLite: snapshots, alerts, settings + retention
    alerts.py                    severity mapping, thresholds, trend deltas
    dashboard.py                 snapshots -> /api/v1/status payload
    api.py                       ThreadingHTTPServer, bearer auth, routes
    app.py                       wiring + collector threads + main loop
  tests/                         unittest, recorded fixtures from official docs

presto/                          MicroPython, no top-level hardware imports
  main.py                        the only file that imports presto/machine
  config.example.py              plain constants (no parser needed on device)
  net.py                         requests wrapper, injectable for tests
  cards.py                       card model + status->accent mapping (pure)
  gestures.py                    tap/swipe/long-press state machine (pure)
  alerts.py                      LED colour + sound cue mapping (pure)
  ui.py                          PicoGraphics drawing (hardware)

examples/
  sample-status.json             Milestone 1 static data
  config.example.toml            daemon config
```

Testability rule that makes the split work: **only `main.py` imports
`presto`/`machine`.** Everything else is importable by CPython, so the pure
logic is unit-tested on the host and the hardware-touching code stays thin.

---

## 4. Test strategy

### Automatable — daemon (pure CPython)

| Area | What is tested |
| --- | --- |
| `config` | defaults, TOML load, env override, validation errors, token generation |
| `models` | status normalisation/aliases, severity ordering, overall rollup |
| `sources/fake` | deterministic cycling, all seven states, no randomness |
| `sources/ollama` | parsing of recorded `/api/version`, `/api/tags`, `/api/ps`, `/api/generate` fixtures; tok/s arithmetic; unreachable → `offline`; missing fields → `unavailable` |
| `sources/litellm` | parsing of recorded `/health/*` and `/user/daily/activity` fixtures; 404/401/503 → `unavailable`; missing DB → spend `unavailable`, not `0` |
| `storage` | insert, query latest, retention cleanup, `settings` round-trip |
| `alerts` | severity mapping, threshold boundaries, trend up/down/flat/unknown |
| `dashboard` | payload shape, card order, `overall_status` rollup |
| `api` | bearer accept/reject/malformed, `/api/v1/status` shape, 404, bad JSON |
| end-to-end | real daemon + fake source over real HTTP on localhost |

### Automatable — Presto logic (host-side, no hardware)

| Area | What is tested |
| --- | --- |
| `gestures` | tap, swipe left/right, long-press, release, multi-touch rejection, debounce |
| `cards` | status → accent mapping, card ordering, detail-line formatting |
| `alerts` | status → LED RGB, sound cue selection, mute behaviour |
| `net` | URL building, header construction, timeout, non-JSON body, HTTP error status |
| syntax | every `presto/*.py` compiles under CPython |
| layering | no file except `main.py` imports `presto` or `machine` |

### Not automatable — requires physical Presto hardware

| Area | Why |
| --- | --- |
| Actual pixels on the 480×480 IPS panel | Needs the ST7701 driver and PicoGraphics |
| Capacitive touch gesture recognition | Needs the FT6236 controller; note the documented caveat that two horizontally-aligned touches do not register correctly |
| 7 × SK6812 LED colour and brightness | Needs the LED strip on GPIO 33 |
| Piezo sound output | Needs the buzzer on GPIO 43 |
| Wi-Fi association via RM2/CYW43439 | Needs the radio and a real AP |
| Backlight / battery behaviour | Needs the power hardware |
| Physical boot-button press (GPIO 46) | Needs the button |

These are covered by the Milestone 7 manual checklist, not by CI. The
mitigation is structural: everything decidable without hardware lives in pure
modules and is tested; the hardware layer is a thin adapter.

---

## 5. Contradictions and gaps found in the plan

### C1 — Status vocabulary is inconsistent (resolved)

§6.1 lists fake-source states including `normal`; the §9 example response uses
`"status": "ok"` for the same condition.

**Resolution:** canonical set is
`ok | active | warning | critical | offline | rate_limited | near_limit`.
`normal` is accepted as an input alias and normalised to `ok` on the way in, so
the fake source can still be written in the plan's own vocabulary. Recorded in
`docs/sources.md`.

### C2 — "recent errors" and "rate limit warnings" have no clean source (resolved, documented)

§6.2 asks for "recent errors, if available" and "rate limit warnings, if
available" from LiteLLM.

- `failed_requests` from `/user/daily/activity` gives a **count**, not a list of
  recent errors. `GET /spend/logs` would give individual records but is out of
  scope for v0 and is not needed for a glanceable card.
- There is **no verified LiteLLM endpoint** that reports "you are approaching a
  rate limit".

**Resolution:** the LiteLLM card surfaces `failed_requests` as "Errors today".
Rate-limit warnings are **not synthesised** for LiteLLM in v0. The
`rate_limited` and `near_limit` states remain first-class in the status model and
in the fake source (so the UI and LED/sound paths are exercised), and a future
source can emit them. No endpoint was invented to fill the gap.

### G1 — Ollama "last measured tokens/sec" cannot come from Ollama (resolved, documented)

Ollama exposes no request history. §6.3 already says this.

**Resolution:** the Ollama source reports live health (`/api/version`,
`/api/tags`, `/api/ps`) plus, *if* an optional samples file exists, the last
measured request. The samples file is the same "helper writes JSON → daemon
reads JSON" pattern the plan already endorses for OpenCode in §7.4. The tok/s
arithmetic is implemented and tested against a recorded `/api/generate` fixture
now, so it is ready for the v0.2 collector without further design work. Until a
collector exists the card honestly reads "last measured: unavailable".

### G2 — `settings` table is listed but never specified (resolved)

§11 lists a `settings` table with no contents.

**Resolution:** a minimal `key`/`value` table for daemon-owned mutable state
(currently: last retention sweep timestamp). Nothing user-facing.

### G3 — Spend requires a LiteLLM database (documented, not resolvable in code)

If the operator's LiteLLM has no `DATABASE_URL`, spend is permanently
unavailable. The card renders "unavailable" and `docs/limitations.md` explains
why. This is a deployment fact, not a code gap.

### G4 — Trend indicators need history (resolved)

§11 asks for tokens/requests/speed up-down and recently active/inactive. With
one snapshot there is nothing to compare.

**Resolution:** compare the newest snapshot to the oldest within a 24h lookback.
Fewer than two snapshots in the window → `unknown`, rendered as "–". No
charting, per §11.

---

## 6. Verified source references

- Pimoroni Presto product page — panel, LEDs, piezo, RM2, buttons, microSD
- `github.com/pimoroni/presto` `main` — `modules/py_frozen/presto.py`,
  `modules/py_frozen/touch.py`, `docs/presto.md`, `examples/cheerlights_bulb.py`,
  `examples/touch_buttons.py`, `examples/buzzer.py`, `examples/secrets.py`
- `github.com/micropython/micropython-lib` `master` —
  `python-ecosys/requests/requests/__init__.py` (header support)
- `docs.litellm.ai/docs/proxy/health`, `/docs/proxy/metrics`,
  `/docs/api_stability_policy`
- `github.com/ollama/ollama` `main` — `docs/api.md`
