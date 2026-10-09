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
cp ../examples/config.example.toml pulse.toml
```

Edit `pulse.toml`. At minimum set `daemon.shared_token` to something long and
random. If you leave it empty the daemon generates one at startup and prints it
once to the log.

Run it:

```bash
python -m presto_ai_pulse.app --config pulse.toml
```

Or, if you installed the console script:

```bash
presto-ai-pulse --config pulse.toml
```

Check it works:

```bash
curl -H "Authorization: Bearer your-token-here" \
     http://<daemon-host>:8787/api/v1/status
```

You should get JSON with three cards, all driven by the `fake` source.

### Environment variables

| Variable | Overrides |
| --- | --- |
| `PULSE_SHARED_TOKEN` | `daemon.shared_token` |
| `PULSE_LITELLM_API_KEY` | `sources.litellm.api_key` |
| `PULSE_OLLAMA_BASE_URL` | `sources.ollama.base_url` |
| `PULSE_LITELLM_BASE_URL` | `sources.litellm.base_url` |
| `PULSE_LOG_LEVEL` | `logging.level` |

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

Press **RESET**. The Presto connects to Wi-Fi, polls the daemon, and shows the
three cards.

## 3. Enable a real source

Edit `pulse.toml` and flip `enabled = true` for the source you want, then
restart the daemon. See `docs/sources.md` for what each source needs and what
it can honestly report.

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

No network access is required; every external response is a recorded fixture
taken from official documentation.
