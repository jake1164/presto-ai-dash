# Presto AI Dash

A tiny AI usage dashboard for Pimoroni Presto with support for local and cloud AI providers.

Repository: `presto-ai-dash`. Local integrations are planned for v0; direct
cloud-provider sources remain future work. The current build uses fake data.

It answers a handful of questions at a glance: is local Ollama active, is
LiteLLM healthy, how many tokens have I used today, is anything near a limit,
is anything offline or broken, how fast is my local model running.

It is **not** an analytics platform, not a general AI observability tool, and
not a dashboard replacement.

---

## Runtime Architecture

The complete system does not run on the Presto. The static M1 demo can run
entirely on the device; the planned connected runtime has two parts:

```text
Pimoroni Presto
    | polls HTTP every ~15s
    v
Presto AI Dash daemon  (Python, stdlib only, on your LAN)
    | reads
    v
LiteLLM  --->  Ollama
```

- The **Presto** is the physical dashboard/controller: touchscreen card
  rendering, swipe/tap navigation, LEDs, optional sound, and polling the local
  HTTP endpoint over Wi-Fi.
- The **daemon** exists to make the Presto display simple, safe and useful. It
  collects from sources, normalises them, keeps a short SQLite history, and
  serves one JSON endpoint.
- The Presto holds **no provider secrets** and makes **no provider API calls**.

## Scope / Non-goals

Out of scope for v0, and deliberately so:

- MQTT, multi-daemon federation, Prometheus/Grafana export
- OpenAI / Codex / Claude / Ollama Cloud / OpenCode direct usage sources
- Provider logo packs, plugin package system, provider marketplace
- Billing reconciliation/invoices, advanced analytics, alert rule engine
- Cloud-hosted dashboard, user accounts, desktop browser opener

See [Future Features](docs/future.md) for where those live. The optional tray,
Docker image, and binary installers are also outside v0.

[Application usage monitoring](docs/future.md#application-usage-monitoring) is
planned as a separate checkpoint after M5: multiple configured apps, reusable
views, LiteLLM attribution first, and optional daily/weekly app budgets. A book
app is one example; the design is independent of any single app or provider.

## Topology and Telemetry Limits

```text
toontown-int-srv1 -> LiteLLM -> jake-desktop -> Ollama
jake-desktop -> Ollama directly
```

For v0, run the daemon on `toontown-int-srv1` and have the Presto poll it over
HTTP. LiteLLM sees only traffic routed through it. Direct desktop Ollama usage
requires a future collector, shim, wrapper, or voluntary metrics reporting;
health polling cannot reconstruct token history.

## Deployment Model

The core is a normal, headless Python 3.11+ app targeting Windows 10/11,
macOS, and Linux, with SQLite, TOML, and portable `pathlib` paths. No systemd
or Windows service is required. Cross-platform support is the target; current
execution checks have been on Windows.

| Release target | Deployment direction |
| --- | --- |
| v0 | Direct Python or pip/pipx installation |
| v0.1 | Optional Docker image, especially for `toontown-int-srv1` |
| v0.2 | Optional separate desktop tray runner |
| v1 | Small binaries/installers if the project proves useful |

The planned command is `presto-ai-dash-daemon`; the **current** installed
command is `presto-ai-dash` (or `python -m presto_ai_dash.app`). The future
`presto-ai-dash-tray` wrapper must leave headless use first-class. Docker,
tray support, pipx validation, and binary packaging are not implemented yet.

## v0 in one paragraph

**Current milestone: M2 is implemented and tested with the fake source only.**
The Presto Home preview stacks daily and weekly quota bars for OpenAI,
Claude, Ollama Cloud, and OpenCode.ai using fixed values labelled **DEMO**,
alongside the
static Ollama and LiteLLM cards. These cloud names do not indicate integrations.
Drag up/down to scroll the Home list; swipe left/right to switch cards.
SQLite supports snapshot insert/read;
retention, trends, alert history, and real sources remain future milestones.
The paragraph below describes the planned v0.

Three sources (`fake`, `ollama`, `litellm`) feed one daemon. The daemon serves
`GET /api/v1/status` behind a shared bearer token. The Presto polls it, shows
three cards (AI Dash, Ollama, LiteLLM), colours its LEDs by status, and can
chirp on a warning. Snapshots are kept in SQLite for 72 hours so simple
up/down trends can be shown. Nothing else.

## Quick start

```bash
# Daemon
cd daemon
python -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -e .
cp ../examples/config.example.toml dash.toml
python -m presto_ai_dash.app --config dash.toml

# Presto
# Install the static demo alongside the Presto's existing carousel launcher.
# See docs/setup.md for the source-to-device file mapping; no Wi-Fi is needed.
```

Full instructions: [`docs/setup.md`](docs/setup.md).

## Repository layout

```text
presto-ai-dash/
  README.md
  LICENSE
  docs/
    architecture.md             how the pieces fit
    setup.md                    install and run
    sources.md                  what each source measures, and how
    limitations.md              what is honestly not measurable
    future.md                   what is deliberately deferred
    implementation-readiness.md the pre-build readiness pass
  daemon/
    pyproject.toml
    src/presto_ai_dash/        Python daemon, stdlib only
  presto/
    main.py                     MicroPython app (only file touching hardware)
    config.example.py
    home.py  net.py  cards.py  gestures.py  alerts.py  ui.py
    launcher/ai_dash.py          entry for the firmware carousel
  examples/
    sample-status.json          the API shape, as static data
    config.example.toml
```

## Documentation map

| Document | Read it when |
| --- | --- |
| `docs/architecture.md` | you want to know how the parts fit together |
| `docs/setup.md` | you are installing or running it |
| `docs/sources.md` | you are adding or debugging a source |
| `docs/limitations.md` | a number looks wrong or missing |
| `docs/future.md` | you are wondering why feature X is absent |
| `docs/milestones.md` | you want to know what is built and what is next |
| `docs/implementation-readiness.md` | you want the verified external contracts and the decisions taken |

## Tests

```bash
cd daemon
python -m unittest discover -s tests -v
```

The daemon suite is pure CPython and uses localhost HTTP only. The Presto logic suite
runs on the host too; only the thin hardware layer in `presto/main.py` needs a
real device. See `docs/limitations.md` for what cannot be automated.

## Licence

MIT. See `LICENSE`.

No provider logos, mascots, fonts or artwork are shipped in this repository.
