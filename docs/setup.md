# Setup

## Requirements

| Component | Requirement |
| --- | --- |
| Daemon | Python 3.11 or newer; install the project to obtain `cryptography` for encrypted keys. |
| Presto | Pimoroni Presto with the current MicroPython firmware and launcher |
| Network | Presto and daemon on the same LAN |

The network requirement applies to the planned connected runtime. The current
static M1 preview needs neither Wi-Fi nor a daemon. The core daemon targets
Windows 10/11, macOS, and Linux; see [Deployment Model](architecture.md#deployment-model)
for the future direct Python/pipx, optional Docker, tray, and binary targets.

## 1. Run the daemon

### VS Code run and debug

Open the repository root in VS Code. The checked-in **Daemon + admin page**
launch profile uses a virtual environment at the repository root and a private
root `dash.toml`. On this Windows workspace the environment is already installed
and the config has been copied from the example. For a fresh Windows checkout:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .\daemon
if (!(Test-Path dash.toml)) { Copy-Item examples/config.example.toml dash.toml }
```

Install the recommended Microsoft Python and Python Debugger extensions if they
are missing. Open **Run and Debug** (`Ctrl+Shift+D`) and select
**Daemon + admin page**. Press **F5** to debug with breakpoints, or
**Ctrl+F5** to run without debugging. Logs and generated tokens appear in the
integrated terminal. These controls follow the
[VS Code Python debugging workflow](https://code.visualstudio.com/docs/python/debugging).

Open **http://127.0.0.1:8788** and unlock with the **admin** token from the log.
The status endpoint is **http://127.0.0.1:8787/api/v1/status** and requires the
separate shared token. These are the default ports; `dash.toml` can change them.
Stop the daemon with **Ctrl+C** in its terminal. During debugging, place a
breakpoint in `Daemon.start()` or `SourceConfiguration.save()` to inspect startup
or browser configuration changes.

The profile uses the repository root as its working directory, keeping the
default `dash.db` and its protected key file there. Source settings in SQLite
remain authoritative after the first admin start. The local TOML, database,
key file, and other VS Code settings are ignored by Git. See
[configuration.md](configuration.md) for key recovery and account editing.

The equivalent Windows terminal command, from the repository root, is:

```powershell
.\.venv\Scripts\python.exe -m presto_ai_dash.app --config dash.toml --admin
```

Run one daemon at a time: stop a terminal/background daemon before launching
the VS Code profile. The status listener (8787) and admin listener (8788)
belong to that same process; PSL (8790) is a separate preview process that
polls the daemon. Daemon listeners use exclusive port binding on Windows so
a second start fails instead of serving competing responses on the same ports.

For macOS/Linux, create the same root `.venv`, install with
`.venv/bin/python -m pip install -e daemon`, and copy the example to root
`dash.toml` before using the profile.

### Terminal setup

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

M3 stores UTC-normalized snapshots and alert observations in SQLite and removes
records older than the configured retention period (72 hours by default).
History queries are internal; the status endpoint remains unchanged. Delete the
database while the daemon is stopped to reset demo history.

### Environment variables

| Variable | Overrides |
| --- | --- |
| `DASH_SHARED_TOKEN` | `daemon.shared_token` |
| `DASH_LOG_LEVEL` | `logging.level` |
| `DASH_HOST` | `daemon.host` |
| `DASH_PORT` | `daemon.port` |
| `DASH_DATABASE` | `daemon.database` |
| `DASH_INGEST_ENABLED` | `daemon.ingest_enabled`, the stored remote-reports toggle |

Prefer these for anything secret so it never lands in a file.

`DASH_INGEST_ENABLED` accepts `1/true/yes/on` and `0/false/no/off`
(case-insensitive) and overrides both the stored toggle and the TOML value;
it locks the admin page's remote-reports switch read-only. An invalid value
fails closed: the daemon logs the error and keeps the route off.

## 2. Flash and configure the Presto

### Keeping PSL and the device in sync

There is one app implementation in `presto/`. PSL imports its real App, cards,
Home, networking, gestures, alerts, and drawing code. Only the display, touch,
LED and Wi-Fi hardware interfaces are emulated. The physical device runs copies
of those same files; it cannot see edits on your PC until you sync them.

For an already configured Presto, use **Terminal → Run Task → Sync Presto** in
VS Code and enter its USB port (currently COM3). This is an explicit action;
saving a file never writes to hardware. **Check Presto sync** checks the actual
installed hashes without uploading code. Both commands interrupt the device
app and relaunch AI Dash, resetting its navigation/mute state. Close Thonny or
other serial tools first. The sync tool refuses automatic board selection and
checks that the selected board identifies as a Pimoroni Presto.

The equivalent Windows commands from the repository root are:

```powershell
.venv/Scripts/python tools/presto_device.py plan
.venv/Scripts/python tools/presto_device.py check --port COM3
.venv/Scripts/python tools/presto_device.py sync --port COM3
```

`plan` does not connect to hardware. Off Windows, use `.venv/bin/python` and
the device's explicit USB path. To install the official USB tool in a fresh
environment, use `.venv/Scripts/python -m pip install -e "daemon[device]"`.
The commands use [MicroPython's mpremote](https://docs.micropython.org/en/latest/reference/mpremote.html).

The shared inventory in `tools/presto_bundle.py` discovers public app modules,
maps `main.py` to `/ai_dash_files/ai_dash_app.py`, and includes the carousel
entry. Newly added public modules join automatically. Sync freezes a source
snapshot, uploads staged files, verifies every SHA-256, replaces installed
files with rollback on write/rename failures, and verifies the installed bytes.
App helpers removed from the source are detected and retired as non-importable
`.presto-prev` backups on the device, so an old helper cannot silently be used.
PSL executes a frozen snapshot of source bytes and bypasses Python bytecode
caches; its displayed revision identifies those loaded bytes.
Re-running sync repairs an interrupted transfer; after USB/power loss, always
verify or sync again. The firmware `/main.py`, private config/secrets, and
daemon data are excluded. Initial Wi-Fi/daemon setup below remains separate.

PSL's footer and `/state` show the app revision plus the **dated last USB
verification**. This is a check of installed files, not live hardware telemetry.
Changing app source afterward shows “App changed since USB verification”; a
different installed hash shows “Device code differs”. A receipt under ignored
`.preview/` contains only revision/status/time. USB checks do not prove physical
display, Wi-Fi, memory or sound behavior. If PSL was already running when its
host tooling changed, restart the usual VS Code preview/compound once to load
the updated footer. Ordinary `presto/*.py` edits continue to reload automatically.

### First installation

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

### Connect the Presto to the daemon

The app supports both modes. Set `STATIC_DEMO = False` in
`/ai_dash_files/config.py` for authenticated daemon polling. `True` keeps the
static cloud quota design preview.

1. Start the daemon using the VS Code profile or terminal command above.
   Give `daemon.shared_token` a persistent value in private `dash.toml`.
2. Put the daemon machine's **LAN IPv4 address** in `DAEMON_HOST`, its status
   port (default 8787) in `DAEMON_PORT`, and the same device shared token in
   `SHARED_TOKEN`. The admin token/port are for browser configuration.
3. Fill in `WIFI_SSID` and `WIFI_PASSWORD` in private `presto/secrets.py` and
   copy it to **`/secrets.py`** on the device. The firmware uses this root file
   for Wi-Fi; credentials are never sent to the daemon. Use a 2.4 GHz network.
4. Deploy the modules in the table above, using your edited private
   `presto/config.py` in place of the example. Preserve the firmware `/main.py`
   launcher. Press RESET and select AI Dash.

Local `presto/config.py` and `presto/secrets.py` are ignored by Git. Never put
provider API keys on the device. For this Windows workspace, the private
device config points to `192.168.1.240:8787`, and the shared token has been saved
in both local configs. If the computer's address changes, update `DAEMON_HOST`.

From the repository root, with `mpremote` installed in your active Python
environment, copy the private settings with:

```powershell
python -m mpremote connect COM3 fs cp presto/config.py :/ai_dash_files/config.py
python -m mpremote connect COM3 fs cp presto/secrets.py :/secrets.py
```

COM3 is the current device; use `python -m mpremote connect list` to find it on
another computer. The firmware connection API is documented in
[Pimoroni's Presto guide](https://github.com/pimoroni/presto/blob/main/docs/presto.md).

Live Home lists enabled source instances and measured request/token counts or
loaded-model information. Multiple gateways remain separate; unknown usage
shows **Usage unavailable**. Fake daemon data is labelled **FAKE**, while real
instances are labelled **LIVE**. The illustrative daily/weekly cloud bars are
available in static mode only. Swipe left/right for Ollama and LiteLLM; Home
scrolls when enough instances are configured.

Polling runs every `POLL_INTERVAL` seconds (default 15). Wi-Fi and HTTP waits
yield to the touch/render loop. Failed requests retry every `RETRY_INTERVAL`
(default 12); each Wi-Fi attempt is bounded by `WIFI_TIMEOUT` (default 10) and
HTTP by `HTTP_TIMEOUT` (default 8). Hostname lookup can block on MicroPython,
so a LAN IP is preferred. See [MicroPython asyncio](https://docs.micropython.org/en/latest/library/asyncio.html).

After a connection interruption or unusable refresh, the last good display is
marked **STALE - retrying** until its age reaches `OFFLINE_AFTER_S` (default 30),
then shows the relevant error screen. Incomplete transfers count as connection
interruptions. Malformed replies produce a diagnostic reason in the USB console
without logging response contents or tokens. First boot without usable data and
a rejected token show a clear error immediately. Successful polling restores
the selected card automatically;
live mode never substitutes static demo values.

If Windows asks about the daemon's network access, allow your trusted private
LAN so the Presto can reach the status port. The admin page remains loopback-only.

## 3. Enable a real source

For browser-based configuration, start with `--admin` and open
`http://127.0.0.1:8788`. Use the separate admin token from the startup log.
Sources and encrypted keys are then managed in SQLite. See
[configuration.md](configuration.md) for account editing and key recovery.

The example config runs the fake source by default. To use Ollama or LiteLLM,
set `sources.fake.enabled = false`, then set the desired source's `enabled`
to `true` and configure its `base_url`. The config rejects mixing fake and live
sources. LiteLLM can read its key from `LITELLM_API_KEY`; Ollama's optional
`samples_file` only reads observations supplied by an external collector.
Neither source sends prompts. See `docs/sources.md` for measured fields and
limits.

### Remote observation ingest (optional)

The status port can also accept pushed observations at
`POST /api/v1/ingest`, authenticated with per-account remote tokens instead
of the Presto shared token. It is off by default; turn it on with the admin
page's **Accept remote reports** switch (stored in the database), with
`[daemon] ingest_enabled = true` in `dash.toml`, or with
`DASH_INGEST_ENABLED=1` for containers. The environment variable wins, and
any successful change takes effect without a restart; with the route off,
the path answers 404 like any unknown one.

The route speaks plain HTTP only — this project deliberately does not
terminate TLS. Never expose it to an untrusted network directly: put the
daemon behind a reverse proxy with TLS, or keep it on a private network such
as Tailscale. Request bodies carry no secrets or credentials, and a remote
token is write-only for one account, but the port also serves the status
endpoint, so treat its exposure like any other unencrypted service. See
[claude.md](claude.md#remote-computers) for the one-time collection setup.


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
