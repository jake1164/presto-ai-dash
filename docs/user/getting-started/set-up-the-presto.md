# Set up the Presto

The Presto ships with MicroPython and a launcher already on it. You add one
app to it, tell the app where your daemon is, and give it your Wi-Fi
credentials.

## The guided setup

This is the normal route, and it needs **no copy of this repository**. The
daemon's admin page prepares the exact app its running build expects, so a
Presto installed this way matches the daemon from the first poll.

You need:

- the daemon installed and running, from PyPI/pipx or Docker, anywhere on your
  network;
- the Presto, plugged into a Windows, macOS or Linux computer by a
  **data-capable** USB cable;
- Python 3 on that computer. The setup script explains how to add the two
  packages it needs (`cryptography` and `mpremote`) if they are missing.

### 1. Open the admin page and choose Set up a Presto

Open the daemon's admin page and unlock it with the admin token. On the
**Settings** tab, open **Set up a Presto**.

### 2. Confirm the address the Presto can reach

The page suggests addresses it knows about. Use the daemon computer's LAN
address, such as `192.168.1.50`, and the daemon's device port (8787 by
default).

- **Never `localhost` or `127.0.0.1`:** that is the Presto's own address, not
  the daemon's.
- **Docker:** do not use a container-internal address. Use the host's LAN
  address and the port the Compose file publishes.
- **A container on a remote server:** the Presto contacts the server over your
  network. Nothing about the container is involved in the USB setup.

Press **Prepare download**. The page then shows:

- a **setup unlock code** with a **Copy** button;
- **`presto_setup.py`**, the standalone installer with this build's app files;
- **`presto-setup.enc`**, the encrypted configuration for this handoff.

The code is deliberately **not** inside either file. It is shown once; if you
lose it, prepare a new download. The encrypted file holds the daemon address,
the app revision and the persistent shared device token, so treat it as
sensitive and delete it when setup is done.

### 3. Run the setup script on the USB computer

Put both files in one folder on the computer the Presto is plugged into, open a
terminal there, and run:

```bash
python presto_setup.py
```

On macOS or Linux, use `python3` if that is how Python 3 is named. The script:

1. asks for the unlock code, with the input hidden, and decrypts the setup file
   **in memory**. Nothing is written to the device, and no temporary plaintext
   file is created on the computer;
2. lists the USB serial devices and asks you to choose one. It never picks a
   board by itself;
3. checks that the chosen device is a Pimoroni Presto **before** writing
   anything, and stops if it is not;
4. asks for your Wi-Fi network name and password locally (hidden), and tells
   you what will change. It only goes to the Presto's own `/secrets.py`;
5. shows a summary, then installs the app, verifies every file by SHA-256, and
   puts the previous app back if a write fails;
6. starts AI Dash and checks from the device that it can reach the daemon.

On success it deletes `presto-setup.enc` and says so. On any failure it keeps
the file, explains what to check, and lets you run the same command again.

### 4. What it keeps

- **Firmware and Pimoroni's stock launcher** are never touched.
- **Existing device settings are kept** unless you answer yes to replacing
  them. Replacing `/ai_dash_files/config.py` keeps the device's existing random
  device identifier; a new device gets one.
- **Other files on the device** are left alone. On a device that already runs
  AI Dash, app modules from an older build are retired.

### If something goes wrong

| What you see | What to do |
| --- | --- |
| `needs two Python packages` | Run the `pip install` line the script prints, then run it again. |
| `No USB serial device was found` | Use a data cable, not a charge-only one. Press **RESET** on the Presto. List ports with `python -m mpremote connect list`. |
| `not a Pimoroni Presto` | The chosen port is another board. Run the script again and choose the Presto. |
| `unlock code did not open` | Copy the code again from the admin page (all 32 letters and digits), or prepare a new download. |
| `different Presto app build` | The daemon was upgraded between the two downloads. Prepare both files again. |
| `could not join the Wi-Fi network` | The Presto's radio is 2.4 GHz only. Check the network name and password. |
| `reached the daemon, but the daemon refused the device token` | Set `daemon.shared_token` to a fixed value, restart the daemon, and prepare a new download. |
| `could not reach the daemon` | Check the address and port, and that the daemon is running and reachable from the Presto's network. |

Deleting the encrypted file, losing the code or preparing another handoff does
**not** revoke the shared device token. To revoke it, change
`daemon.shared_token`, restart the daemon, and set the same value on every
device that uses it.

### A note on updates

This one-time setup installs the app of the build that served it. Upgrading
the daemon does **not** change the app on a Presto that is already installed;
run **Set up a Presto** again (or the repository's sync tool, below) to move a
device to the new build. Unattended Wi-Fi updates and a two-slot launcher are
future work and are not part of this setup.

## The manual route

The rest of this page is the manual path, for a development checkout or for
understanding exactly which files go where. It does the same thing the guided
setup does, one file at a time.

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

These two files go on the device first. `install-presto` refuses a device that
has no `/ai_dash_files/config.py`, because it never creates your settings (or
chooses a Wi-Fi network) for you: it points at **Set up a Presto** instead.

**With a checkout of the repository.** The files are `presto/config.py` and
`presto/secrets.py`. From the repository root, with `mpremote` installed:

```bash
python -m mpremote connect COM3 fs cp presto/config.py :/ai_dash_files/config.py
python -m mpremote connect COM3 fs cp presto/secrets.py :/secrets.py
```

**Without a checkout (installed with pipx, or the daemon runs in Docker).**
Make two small text files in any folder, named `config.py` and `secrets.py`,
with the contents shown above. The USB tool `mpremote` is installed with the
`[device]` extra (step 4), and `pipx run` starts it:

```bash
pipx run mpremote connect COM3 fs mkdir :/ai_dash_files
pipx run mpremote connect COM3 fs cp config.py :/ai_dash_files/config.py
pipx run mpremote connect COM3 fs cp secrets.py :/secrets.py
```

If the first command says the folder already exists, carry on with the other two.

Replace `COM3` with the port of your Presto. List the ports with:

```bash
python -m mpremote connect list
```

(`pipx run mpremote connect list` does the same without a checkout.)

When `mpremote` copies a file it stops the running app, so the screen goes
black. Restart the board with `mpremote connect COM3 reset` (or unplug and
replug it), or run the app install in step 4, which relaunches it.

Never put a provider API key on the device. It has no use for one.

## 4. Install the app

### From a release (no checkout needed)

A release wheel carries the Presto app, so the device always gets the version
that matches your daemon. Install the package with its USB tools, then install
the app with the port you found above:

```bash
pipx install "presto-ai-dash[device]"      # or the wheel from the GitHub Release
presto-ai-dash install-presto --port COM3
presto-ai-dash check-presto --port COM3          # compares only, writes nothing
```

Run these on the computer the Presto is plugged into by USB. If the daemon runs
in Docker or on a server, that is usually a different computer: the container
never touches USB. Install the same version as the image, for example
`pipx install "presto-ai-dash[device]==0.50.0"` for the `0.50.0` image.

On macOS or Linux the port looks like `/dev/ttyACM0`. After an upgrade of the
daemon (`pipx upgrade presto-ai-dash`), run `install-presto` again. It does the same verified, rolled-back copy
as the repository tool below, and leaves the firmware and your private settings
alone. A daemon built from the source tree (not from a release wheel) does not
include the app: use the repository tool.

### From the repository

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
