# Set up the Presto

The Presto ships with MicroPython and a launcher already on it. You add one
app to it, tell the app where your daemon is, and give it your Wi-Fi
credentials.

## 1. Flash the firmware (only if you need to)

If the Presto already shows its launcher carousel when you press **RESET**,
skip this step.

Otherwise, download the latest `with-filesystem` `.uf2` from the
[Presto releases page](https://github.com/pimoroni/presto/releases/latest),
hold **BOOT** while tapping **RESET**, and drag the `.uf2` onto the `RP2350`
drive that appears.

## 2. Copy the app files

There is one app implementation, in the `presto/` directory of the
repository. The physical device runs copies of exactly those files, so it
cannot see edits on your computer until you sync them.

Keep the device's existing `/main.py` carousel launcher. Copy the app with
[Thonny](https://thonny.org/) (interpreter: *MicroPython (Raspberry Pi Pico)*)
or with `mpremote`:

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
| `presto/popup.py` | `/ai_dash_files/popup.py` |
| `presto/ticks.py` | `/ai_dash_files/ticks.py` |
| `presto/ui.py` | `/ai_dash_files/ui.py` |

Only the entry script lives at the root, so the helper modules do not appear
as extra apps in the carousel. The folder is named `ai_dash_files` on
purpose: MicroPython would import a folder called `/ai_dash` instead of
running the root `/ai_dash.py` entry.

The `tools/presto_device.py` sync command does all of this for you and
verifies it afterwards. Use it instead of copying files by hand.

## 3. The device settings

Two files on the device hold the settings. Neither is in version control,
and neither belongs in a screenshot.

### `/ai_dash_files/config.py`

Where the device finds your daemon and how it identifies itself:

```python
DAEMON_HOST = "192.168.1.50"
DAEMON_PORT = 8787
SHARED_TOKEN = "the same value as daemon.shared_token"
DEVICE_NAME = "presto"
```

The app always polls the daemon. It has no demo data of its own: sample data
comes from the daemon's [demo mode](../guides/admin-page.md#demo-mode).

`DAEMON_HOST` is the LAN IPv4 address of the machine running the daemon.
Use an address, not a hostname: name lookup can block on MicroPython and
stall the touch loop.

### `/secrets.py`

The Wi-Fi credentials:

```python
WIFI_SSID = "your-network-name"
WIFI_PASSWORD = "your-network-password"
```

The firmware reads this root-level file for Wi-Fi. The credentials go to your
access point and nowhere else: they are never sent to the daemon.

The Presto's radio is 2.4 GHz only. A 5 GHz-only network will not work.

### Copying the private files

From the repository root, with `mpremote` installed:

```bash
python -m mpremote connect COM3 fs cp presto/config.py :/ai_dash_files/config.py
python -m mpremote connect COM3 fs cp presto/secrets.py :/secrets.py
```

Replace `COM3` with the port of your Presto. List the ports with:

```bash
python -m mpremote connect list
```

Never put a provider API key on the device. It has no use for one.

## 4. Sync the app from the repository

The sync tool uploads the current app files, verifies every file by its
SHA-256, replaces the installed files with a rollback if a write fails, and
verifies the installed bytes afterwards. It leaves the firmware, your private
settings and the secrets alone.

```bash
.venv/Scripts/python tools/presto_device.py sync --port COM3
.venv/Scripts/python tools/presto_device.py check --port COM3
```

On macOS or Linux use `.venv/bin/python`. The tool refuses to pick a board
automatically: you name the port.

In VS Code the same two actions are **Terminal -> Run Task -> Sync Presto**
and **Check Presto sync**.

## 5. Start it

Press **RESET** on the Presto, find **AI Dash** in the firmware carousel and
tap it.

The app joins the Wi-Fi network, then polls the daemon every
`POLL_INTERVAL` seconds (15 by default). The first screen is Home, listing
your accounts.

If the daemon is not reachable yet, the screen says **Daemon offline** and
keeps trying. See [Daemon offline and Wi-Fi drops](../troubleshooting/daemon-offline.md).

## 6. The demo first

To see the screens without any account, turn on **demo mode** in the daemon's
admin page (Settings, Demo mode), or start the daemon with `DASH_DEMO=1`. The
Presto then shows a scripted set of sample accounts, each screen marked with a
purple **DEMO - SAMPLE DATA** chip, and the daemon makes no provider request
for it. That is also the quickest way to check the screen, the touch and the
LEDs. Turn it off to see your real accounts.

You can also try the same screens in a browser, without a Presto:
[see it running](../demo/index.md).

If no service is set up and demo mode is off, the Presto shows **No services
set up / Open the admin page**, with an amber bar and LED. That is the first
screen on a new install: it means the Presto reached the daemon, which has
nothing to show yet.

## Polling and failure behaviour

| Setting | Default | What it does |
| --- | --- | --- |
| `POLL_INTERVAL` | 15 | Seconds between polls |
| `RETRY_INTERVAL` | 12 | Seconds between retries after a failure |
| `WIFI_TIMEOUT` | 10 | Seconds before a Wi-Fi attempt is given up |
| `HTTP_TIMEOUT` | 8 | Seconds before an HTTP request is given up |
| `OFFLINE_AFTER_S` | 30 | Seconds without a good poll before the offline screen |

After an interruption the last good screens stay visible, marked
**STALE - retrying**. When they reach `OFFLINE_AFTER_S` the app shows the
error screen instead. A successful poll brings the selected card back on its
own.

## Next

- [Add the first source](add-the-first-source.md)
- [Home display and the overflow strip](../guides/home-display.md)
