# Presto AI Dash

A tiny AI usage dashboard for Pimoroni Presto with support for local and cloud AI providers.

Repository: `presto-ai-dash`. The daemon supports fake, local Ollama, LiteLLM,
OpenCode Go and Codex subscription quotas, and Ollama Cloud activity. Manage accounts,
keys, and refresh settings in the local UI; they are saved in SQLite, with keys
encrypted. Codex uses its existing provider-managed ChatGPT login; see
[setup and remaining login work](docs/codex.md). Claude uses a UI-configured
[Claude Code terminal exporter](docs/claude.md), verified with live 5h/Weekly readings.
See [cloud readings and limitations](docs/quotas.md).

It answers a handful of questions at a glance: is local Ollama active, is
LiteLLM healthy, how many tokens have I used today, is anything near a limit,
is anything offline or broken, how fast is my local model running.

The priority is now subscription usage against provider time-window limits,
with reset times and multiple accounts. [The roadmap](docs/milestones.md)
puts reporting readiness, a reusable quota model, named subscription adapters,
and a live scrolling Home ahead of alert polish and release. The live stacked bars
use real labels such as `5h`, `Weekly`, and `Monthly`; a five-hour allowance
is not a daily quota. See [the subscription plan](docs/subscription-monitoring.md).

It is **not** an analytics platform, not a general AI observability tool, and
not a dashboard replacement.

---

## Runtime Architecture

The complete system does not run on the Presto. The static M1 demo can run
entirely on the device; the connected runtime has two parts:

```text
Pimoroni Presto
    | polls HTTP every ~15s
    v
Presto AI Dash daemon  (Python, on your LAN)
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
- Direct OpenAI/Anthropic API billing connectors
- Provider logo packs, plugin package system, provider marketplace
- Billing reconciliation/invoices, advanced analytics, alert rule engine
- Cloud-hosted dashboard, user accounts, desktop browser opener

See [Future Features](docs/future.md) for where those live. The optional tray,
Docker image, and binary installers are also outside v0.
OpenAI/Codex, Claude, OpenCode Go, and Ollama Cloud monitoring is in the active
plan, gated by supported reporting paths. LiteLLM token/spend metrics do not
establish real subscription allowance usage; only the quota sources do.

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

**Current delivery: M0–M5, the configuration UI, connected Presto, the reusable
quota model (M7), OpenCode Go (M10), and the grouped quota Home (M11) are done.**
Codex (M8) reports live with an existing login, and Claude (M9) reports through a
UI-managed terminal exporter; both have acceptance work left. Alert polish (M12)
and the demo release (M13) are not started. See [milestones](docs/milestones.md).

The daemon uses fake data by default; add sources in the
[local configuration page](docs/configuration.md), which supports multiple
accounts per connector and stores keys encrypted in SQLite. Enable it with
`--admin`; its separate listener uses localhost and an admin token. Browser login
is remembered for seven days; **Lock configuration** revokes that session.

Seven connector types (`fake`, `ollama`, `litellm`, `opencode_go`, `ollama_cloud`,
`codex`, and `claude`, the last two provider-managed subscription sources) feed one
daemon. It serves `GET /api/v1/status` behind a shared bearer token. The Presto
(with `STATIC_DEMO = False`) polls it, shows a Home with grouped stacked quota bars
using real period labels (`5h`, `Weekly`, `Monthly`), plus one swipe card per
enabled account. Drag up/down to scroll Home; swipe left/right to switch cards.
Failed refreshes keep the last good cards marked STALE until `OFFLINE_AFTER_S`.
Snapshots are kept in SQLite for 72 hours; history stays internal with no trends.
The static demo mode still uses fixed values labelled **DEMO**.

## Quick start

In VS Code, select **Daemon + admin page** in **Run and Debug**: **F5**
debugs and **Ctrl+F5** runs it. Open `http://127.0.0.1:8788` and use the admin
token printed in the terminal. See [IDE setup](docs/setup.md#vs-code-run-and-debug)
for the root `.venv` and private `dash.toml` used by this profile.

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
    quotas.md                   quota model and API contract
    codex.md  claude.md         subscription source setup and status
    subscription-monitoring.md  subscription plan and acceptance
  daemon/
    pyproject.toml
    src/presto_ai_dash/        Python daemon with encrypted configuration
  presto/
    main.py                     MicroPython app (only file touching hardware)
    config.example.py
    home.py  net.py  cards.py  gestures.py  alerts.py  ui.py
    launcher/ai_dash.py          entry for the firmware carousel
  tools/presto_preview/         host-side screen preview (PNG + viewer)
  examples/
    sample-status.json          the API shape, as static data
    config.example.toml
```

## Documentation map

| Document | Read it when |
| --- | --- |
| `docs/architecture.md` | you want to know how the parts fit together |
| `docs/setup.md` | you are installing or running it |
| `docs/configuration.md` | you want to manage source instances and encrypted keys |
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

## Previewing the Presto screen

```bash
python tools/presto_preview/live.py          # live dash: http://127.0.0.1:8790/
python tools/presto_preview/live.py --demo   # STATIC_DEMO Home, no network
python tools/presto_preview/render.py        # one PNG per card -> .preview/
```

`live.py` runs the real `presto/main.py` App loop on the host, polling the
daemon from `presto/config.py`. Only the hardware is faked: an emulated
240x240 RGB565 PicoGraphics framebuffer using the firmware's bitmap6/bitmap8
fonts (shown at the panel's 480x480), the mouse as the touch panel, and
always-connected Wi-Fi. Drag to swipe or scroll, click to tap, hold to mute;
arrow keys, Enter and M do the same. "Simulate network failure" exercises the
real stale/offline/recovery timers. Saving any `presto/*.py` restarts the app
with fresh code; exceptions appear over the screen.

For agents: `GET /frame.png?scale=2` is the current screen, `GET /state` the
frame number and LED colour, and in Playwright `await presto.gesture("left")`
(also `right`, `up`, `down`, `tap`, `long`). `render.py` writes every card
without a browser (`--payload live|sample|demo|file.json`, `--stale`,
`--offline`, `--muted`).

Edit the app only in `presto/`: PSL runs these source files, and the **Sync
Presto** VS Code task installs the same files on the physical device. **Check
Presto sync** compares actual device file hashes. PSL shows its source revision
and the dated result of the last USB check; saving app code after a successful
check marks the device as needing sync. See [the sync workflow](docs/setup.md#keeping-psl-and-the-device-in-sync).

Not emulated: sound, LED hardware beyond a glow hint, memory, CPU speed and
Wi-Fi joining. Non-ASCII text draws blank (the device remaps some accented
characters).

## Licence

MIT. See `LICENSE`.

No provider logos, mascots, fonts or artwork are shipped in this repository.
