# Local configuration and encrypted keys

Start the configuration page with the installed daemon:

```bash
cd daemon
pip install -e .
python -m presto_ai_dash.app --config ../examples/config.example.toml --admin
```

Open **http://127.0.0.1:8788** on the machine running the daemon. Unlock with the
separate admin token shown in its startup log. The device's shared token cannot
access configuration. To retain an admin token across restarts, set
`DASH_ADMIN_TOKEN` or `admin.token` in your local TOML. The admin listener always
binds to loopback; for a remote headless server, use an SSH port forward to its
admin port. The status listener remains on `daemon.host`.

## Sources and accounts

The page supports adding, editing, disabling, and deleting multiple source
instances. Each instance has a stable ID, a display name, its source type,
connection settings, an enabled flag, and an optional encrypted key. For
example, two LiteLLM instances can use different gateways or keys. Their usage
is reported separately because two keys may observe overlapping traffic.

Disable the fake source before enabling a live source. Only one demo instance
can be enabled, and it cannot run with live sources.

For an existing key, leaving the key field blank preserves it. Enter a new key
to replace it, or select **Remove stored key**. Keys are never returned by the
management API. Edits preserve the instance ID and its observations. Deleting
an instance removes its encrypted key and stops future collection; its past
observations remain until retention cleanup.

Changes trigger collection without restarting the daemon. An in-flight probe
may finish first; its result is discarded if its configuration changed or the
instance was disabled/deleted.

Available connectors are fake, Ollama, and LiteLLM. OpenAI/Codex, Claude, and
OpenCode Go subscription quota adapters are the next planned integrations;
their account/login and reporting paths require readiness checks first.
See [subscription monitoring](subscription-monitoring.md). They are not current
source types, and entering their credentials does not add a missing usage API.
Direct API billing and Ollama Cloud connectors remain future work. Application
attribution remains a separate feature; these reusable source IDs provide a
place to attach it later.

## What is stored where

| Data | Location |
| --- | --- |
| Source instances and ordinary source settings | SQLite `source_instances` |
| Provider keys | Authenticated ciphertext in SQLite, bound to the instance ID |
| Encryption master key | `DASH_ENCRYPTION_KEY`, or a separate protected key file |
| Bind address, ports, tokens, polling, retention | Bootstrap TOML / environment |
| Usage snapshots and alert observations | Existing SQLite history tables |

The first `--admin` start imports TOML source settings and the resolved LiteLLM
environment key into SQLite. Import runs once. After that, database source
configuration takes precedence, including when the admin page is disabled.
Deleting every source does not re-import the defaults on restart.
`--check` validates bootstrap TOML; it does not unlock or validate the database.
Remove provider keys from plaintext TOML after importing them.

SQLite schema version 2 adds configuration tables; existing history is preserved.
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

Requests require the admin bearer token. Mutations require JSON and are capped
at 64 KiB. Host/Origin checks, absence of cross-origin permissions, and the
separate listener protect browser access. Updates/deletes require the current
`revision`; conflicting edits return 409 and require a reload.

The device endpoint still returns its three navigation cards. In managed mode
it also includes a `sources` array containing separate enabled-instance
measurements. Multiple instances of one type are summarized on that type's
card without summing possibly overlapping usage. With `STATIC_DEMO = False`,
the Presto polls this endpoint and lists configured instances on Home, retaining
the Ollama/LiteLLM card navigation. Names, enable/disable changes, and instances
appear on its next successful poll. Static cloud quota bars are shown only in
static preview mode; live quota measurements remain future work.
