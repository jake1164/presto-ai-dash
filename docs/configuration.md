> Public, user-facing version: <https://jake1164.github.io/presto-ai-dash/guides/admin-page/>, <https://jake1164.github.io/presto-ai-dash/reference/configuration/>, <https://jake1164.github.io/presto-ai-dash/reference/ports-and-endpoints/>. This file is the internal reference and may go into more detail.
>
> Corrections (2026-10-21):
>
> - "Dashboard-managed Codex sign-in remains pending": the sign-in, cancel and sign-out routes exist in the admin API, but the page has no controls for them.
> - Claude's real-export verification is done (live readings on 2026-10-11, M9).

# Local configuration and encrypted keys

Start the configuration page with the installed daemon:

```bash
cd daemon
pip install -e .
python -m presto_ai_dash.app --config ../examples/config.example.toml --admin
```

Open **http://127.0.0.1:8788** on the machine running the daemon. Unlock with the
separate admin token shown in its startup log when `admin.token` is empty. A
fresh token is generated each start. If you set `DASH_ADMIN_TOKEN` or
`admin.token`, use that configured value; it is not printed. The device's shared
token cannot access configuration.

After unlocking, this browser stays logged in for **seven days**, including page
reloads, browser restarts, and daemon restarts with a freshly generated admin
token. The browser holds an HttpOnly, SameSite=Strict cookie; SQLite holds only
its credential hash, an expiry, and a separate CSRF token. The startup admin
token is not stored in the browser. **Lock configuration** revokes the current
browser session on the server. Expiry requires the current startup token to
log in again; other browsers have independent sessions. The expiry is absolute,
not extended by activity. Sessions survive only when reusing the same database.

Use the same browser address and port each time: `localhost` and `127.0.0.1`
have separate cookies. The admin listener binds to loopback by default; for a
remote headless server, use an SSH port forward to its admin port. A
container may bind it to another address, but then it needs an explicit
admin token and the list of names it answers to; see [Docker](user/getting-started/install-with-docker.md).
The status listener remains on `daemon.host`.

### The admin bind address and the names it answers to

`admin.host` is the address the admin page listens on (`127.0.0.1` by
default) and `admin.allowed_hosts` is the list of names it answers to. The
environment variables are `DASH_ADMIN_HOST` and `DASH_ADMIN_ALLOWED_HOSTS`,
the latter a comma-separated list.

The page always answers to `127.0.0.1` and `localhost` on its own port. A
name in the list answers too, with or without a port, and over HTTP or
HTTPS, which is what a reverse proxy that terminates TLS needs. Any other
`Host` header is refused, exactly as before, and a change still needs the
session's own CSRF token.

A bind address that is not loopback makes the page reachable from other
machines, so it takes two deliberate choices: an admin token you set
(`admin.token` or `DASH_ADMIN_TOKEN`), and a non-empty allowed-hosts list.
Without both, the daemon refuses to start the admin page and says why.

## The three tabs

The page has three tabs: **Services**, **Status** and **Settings**.

- **Services** configures and arranges the accounts. Every account is a card
  that is collapsed by default and shows, on one line, a status dot, the
  name, its key numbers (`5h 6% / Wk 59%`, `in use 3m`, `12 req`), a
  freshness chip (`fresh 3m`, `stale 2d`, `no data`) and a chip that says
  where the numbers came from (`terminal export`, `usage cache`, `local
  logs`). Opening a card shows everything the page has always shown for that
  account: the window meters, the labelled Quota / Activity / Usage cache /
  Status note lines, and that account's own actions (the Claude export
  setup, the remote token, the collector trail). The order arrows, the
  **Enabled** checkbox, **Edit** and **Add source** stay where they were.
  Everything on this tab applies to one account at a time.
- **Status** is read-only and says whether everything works: the daemon, the
  clients, the health of every account, the local readers, the remote
  collectors and the recent problems. It refreshes itself every ten seconds
  while the tab is open and the browser tab is in the foreground.
- **Settings** holds what applies to every service, in collapsible sections
  (the first open, the rest folded away). Every section is headed
  *Applies to all services*.

The active tab is in the URL hash (`#services`, `#status`, `#settings`), so a
link points at a tab, and it is remembered in `localStorage` for the next
visit. A browser that refuses `localStorage` (private mode, a full disk)
still works: the hash and the default tab are enough. The tabs follow the
ARIA tab pattern: they are reachable with the keyboard, the left and right
arrow keys move between them, and Home and End jump to the first and the
last one. The page is plain HTML, CSS and JavaScript with no dependencies,
and it works at phone width. Every value that comes from the daemon is
written with `textContent`, so a report can never become markup.

## Sources and accounts

The page supports adding, editing, disabling, and deleting multiple source
instances. Each instance has a stable ID, a display name, its source type,
connection settings, an enabled flag, and an optional encrypted key. For
example, two LiteLLM instances can use different gateways or keys. Their usage
is reported separately because two keys may observe overlapping traffic.

Drag an account by its grip to reorder it. The up/down buttons also move it
one place; a focused grip accepts the Up/Down arrow keys. The order saves
automatically in SQLite and survives restarts. Presto Home and the account
swipe cards follow it on their next successful poll, with Home always first.
Disabled accounts keep their saved position but are hidden on Presto. New
accounts are appended. Press Escape to cancel a drag. If another browser
changes the account list or order, a conflicting move reloads the saved list.

Each account row has an **Enabled** checkbox. Uncheck it to stop collection and
hide the account on Presto without deleting it. Check it again to resume.
Changes save immediately; the settings, stored key, history, and display
position are retained. Presto updates on its next successful poll.

Source settings render according to their declared type: `string` and `url`
fields are text inputs, `int` fields are number inputs bounded by their
configured minimum and maximum, and `bool` fields are checkboxes whose stored
value is a real JSON boolean. The management API accepts only that exact type
(`"true"` or `1` for a checkbox setting is a validation error), so an
opt-in setting such as Claude's local activity reader can never be enabled by
an ambiguous value.

A Claude account has a second freshness setting, **Mark usage cache readings
stale after (seconds)** (300 to 604800, default 7200): it is how long a
reading from Claude Code's local usage cache stays fresh, because that cache is
only refreshed when `/usage` is opened. The terminal export keeps its own
**Mark idle readings stale after (seconds)** threshold, and each window of a
merged report is judged by the source it came from.

Disable the fake source before enabling a live source. Only one demo instance
can be enabled, and it cannot run with live sources. Fake remains an available
**demo/test** connector for hardware and connection checks. Delete its saved
instance if you no longer want it listed; it will not be recreated on restart.

The Ollama connector monitors **local server health and loaded models**. Local
Ollama requires no API key, so its source row says **local API · no key required**.
This is expected, not a lost credential. Ollama Cloud authentication and
subscription quotas are not implemented by this connector.

For an existing key, leaving the key field blank preserves it. Enter a new key
to replace it, or select **Remove stored key**. Keys are never returned by the
management API. Edits preserve the instance ID and its observations. Deleting
an instance removes its encrypted key and stops future collection; its past
observations remain until retention cleanup.

Changes trigger collection without restarting the daemon. An in-flight probe
may finish first; its result is discarded if its configuration changed or the
instance was disabled/deleted.

Available connectors are fake, local Ollama, LiteLLM, **Codex**, **Claude**, **OpenCode Go**, and
**Ollama Cloud**. Manage provider account settings, keys, and refresh intervals in this
page; the database is the source of truth. No separate provider configuration
file is required. OpenCode Go shows 5h/Weekly/Monthly quota meters. Ollama Cloud
shows dated activity; its current reporting response does not expose monthly
allowance or remaining credits. See [cloud readings](quotas.md).

The menu labels Fake as demo/test data. Codex uses an existing subscription
login; its source settings live in SQLite, while OAuth stays with Codex's own
credential cache/keychain. Dashboard-managed Codex sign-in remains pending.
See [Codex setup](codex.md). Claude has a [terminal quota exporter](claude.md),
configured with a setup/removal button in the account row. Its real-export
verification is pending. No separate telemetry file is required.
Direct OpenAI/Anthropic API billing and application attribution remain separate
future features; reusable source IDs provide a place to attach attribution later.

## What is stored where

| Data | Location |
| --- | --- |
| Source instances, ordinary source settings, provider refresh intervals | SQLite `source_instances`, managed through the UI |
| Provider keys | Authenticated ciphertext in SQLite, bound to the instance ID |
| Codex OAuth login | Provider-owned Codex credential cache/keychain; never scraped/copied into SQLite |
| Claude quota exports and encrypted previous status line | SQLite; managed through the UI |
| Claude exporter command | Provider-owned Claude Code `settings.json`, installed/removed by the UI |
| Encryption master key | `DASH_ENCRYPTION_KEY`, or a separate protected key file |
| Bind address, ports, tokens, polling, retention | Bootstrap TOML / environment |
| Usage snapshots and alert observations | Existing SQLite history tables |
| Remembered browser sessions | SQLite `admin_sessions`: credential hashes, CSRF tokens, absolute expiry |

The first `--admin` start imports TOML source settings and the resolved LiteLLM
environment key into SQLite. Import runs once. After that, database source
configuration takes precedence, including when the admin page is disabled.
Deleting every source does not re-import the defaults on restart.
`--check` validates bootstrap TOML; it does not unlock or validate the database.
Remove provider keys from plaintext TOML after importing them.

SQLite schema version 5 adds account positions, preserving the existing order
during migration. Version 4 added Claude export/setup storage; version 3 added
browser sessions to the version 2 configuration
tables; existing source instances, encrypted credentials, and history are preserved.
Only credential values are encrypted. Ordinary source settings and usage
history remain readable in SQLite.

## Encryption key and recovery

Encryption uses [Fernet authenticated encryption](https://cryptography.io/en/latest/fernet/)
from the `cryptography` package. Ciphertexts include their source ID inside the
authenticated payload, preventing account credentials from being swapped
between records. A verifier also detects the wrong master key at startup.

By default, the daemon creates `<database>.key` outside SQLite. On Windows,
the key file is protected with [DPAPI for the current Windows account](https://learn.microsoft.com/en-us/windows/win32/api/dpapi/nf-dpapi-cryptprotectdata).
On Linux/macOS, it is an owner-only file (mode 600). `admin.key_file` or
`DASH_KEY_FILE` can choose a different location.

For deployments needing a portable externally managed master key:

```bash
python -m presto_ai_dash.app --generate-key
# Store the resulting key securely as DASH_ENCRYPTION_KEY for the daemon.
```

Keep the same key across restarts. Back up the database and the key separately.
A Windows DPAPI key file normally needs the same Windows account and computer;
use an external master key when planning a move between machines. If the key
is missing, wrong, or a credential is corrupted, startup fails without creating
a replacement key or overwriting encrypted records. Restore the original key.
Provider-key replacement is supported; automatic master-key rotation is not
part of this checkpoint.

Encryption protects credentials in database copies. Access to the running
daemon account and its master key still permits decryption.

## Home display

`overflow_rows` is the maximum number of lines the Home overflow strip may
use to name the accounts that do not fit in the list. It is an integer from 0
to 3, and 1 is the default. The strip uses only as many lines as the hidden
names need, up to that maximum, shows `+N` when the maximum is not enough,
and disappears when the whole list fits. 0 turns the strip off.

It is read afresh for every status payload, so a change applies on the next
poll without restarting the daemon. Three sources, in this order:

1. The `DASH_OVERFLOW_ROWS` environment variable. A value that is not an
   integer in 0..3 stops the daemon at startup with a configuration error.
2. The value stored in SQLite by the admin page (the **Home display** section).
   When the environment variable is set, the page's control is read-only and
   the request returns 409.
3. `[display] overflow_rows` in `dash.toml`, for a bootstrap default.
4. One line, when none of the above says otherwise.

A garbled environment variable found at run time (not at startup) fails closed
to the default and is logged once.

## Alerts

The alert settings apply to every service. They live in the admin page's
**Alerts** section (Settings tab), and in the `[alerts]` section of
`dash.toml`:

| Key | Meaning | Default |
| --- | --- | --- |
| `enabled` | The master switch: alerts on or off for every service | on |
| `warning_percent` | A window that has used this much of its allowance makes the service *near limit*: yellow. 1..100 | 80 |
| `critical_percent` | A window that has used this much makes the service *critical*: red. 1..100, and above the warning percent | 95 |
| `sound` | Sound a short cue when a service gets worse | off |
| `quiet_hours` | Be silent and dim the light during the quiet window | off |
| `quiet_start` | When the quiet window starts, `HH:MM` in the daemon's local time | `22:00` |
| `quiet_end` | When the quiet window ends, `HH:MM` in the daemon's local time | `07:00` |
| `pop_to_top` | Let Home scroll to a service that has just got worse | on |
| `mute_hours` | How long a long-press mute on the Presto lasts, in hours. 0..48; 0 means it lasts until you press again | 4 |

```toml
[alerts]
enabled = true
warning_percent = 80
critical_percent = 95
sound = false
quiet_hours = false
quiet_start = "22:00"
quiet_end = "07:00"
pop_to_top = true
mute_hours = 4
```

Where a value comes from, in this order:

1. The value stored in SQLite by the admin page.
2. `[alerts]` in `dash.toml`.
3. The built-in default above.

There is no environment variable for these: they are the user's own choices.
A value the admin page puts back to the value in `dash.toml` stops
overriding the file, so the effective configuration still says where each
value came from.

**The sound is off until you turn it on.** A desk device must not beep
unannounced, so `sound` defaults to off and the admin page's **Alerts**
section shows the *Sound on a new warning* box unticked. Turn it on there, or
set `sound = true` in `[alerts]` in `dash.toml`; the admin page's stored
value wins over the file. Nothing else changes when you turn the sound on:
the alert thresholds, the LED and the pop-to-top are separate settings.

A value you already stored (in the database, or as `sound = true` in
`dash.toml`) is respected unchanged: only the unset default is off, and
there is no migration.

The device has its own hardware permission, `SOUND_ENABLED` in the Presto's
`config.py`. The audible result is the AND of the two: the Presto chirps
only when the daemon says the sound is on *and* the device's own setting
allows it.

The daemon reads them afresh for every status payload, so a change applies on
the next poll without restarting the daemon.

**The master switch and the per-service settings.** `enabled = false` turns
alerts off for every service at once. With the master off the admin page
disables the sound, quiet-hours and pop-to-top controls and every per-service
control (it keeps their stored values, and says in words why they are off),
and the service cards show the chip *alerts off (global)*.

Each service can also have its own settings, on the Services tab in the
service's expanded card:

- **alerts on or off** for that one service, and
- its own warning and critical percent. A blank field means *use the global
  value*; the field shows the global value as its placeholder (`global 80`).

These per-service settings are **not** source configuration fields. They are
stored in their own rows in SQLite, keyed by the service's instance id, so
switching the alerts of one service never rebuilds the source and never
restarts its readers. They survive an edit, a rename and a rebuild of the
source, and they are removed when the service is deleted.

A service's own thresholds are used only when the pair is usable: the
warning percent must sit below the critical one. When it does not - for
example because the global value changed under it - the service falls back
to the global pair, the daemon logs one line, and the admin page says
*override ignored*. A pair that is saved is checked against the pair it
would produce, so an unusable one is refused with a plain message.

**What "alerts off" means.** For that service (or for all of them when the
master is off): no chirp, no LED escalation and no pop-to-top, and the
service does not count towards the overall accent or the Home card's status.
It never hides data: the row, the numbers, the true threshold status colour
and the service's own card are unchanged. The daemon still computes the
service's true status.

**What alerts.** The percentage is mapped onto the status vocabulary: at the
warning percent a service is *near limit*, at the critical percent it is
*critical*, and at 100% it is *rate limited*, as it always was. A source
takes the status of its worst window. Only a fresh, available window alerts:

- a stale window keeps the stale presentation it already has and never
  raises an alert,
- an unavailable window never raises an alert,
- a window whose reset has passed never raises an alert, and
- a window that resets within the next 30 minutes does not raise an alert
  either: it is about to clear.

**What the device does with it.** The device tracks every service on its own,
so a second service that gets worse still speaks while another one is
already critical: one chirp for a new warning, two for a critical,
rate-limited or offline service. A warning that persists does not chirp
again, a recovery is silent, and the first reading after boot is a silent
baseline.

**The mute that expires.** A long press on the Presto mutes the sound. The
mute ends by itself after `mute_hours` hours (four by default; 0 means it
lasts until you press again), and the screen shows a `MUTED` chip with the
time left - `MUTED 3H`, `MUTED 45M`, or `MUTED` alone when the mute has no
end. A long press while muted unmutes at once. The mute is not stored
anywhere: a restart clears it. Changing `mute_hours` while a mute is running
applies to the next mute, not to the running one.

The device has no wall clock, so it counts the length with its own tick
counter, which wraps; the daemon only sends the number. The mute ends
silently: the statuses were tracked while it was on, so the device does not
chirp for an alarm that began during the mute.

The LED shows the worst service, not only the account on the open card: red
for critical, rate limited and offline, yellow for a warning, blue for a
service in use, green for everything else. A service that is merely in use
never hides a warning.

**Quiet hours.** The daemon computes the quiet flag from its own local time
and sends it with the payload, so the device needs no clock and no time
zone. While the flag is true the device is silent and its light is dimmed
(the colour is kept, so a warning is still visible).

**Pop to top.** When a service gets worse, Home scrolls so that the service's
row is on screen. It never moves while the user is touching the screen or has
scrolled recently, the alarm must hold for 30 seconds before the list moves,
a service moves the list at most once in ten minutes, a new alarm only acts
when it is worse than the worst one already shown, and the user's own
position comes back a minute later unless they have scrolled since. The
rules live in `presto/popup.py` and never run on a card or a detail view.

## The Status tab

**Daemon.** The version, the database schema version, the uptime, the poll
interval, the start time, the address the status listener binds to and the log
level.

**Clients.** Every device that fetches `/api/v1/status`: its name, its
address, when it last fetched, how often, the app revision it reports and
whether it is connected. A client that has not been seen for three poll
intervals is shown as *not connected*.

The daemon has one shared token, so authentication cannot tell clients apart.
Each client therefore declares a name in the `X-Presto-Client` request header
and may declare its app revision in `X-Presto-Revision`. The Presto sends
`DEVICE_NAME` from its own `config.py` (default `presto`); the PSL preview
sends `psl`. Header values are untrusted: a value is kept only when it is
printable ASCII of at most 32 characters, and anything else becomes
`unknown`. The daemon keeps at most 16 records in memory, one per (name,
address), and drops the least recently seen one; the records are lost when the
daemon restarts and nothing about a client is stored or sent anywhere else.

The address is best effort. Behind NAT, a reverse proxy or Docker Desktop,
several clients can share one address, so the name is what identifies a
client and two clients with the same name and address cannot be told apart.
The page says so.

**Accounts.** One row per account: when it was last collected, its status and
its last problem in plain words ("no reading yet", "the provider refused the
key").

**Readers and collectors.** The local activity reader (ok, scanning, or the
reason it is unavailable), the usage cache reader (when it was fetched, or
the reason it could not be read) and the remote collectors (when each account
last received a report, and from which host).

**Problems.** The recent `WARNING` and `ERROR` records of the daemon, in an
in-memory ring buffer of the last 50, de-duplicated. This is where "format
changed" and "access denied" become visible without reading a log file. The
buffer holds only the log message: paths and credential-shaped text are
replaced, and an exception contributes its type only, so no file contents or
secrets can end up on the page. The buffer is memory only and is empty again
after a restart.

## The Settings tab

**Home display** holds the overflow strip setting above.

**Remote reports** holds the ingest switch, the collector download and the
plain-HTTP warning.

**Alerts** holds the master switch, the thresholds, the sound switch, quiet
hours and the pop-to-top switch, all of which apply to every service.

**Effective daemon configuration** is read-only. Every value says where it
came from: `environment`, `dash.toml`, `database`, or `default` when nothing
decided it. These values need a daemon restart, so they are edited in
`dash.toml` or in the environment rather than on the page.

**About and help** holds the explanatory text: the cloud and subscription
sources, the Fake demo source, the order note, and where the logs and the
diagnostics commands are.

## Management API

The admin port serves the page plus:

| Method | Path | Result |
| --- | --- | --- |
| GET | `/api/admin/sources` | Connector field descriptions and saved instances; only `has_key` is returned |
| POST | `/api/admin/sources` | Add an instance |
| PUT | `/api/admin/sources/{id}` | Edit, replace/remove key, enable/disable |
| PUT | `/api/admin/source-order` | Save a complete account order, including disabled accounts |
| GET | `/api/admin/display` | The Home display settings and whether the environment locks them |
| PUT | `/api/admin/display` | Save the Home display settings (409 when the environment locks them) |
| GET | `/api/admin/alerts` | The alert settings, the master switch, the thresholds, the quiet hours and whether it is quiet now |
| PUT | `/api/admin/alerts` | Save the alert settings; the body must carry every one of them |
| GET | `/api/admin/sources/{id}/alerts` | One service's alert settings: its own values (a threshold that is null is inherited), the resolved values, and the flags `override_ignored` and `master_off` |
| PUT | `/api/admin/sources/{id}/alerts` | Save one service's alert settings; the body is a subset of `enabled`, `warning_percent` and `critical_percent`, and a null threshold means "inherit" |
| GET | `/api/admin/status` | The Status tab: the daemon, the clients, the account health, the readers, the collectors and the recent problems. Read-only |
| GET | `/api/admin/config` | The effective daemon configuration and where each value came from. Read-only |
| DELETE | `/api/admin/sources/{id}` | Delete the instance and its saved key |
| POST | `/api/admin/session` | Exchange the current admin bearer token for a seven-day browser session |
| GET | `/api/admin/session` | Restore a valid cookie session and obtain its CSRF token |
| DELETE | `/api/admin/session` | Revoke the current cookie session and clear its cookie |

Source requests accept the admin bearer token or a valid browser session.
Cookie-authenticated mutations also require the exact page Origin and the
`X-CSRF-Token` header returned by the session endpoint. Source mutations
require JSON and are capped at 64 KiB. Host/Origin checks, absence of cross-origin permissions, and the
separate listener protect browser access. Updates/deletes require the current
`revision`; conflicting edits return 409 and require a reload.
Reordering accepts `order` and `expected_order` arrays of account IDs. Each
account must appear exactly once in `order`; `expected_order` must match the
current sequence, otherwise the request returns 409. A reorder is atomic and
does not change account revisions or discard in-flight provider readings.

The two new endpoints, `/api/admin/status` and `/api/admin/config`, are GET
only and change nothing. They need the same authorization as the other
sources endpoints (the admin bearer token or a browser session) and the same
host and origin checks. A request to them with any other method returns 404.

The cookie is host-only, restricted to `/api/admin`, and named for the admin
port. The loopback listener uses HTTP, so it does not set the HTTPS-only Secure
attribute. Do not expose it through an unsecured remote HTTP proxy. Session
credentials are never logged or returned in JSON. Changing the startup admin
token deliberately leaves remembered sessions valid. To revoke all remembered
sessions, stop the daemon and delete the rows from `admin_sessions` in SQLite;
do not delete the database or its provider keys.

In managed live mode, the device endpoint returns Home plus one swipe card per
enabled source instance and a `sources` array of independent measurements.
Multiple accounts of one type have separate cards and usage. With
`STATIC_DEMO = False`, the Presto polls the endpoint and groups each quota
account's actual windows into stacked, labelled bars on Home. All accounts
follow the saved admin order. Additional accounts/windows scroll; swipe
cards show details and UTC resets. Names, enable/disable changes, and accounts
appear on the next successful poll. Refreshes preserve the selected card by
instance ID; removing it returns to Home. Static samples remain DEMO.

## The small summary and the lazy card detail

The status payload has two shapes. A client that asks for nothing gets the
whole payload, exactly as before. A client that asks for the small summary
gets the same payload without the cards' detail lines:

```text
GET /api/v1/status?cards=lazy
Authorization: Bearer <shared token>
```

The Presto asks for the small summary, so one source's detail can grow
without making every poll bigger. The answer keeps the card's title,
status, primary and secondary lines, the `sources` array and the settings;
only the `detail` lines of the cards are left out.

One card's detail is fetched when the card is opened:

```text
GET /api/v1/cards/<card id>
Authorization: Bearer <shared token>
```

The answer is `{"id": ..., "tiles": [...], "lines": [...]}`:

- `tiles` is a short list of at most six `{"label", "value", "tone"}` items.
  The label is at most 8 ASCII characters, the value at most 10, and the tone
  is one of `normal`, `good`, `warn`, `bad` or `dim`. A value the report does
  not carry is left out; it is never shown as zero.
- `lines` are that card's detail lines, the same ones the whole payload
  carries.

The card id must match `[A-Za-z0-9_-]{1,80}`. A wrong token is 401, an
unknown card is 404, and the route changes nothing: it reads the last
snapshots and returns what the card would show. It is never a provider call.

The payload keys the device reads are on a whitelist in
`daemon/src/presto_ai_dash/dashboard.py`, and a test proves that the device
code reads nothing else. Two keys are new in this shape:

- `sources[].marks` is the pair of percentages `[warning, critical]` that the
  service really uses, so a card can draw a tick on its bars. The device draws
  them and decides nothing; missing or junk marks mean no ticks.
- `cards[].tiles` is optional. A payload that carries the tiles with the card
  is drawn the same way as one that fetches them separately.

## The device's one write: `POST /api/v1/alerts/{id}`

The Presto can switch the alerts of one service on or off. It sends the shared
bearer token and a body of exactly `{"enabled": true}` or `{"enabled": false}`:

```text
POST /api/v1/alerts/<source id>
Authorization: Bearer <shared token>
Content-Type: application/json

{"enabled": false}
```

The answer is `{"ok": true, "alerts": <the effective answer>, "master": <bool>}`.

- It **sets** a state and never toggles, so a double tap or a retry is
  harmless.
- It changes only that one service's *alerts on/off* value: never the
  thresholds, never the master switch, never anything else, and never a
  provider. It is a setting of this daemon, not a provider action.
- The body must be JSON, with a `Content-Length` of at most 256 bytes, no
  chunked encoding, and exactly the one key `enabled` with a real boolean.
  Anything else is refused with 400 (413 for an oversized body, 415 for the
  wrong content type).
- An unknown service is 404, a wrong token is 401, and any method other
  than POST on that path is 405.
- The route has its own rate limit: 30 writes a minute per address, then
  429. It is separate from the ingest limiter.
- The daemon logs one line at INFO: `alerts for <id> turned on|off by
  <client name>`. The name comes from the `X-Presto-Client` header and is
  sanitised; the token is never logged.
- The write goes through the same code as the admin page's per-service
  control, so both surfaces agree and the last write wins.

**The shared token travels over plain HTTP on the LAN.** The device already
uses it to read the status payload. The worst an eavesdropper on the LAN
could do with it is switch the alerts of one service on or off: no provider
is contacted, no quota is changed, and no data is read or written that the
device could not read anyway. On an untrusted network, put the daemon behind
a reverse proxy with TLS or use a private network such as Tailscale.

The updated renderer is deployed to the connected Presto. Automated real-font,
live-data, navigation, and clipped-scroll checks pass. Codex now reports live
quotas through its existing subscription login. Claude's exporter is implemented
but awaits a real reading; static demo values do not appear as live usage.
