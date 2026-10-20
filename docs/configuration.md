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
have separate cookies. The admin listener always
binds to loopback; for a remote headless server, use an SSH port forward to its
admin port. The status listener remains on `daemon.host`.

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

The updated renderer is deployed to the connected Presto. Automated real-font,
live-data, navigation, and clipped-scroll checks pass. Codex now reports live
quotas through its existing subscription login. Claude's exporter is implemented
but awaits a real reading; static demo values do not appear as live usage.
