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

## Sources and accounts

The page supports adding, editing, disabling, and deleting multiple source
instances. Each instance has a stable ID, a display name, its source type,
connection settings, an enabled flag, and an optional encrypted key. For
example, two LiteLLM instances can use different gateways or keys. Their usage
is reported separately because two keys may observe overlapping traffic.

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

SQLite schema version 4 adds Claude export/setup storage; version 3 added
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

## Management API

The admin port serves the page plus:

| Method | Path | Result |
| --- | --- | --- |
| GET | `/api/admin/sources` | Connector field descriptions and saved instances; only `has_key` is returned |
| POST | `/api/admin/sources` | Add an instance |
| PUT | `/api/admin/sources/{id}` | Edit, replace/remove key, enable/disable |
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
account's actual windows into stacked, labelled bars on Home. Quota accounts
appear before local/gateway activity. Additional accounts/windows scroll; swipe
cards show details and UTC resets. Names, enable/disable changes, and accounts
appear on the next successful poll. Refreshes preserve the selected card by
instance ID; removing it returns to Home. Static samples remain DEMO.

The updated renderer is deployed to the connected Presto. Automated real-font,
live-data, navigation, and clipped-scroll checks pass. Codex now reports live
quotas through its existing subscription login. Claude's exporter is implemented
but awaits a real reading; static demo values do not appear as live usage.
