# Architecture overview

## The shape of the system

```text
                  +--------------------+
                  |   Pimoroni Presto  |   MicroPython app, 240x240, 7 LEDs, buzzer
                  +--------------------+
                    |  GET /api/v1/status
                    |  Authorization: Bearer <shared token>
                    v
                  +--------------------+
                  |   Python daemon    |   api.py
                  +--------------------+
                    |     |      |     |
                collect  store   serve  build
                    |     |      |     |
                    v     v      v     v
               +--------+ +-----+ +------------------+
               | sources| |SQLite| | dashboard.py     |
               +--------+ +-----+ +------------------+
                    |
                    v
        Claude, Codex, OpenCode Go, Ollama Cloud, Ollama, LiteLLM
```

The daemon is the brain. The device is a display and a remote control for
one setting.

## The daemon's modules

| Responsibility | Module |
| --- | --- |
| Load and validate the configuration | `config.py` |
| Edit accounts and apply the changes | `management.py` |
| Encrypt provider credentials, protect the master key | `credentials.py` |
| Serve the configuration page and the admin API | `admin.py`, `web/` |
| The shared vocabulary: statuses, snapshots, cards | `models.py` |
| Talk to services over HTTP | `http_client.py` |
| Collect from each service | `sources/` |
| Normalise the readings | `quotas.py` |
| Local readers and the push collector | `collectors/` |
| Validate and store pushed readings | `ingest.py` |
| Persist snapshots, accounts and observations | `storage.py` |
| Decide the alert thresholds | `alert_policy.py`, `alerts.py` |
| Build the status payload and the device view | `dashboard.py` |
| Build a card's tiles and detail lines | `card_tiles.py` |
| Serve the device API | `api.py` |
| Wire it together and run | `app.py` |

## The collection loop

After the first admin start, the accounts in SQLite are authoritative. The
managed collector wakes on a timer or when a configuration change happens:

```text
for each enabled account:
    build the source object from its stored settings
    decrypt its credential, if it has one
    source.collect_safely()      -> a SourceSnapshot
    storage.save_snapshot(...)
```

Accounts are probed concurrently, up to four at a time. The source object is
cached and rebuilt only when the account's revision changes, so a source
that keeps state - a cached reading, a next-probe time - keeps it.

`collect_safely()` never raises. A source that fails becomes an `offline`
snapshot with the error text, and the daemon keeps running.

The status endpoint never blocks on a source. It reads the newest snapshot
per account from SQLite and renders immediately, so a slow or dead service
shows a stale card rather than a hung endpoint.

## The status vocabulary

```text
ok  <  active  <  warning  <  near_limit  <  rate_limited  <  critical  <  offline
```

`normal` is accepted as an alias for `ok` and normalised on the way in.
`overall_status` is the most severe status across the cards.

## The two payloads

The daemon builds one payload and then makes a **device view** of it:

- the **full** payload keeps everything, for the admin page and the tests,
- the **device view** keeps only the fields the device reads, and sends each
  quota report once.

The whitelist is the `DEVICE_*_KEYS` constants in `dashboard.py`, and
`daemon/tests/test_device_payload.py` proves two things: the device draws
exactly the same screens from the slim payload, and the whitelist still
covers every key the device code reads.

## The Presto app

```text
presto/main.py     the only file that imports presto/machine
presto/net.py      the HTTP fetch and the one write
presto/cards.py    the card model, and the status -> accent mapping   (pure)
presto/home.py     the Home rows, the overflow strip, the badges        (pure)
presto/gestures.py tap / swipe / long-press state machine              (pure)
presto/alerts.py   status -> LED colour and sound cue                  (pure)
presto/ui.py       the drawing
presto/popup.py    the pop-to-top rules                                (pure)
```

The split exists so that everything decidable without hardware is unit
tested on a development machine, and the hardware layer stays a thin
adapter. A test enforces the layering: only `main.py` and `ui.py` may import
`presto`, `machine`, `picographics` or `picovector`.

Everything is drawn with bitmap fonts and ASCII text.

## One implementation, two places

`presto/` is the only app source. The host-side preview (`PSL`) imports the
real `main.py` and `ui.py` and fakes only the display, the touch, the LEDs
and the Wi-Fi. `tools/presto_bundle.py` is the shared inventory: it decides
which files are public, maps `main.py` to its device path, and computes the
revision hash the device and the preview report.

So there is no simulator-specific copy of the app logic, and the code on
the device is byte-for-byte the code the preview runs.

## Storage

SQLite, with a schema version. The tables that matter:

| Table | What it holds |
| --- | --- |
| `source_instances` | The accounts: their type, name, settings, position, revision |
| Credentials | Encrypted ciphertext, bound to the instance ID |
| `observations` | The pushed readings, with their evidence hashes and collector trail |
| `ingest_tokens` | The SHA-256 hashes of the remote tokens |
| Snapshots and alerts | The short history, swept by the retention timer |
| `admin_sessions` | The browser sessions: credential hashes, CSRF tokens, expiry |

The encryption master key lives outside the database.

## Security posture

- The daemon binds to the LAN and is never meant to be public.
- `/api/v1/status` requires the shared token.
- Provider secrets are authenticated ciphertext in SQLite, and the master key
  is outside it.
- The admin page has its own token, a loopback-only listener, and Host and
  Origin checks. The device token cannot edit sources.
- The daemon never makes a paid model call.

## See also

- [How a source works](how-a-source-works.md)
- [Running the tests](running-the-tests.md)
- [The preview and the device sync](preview-and-device.md)
