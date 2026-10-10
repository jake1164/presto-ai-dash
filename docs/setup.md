# Setup

## Requirements

| Component | Requirement |
| --- | --- |
| Daemon | Python 3.11 or newer (for `tomllib`). No third-party packages. |
| Presto | Pimoroni Presto with the current MicroPython firmware and launcher |
| Network | Presto and daemon on the same LAN |

The network requirement applies to the planned connected runtime. The current
static M1 preview needs neither Wi-Fi nor a daemon. The core daemon targets
Windows 10/11, macOS, and Linux; see [Deployment Model](architecture.md#deployment-model)
for the future direct Python/pipx, optional Docker, tray, and binary targets.

## 1. Run the daemon

```bash
cd daemon
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

pip install -e .
```

Create a config file:

```bash
cp ../examples/config.example.toml dash.toml
```

Edit `dash.toml`. At minimum set `daemon.shared_token` to something long and
random. If you leave it empty the daemon generates one at startup and prints it
once to the log.

Run it:

```bash
python -m presto_ai_dash.app --config dash.toml
```

Or, if you installed the console script:

```bash
presto-ai-dash --config dash.toml
```

`presto-ai-dash-daemon` is the planned command name, not yet installed. Use
the current commands above. No Docker image, tray runner, or installers exist
yet; pipx support is a deployment target still to be validated.

Check it works:

```bash
curl -H "Authorization: Bearer your-token-here" \
     http://<daemon-host>:8787/api/v1/status
```

You should get JSON with three cards, all driven by the `fake` source.
Without the bearer token, or with a wrong token, this endpoint returns 401.
Validate config without starting the daemon with `presto-ai-dash --check --config dash.toml`.
Stop the daemon with Ctrl+C.

M2 stores snapshots in SQLite, but does not yet implement retention, history
queries, trends, or alert thresholds. Delete the database while the daemon is
stopped to reset demo history.

### Environment variables

| Variable | Overrides |
| --- | --- |
| `DASH_SHARED_TOKEN` | `daemon.shared_token` |
| `DASH_LOG_LEVEL` | `logging.level` |
| `DASH_HOST` | `daemon.host` |
| `DASH_PORT` | `daemon.port` |
| `DASH_DATABASE` | `daemon.database` |

Prefer these for anything secret so it never lands in a file.

## 2. Flash and configure the Presto

The Presto ships with MicroPython and a launcher preinstalled. If you do not
see the launcher, download the latest `with-filesystem` `.uf2` from the
[Presto releases page](https://github.com/pimoroni/presto/releases/latest),
hold **BOOT** while tapping **RESET**, and drag the `.uf2` onto the `RP2350`
drive.

The first hardware checkpoint uses the existing static fake-data demo. It
needs no Wi-Fi credentials or running daemon.

Keep the device's existing `main.py` carousel launcher. Create `/ai_dash_files` on
the device and copy files using [Thonny](https://thonny.org/) (interpreter:
*MicroPython (Raspberry Pi Pico)*) or `mpremote`:

| Repository file | Device path |
| --- | --- |
| `presto/launcher/ai_dash.py` | `/ai_dash.py` |
| `presto/main.py` | `/ai_dash_files/ai_dash_app.py` |
| `presto/config.example.py` | `/ai_dash_files/config.py` |
| `presto/home.py` | `/ai_dash_files/home.py` |
| `presto/net.py` | `/ai_dash_files/net.py` |
| `presto/cards.py` | `/ai_dash_files/cards.py` |
| `presto/gestures.py` | `/ai_dash_files/gestures.py` |
| `presto/alerts.py` | `/ai_dash_files/alerts.py` |
| `presto/ui.py` | `/ai_dash_files/ui.py` |
| `examples/sample-status.json` | `/ai_dash_files/sample-status.json` |

Only the entry script lives at the root, so helper modules do not appear as
extra carousel apps. The entry script explicitly starts the app when imported
by the firmware launcher.
Use the distinct folder name shown above: MicroPython would import a folder
named `/ai_dash` instead of running the root `/ai_dash.py` entry.

Press **RESET**, find **AI Dash** in the firmware carousel, and tap it. Home
shows OpenAI, Claude, Ollama Cloud, and OpenCode.ai quota bars with a **DEMO** label: these
are illustrative values, not real account usage. Each provider has one combined
meter with two stacked lanes: cyan **daily** on top and purple **weekly** below,
separated by a small gap. Both percentages remain visible, each against its
own period's limit. Colours identify periods rather than severity. Missing
quotas show `--`; over-limit percentages remain numeric while the fill caps
at 100%. There is no period toggle.

Three rows fit at once. Drag up/down on the list to reveal the fourth demo;
the title and legend stay fixed and the scrollbar shows position. Daily and
weekly values for OpenCode.ai are illustrative, not a real usage integration.

Swipe left/right to reach the existing Ollama and LiteLLM static cards.
Home remembers its scroll position when returning from another card.
Long-press should toggle the MUTED indicator. Press **RESET**
to return to the firmware carousel.

Live daemon polling is not wired into the device loop yet. M2 verifies the
daemon API independently on localhost. Keep the daemon settings in `config.py`
for the future polling integration; they are unused by this static demo.

## 3. Future real sources

Ollama and LiteLLM integration are deferred to M4 and M5. M2 rejects config
that enables a real source. The cards named Ollama and LiteLLM use fake demo
metrics only. See `docs/sources.md` for the planned source contracts.

## Troubleshooting

| Symptom | Likely cause |
| --- | --- |
| Presto shows "daemon offline" | Wrong `DAEMON_HOST`/`DAEMON_PORT`, daemon not running, or firewall |
| Presto shows "unauthorised" | `SHARED_TOKEN` does not match `daemon.shared_token` |
| Presto shows "no network" | `secrets.py` SSID/password wrong, or 5GHz-only network (the RM2 module is 2.4GHz) |
| Card says "unavailable" | The source is reachable but that metric is not exposed. See `docs/limitations.md` |
| Card says "offline" | The source itself is unreachable. Check `base_url` |
| Daemon exits at startup | Config parse error, or an invalid value. The log names the key |

## Running the tests

```bash
cd daemon
python -m unittest discover -s tests -v
```

No external services are required. The M2 suite exercises a real HTTP server
on localhost and temporary SQLite databases; Presto tests run on the host.
