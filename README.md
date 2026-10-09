# Presto AI Pulse

A small physical AI-usage/status display for the **Pimoroni Presto**.

It answers a handful of questions at a glance: is local Ollama active, is
LiteLLM healthy, how many tokens have I used today, is anything near a limit,
is anything offline or broken, how fast is my local model running.

It is **not** an analytics platform, not a general AI observability tool, and
not a dashboard replacement.

---

## What this is

```text
Pimoroni Presto
    | polls HTTP every ~15s
    v
Presto AI Pulse daemon  (Python, stdlib only, on your LAN)
    | reads
    v
LiteLLM  --->  Ollama
```

- The **Presto** is the product. It renders three swipeable cards, drives seven
  RGB LEDs, and can chirp on a warning.
- The **daemon** exists to make the Presto display simple, safe and useful. It
  collects from sources, normalises them, keeps a short SQLite history, and
  serves one JSON endpoint.
- The Presto holds **no provider secrets** and makes **no provider API calls**.

## What this is not

Out of scope for v0, and deliberately so:

- MQTT, multi-daemon federation, Prometheus/Grafana export
- OpenAI / Codex / Claude / Ollama Cloud / OpenCode direct usage sources
- Provider logo packs, plugin package system, provider marketplace
- Per-project billing breakdown, advanced analytics, alert rule engine
- Cloud-hosted dashboard, user accounts, desktop browser opener

See `docs/future.md` for where those live.

## v0 in one paragraph

Three sources (`fake`, `ollama`, `litellm`) feed one daemon. The daemon serves
`GET /api/v1/status` behind a shared bearer token. The Presto polls it, shows
three cards (AI Pulse, Ollama, LiteLLM), colours its LEDs by status, and can
chirp on a warning. Snapshots are kept in SQLite for 72 hours so simple
up/down trends can be shown. Nothing else.

## Quick start

```bash
# Daemon
cd daemon
python -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -e .
cp ../examples/config.example.toml pulse.toml
python -m presto_ai_pulse.app --config pulse.toml

# Presto
# Copy presto/*.py to the Presto, add your Wi-Fi details to secrets.py,
# copy presto/config.example.py to config.py and edit it.
```

Full instructions: [`docs/setup.md`](docs/setup.md).

## Repository layout

```text
presto-ai-pulse/
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
    src/presto_ai_pulse/        Python daemon, stdlib only
  presto/
    main.py                     MicroPython app (only file touching hardware)
    config.example.py
    net.py  cards.py  gestures.py  alerts.py  ui.py
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
| `docs/implementation-readiness.md` | you want the verified external contracts and the decisions taken |

## Tests

```bash
cd daemon
python -m unittest discover -s tests -v
```

The daemon suite is pure CPython and needs no network. The Presto logic suite
runs on the host too; only the thin hardware layer in `presto/main.py` needs a
real device. See `docs/limitations.md` for what cannot be automated.

## Licence

MIT. See `LICENSE`.

No provider logos, mascots, fonts or artwork are shipped in this repository.
