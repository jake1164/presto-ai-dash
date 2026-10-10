# Setup

## Requirements

| Component | Requirement |
| --- | --- |
| Daemon | Python 3.11 or newer (for `tomllib`). No third-party packages. |
| Presto | Pimoroni Presto with the current MicroPython firmware and launcher |
| Network | Presto and daemon on the same LAN |

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

Copy the app files to the Presto root with
[Thonny](https://thonny.org/) (interpreter: *MicroPython (Raspberry Pi Pico)*):

```text
main.py
net.py
cards.py
gestures.py
alerts.py
ui.py
config.py          <- copy of config.example.py, edited
secrets.py         <- your Wi-Fi details
```

`secrets.py` must contain:

```python
WIFI_SSID = "your-network"
WIFI_PASSWORD = "your-password"
```

Edit `config.py` and set `DAEMON_HOST`, `DAEMON_PORT` and `SHARED_TOKEN` to
match the daemon.

Press **RESET** to run the current M1 static demo. The current `presto/main.py`
loads sample data; live daemon polling is not wired into the device loop yet.
M2 verifies the daemon API independently on localhost. Keep the daemon settings
above for the future device polling integration.

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
