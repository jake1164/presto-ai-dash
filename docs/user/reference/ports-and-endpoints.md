# Ports and endpoints

The daemon serves two ports, and the device uses one of them. The
documentation site is a third thing entirely: it is static files on GitHub
Pages and never talks to your daemon.

## The ports

| Port | Default | Who uses it | Bind address |
| --- | --- | --- | --- |
| Status | 8787 | The Presto, and the host-side preview | `daemon.host`, `0.0.0.0` by default |
| Admin | 8788 | Your browser | `127.0.0.1` by default; see [The admin port](#the-admin-port) |
| Preview | 8790 | Your browser, on the host-side preview tool | `127.0.0.1` only |

The preview port is not part of the daemon. It belongs to the development
tool and is never started by the daemon.

## The status port

```text
GET /healthz
```

The one route with no token. It answers `{"ok": true}` and nothing else, and
it exists so a container can tell that the daemon is alive.

```text
GET /api/v1/status
Authorization: Bearer <shared token>
```

The full payload. A client that asks for nothing gets everything.

```text
GET /api/v1/status?cards=lazy
Authorization: Bearer <shared token>
```

The small summary: the same payload without the cards' detail lines. The
Presto asks for this, so one source's detail can grow without making every
poll bigger.

```text
GET /api/v1/cards/<card id>
Authorization: Bearer <shared token>
```

One card's detail, fetched when the card is opened. The answer is
`{"id": ..., "tiles": [...], "lines": [...]}`.

```text
POST /api/v1/alerts/<source id>
Authorization: Bearer <shared token>
Content-Type: application/json

{"enabled": false}
```

The device's one write: it sets one service's alerts on or off. See
[Alerts and the card bell](../guides/alerts-and-the-bell.md).

```text
POST /api/v1/ingest
Authorization: Bearer <remote token>
Content-Type: application/json
```

The remote reports route. It is off by default and answers `404` while it is
off. See [Remote reports](../concepts/remote-reports.md).

### The two optional objects in the answer

A request that carries a valid device identifier and lifecycle report gets two
extra top-level objects. Both are optional: an older daemon, a legacy client or
a payload with no room for them simply has neither, and the device keeps its
normal quota rendering.

`device_lifecycle` describes **only the requesting device**: its identity, the
revision it reported, the revision this daemon's own bundle expects, how the
two compare (`equal`, `different`, `incomparable` or `unknown`), and the derived
update state (`current`, `pending`, `updating`, `failed`, `manual_action`,
`unconfirmed` or `unknown`). It never carries another device's state, name,
address or retry command.

Revisions have no order: two hashes are equal or different, and a different one
is not "older". The **legacy** scheme is the aggregate of the current fixed-path
install and the **slot** scheme is a future per-slot hash; the two are never
compared with each other.

Operational updates are disabled in this build, so `ota_enabled` is false,
`command` and `confirm_attempt_id` are null, and a known mismatch is
`manual_action` with `usb_update_required` rather than a pending update that can
never start. `needs_attention` is true for `failed`, `manual_action` and
`unconfirmed`, and stays true for an earlier unresolved warning until a
supported recovery confirms the app.

`daemon_release` is the cached result of the separate question "has a newer
**daemon** been published", from the registry this installation uses. It is
`disabled` until a release checker exists; it is never reported as `current` on
the strength of not having looked. The device never contacts a registry itself.

Both objects are bounded (2 KiB and 1 KiB) and the whole answer stays inside the
device's 64 KiB limit. If an extension would push the answer past it, the
extensions are omitted and the quota data is kept: a device with no room is told
nothing new.

### App downloads over TLS

```text
GET /api/v1/device-app/manifest
GET /api/v1/device-app/file/<module name>
Authorization: Bearer <shared token>
```

A Presto downloads app updates from these two routes, over TLS on this same
port: the daemon looks at the first byte of each connection, and a TLS
handshake is answered with TLS while everything else stays plain HTTP. Over
TLS, only these two routes answer; every other path is `404`. The certificate
is signed by the daemon's own authority (`dash.db.ota.pem` beside the database)
for the fixed name `presto-ai-dash-ota.invalid`, and the Presto trusts only
that authority, which it received from the setup utility. They serve only the
running daemon's own app, one file per request.

### The status codes

| Code | Meaning |
| --- | --- |
| `200` | The payload |
| `204` | The ingest route accepted a report |
| `400` | A malformed body, a bad `Content-Length`, or a chunked body, which the daemon does not accept |
| `401` | Missing or wrong token |
| `404` | Unknown path, a disabled ingest route, or an unknown card |
| `405` | The wrong method on a known path |
| `413` | The body is too large |
| `429` | Too many requests in a short window |
| `503` | The daemon is already handling as many requests as it allows |

### Limits

The status and admin ports share these, so one slow or greedy client cannot
keep the daemon from answering the Presto:

- A whole request must arrive within 10 seconds, and each write of an answer must finish within 3.
- At most 64 requests are handled at once, and at most 16 from one address. The
  next one is answered `503` and closed at once.
- A body is refused with `413`, before it is read, over 16 KiB on the ingest
  route, 256 bytes on the alerts write and 64 KiB on the admin port.

### The headers a client may send

A request to the status port may carry five extra headers. They say who is
asking and how the device is doing. They change nothing about the answer: the
status payload is the same with or without them, apart from the optional
`device_lifecycle` and `daemon_release` objects described below.

| Header | What it carries |
| --- | --- |
| `X-Presto-Client` | The client's name, at most 32 printable ASCII characters |
| `X-Presto-Revision` | The app revision the client runs |
| `X-Presto-Diag` | The client's own diagnostics, at most 120 characters |
| `X-Presto-Device-ID` | The persistent device identifier: exactly 32 lowercase hexadecimal characters. Status requests only |
| `X-Presto-Lifecycle` | One compact JSON object, at most 1,024 bytes, schema `v: 1`. Status requests only |
| `X-Presto-Time-Nonce` | Exactly 32 lowercase hexadecimal characters. Sent only by a Presto whose clock has restarted at 2021 and needs the date. Status requests only |

A header value that is not printable ASCII, or that is too long, is dropped
rather than partly trusted. The first three are kept in memory only, and the
daemon forgets them when it restarts.

The last two are different: a device that sends a valid identifier and a valid
lifecycle report gets a **durable record**. That record holds the device's name,
its installed scheme and revision, its capabilities, its last report counters
and its update state, so an unresolved problem survives a daemon or container
restart. It holds no diagnostics, address, provider data or credential. A
repeated header, a wrong type, an unknown key or version, or an invalid
cross-field combination refuses the lifecycle report whole: the poll still
answers with the normal quota data and the record is left as it was.

A client without a device identifier is legacy telemetry: a bare 12- or
64-character revision is compared with the expected **legacy** aggregate only,
and is never stored. A repeated poll whose `(boot, seq)` is not higher than the
stored one is `stale` and changes nothing.

A Presto has no clock battery, so every cold start reads January 2021, and it
will not fetch an update until it knows the date: the date is what rejects an
expired certificate. A device in that state sends a fresh random nonce, and the
status answer then carries one extra object:

```json
"clock": {"t": 1793318338, "mac": "<64 lowercase hexadecimal characters>"}
```

`t` is the daemon's own time in Unix seconds, and `mac` is HMAC-SHA256, keyed
with the shared token, over `presto-time-v1|<nonce>|<t>`. The device sets its
clock only if the answer verifies for the nonce it just sent, so an answer
forged on the network, or replayed from an earlier poll, cannot move its clock.
Nothing is stored. A request with no nonce, an invalid one, or more than one
gets no `clock` object, and so does an unauthenticated request.

`X-Presto-Diag` is a list of `key=value` pairs, separated by `;`:

```text
X-Presto-Diag: err=OSError 110;st=conn;n=3;free=41;up=312;rssi=-61;drops=1
```

| Key | What it is | The range the daemon accepts |
| --- | --- | --- |
| `err` | The last fetch failure: the exception's type and, at most, its errno | A name, then at most one errno, 24 characters in all |
| `st` | Where the last fetch stopped | `conn`, `send`, `head`, `body` or `parse` |
| `n` | The failures since the client was switched on | 0 to 999999 |
| `free` | The free heap, in KiB | 0 to 65535 |
| `up` | The seconds since the client started | 0 to 99999999 |
| `rssi` | The Wi-Fi signal, in dBm | -127 to 0 |
| `drops` | The Wi-Fi drops since the client started | 0 to 9999 |

The whole header is at most 120 characters. A pair the daemon does not know,
or whose value is out of range, is left out and the rest of the header is
still used. The value of `err` is never the text of an exception, only its
type and errno, so no address, token or response body can travel in it.

When a client reports a problem, and the problem changes, the daemon writes
one line to its log:

```text
client "presto" reported a fetch problem: err=OSError 110 st=conn n=3
```

A client can only report a failure on a *later* successful poll: the request
that failed never reached the daemon. A device that has never been seen at
all therefore cannot report anything, and the Status tab's "Last seen" column
is the one to read first.

## The admin port

The admin port serves the configuration page and its API.

**It binds to `127.0.0.1` by default.** `admin.host` (`DASH_ADMIN_HOST`) can
change that, but the daemon refuses to start on any other address unless an
explicit admin token is set. In the Docker image the page listens on `0.0.0.0`
*inside* the container, and the example `compose.yaml` publishes it on the
host's loopback address only.

**It answers only to the names it knows.** A request whose `Host` header is not
`127.0.0.1` or `localhost` with the port the page is on, or a name in
`admin.allowed_hosts` (`DASH_ADMIN_ALLOWED_HOSTS`), is refused with `403`. That
is what stops a web page in your browser from driving it through DNS
rebinding. A browser change must also name an `Origin` the daemon knows.

Any other method than the ones below is answered `501`.

| Method | Path | What it does |
| --- | --- | --- |
| GET | `/` | The page itself. No token: it holds no data |
| GET | `/admin.js` | The page's script |
| GET | `/admin.css` | The page's styles |
| GET | `/presto-ai-dash-icon.svg` | The public Watchful Meter product icon, served locally |
| GET | `/favicon.ico` | The public product favicon, served locally |
| GET | `/favicon.svg` | The tightly framed SVG favicon for browser tabs, served locally |
| GET | `/api/admin/sources` | The connector list and the saved instances. Only `has_key` is returned |
| POST | `/api/admin/sources` | Add an instance |
| PUT | `/api/admin/sources/{id}` | Edit, replace or remove the key, enable or disable |
| DELETE | `/api/admin/sources/{id}` | Delete the instance and its key |
| PUT | `/api/admin/source-order` | Save the account order |
| POST | `/api/admin/sources/{id}/claude-export` | Install or remove the Claude status-line export |
| POST | `/api/admin/sources/{id}/ingest-token` | Create or rotate the account's remote token. It is in the answer, once |
| DELETE | `/api/admin/sources/{id}/ingest-token` | Revoke the account's remote token |
| POST | `/api/admin/sources/{id}/codex-login` | Start a Codex sign-in |
| POST | `/api/admin/sources/{id}/codex-logout` | Sign a Codex account out |
| GET | `/api/admin/codex-logins/{id}` | The state of a Codex sign-in in progress |
| POST | `/api/admin/codex-logins/{id}/cancel` | Cancel a Codex sign-in in progress |
| GET | `/api/admin/ingest` | The remote reports switch, whether the environment locks it, and the collector file with its hash |
| PUT | `/api/admin/ingest` | Turn remote reports on or off |
| GET | `/api/admin/collector` | Download the collector script |
| GET | `/api/admin/display` | The Home display settings, and whether the environment locks them |
| PUT | `/api/admin/display` | Save the Home display settings |
| GET | `/api/admin/demo` | The demo switch, and whether `DASH_DEMO` locks it |
| PUT | `/api/admin/demo` | Turn demo mode on or off |
| GET | `/api/admin/alerts` | The alert settings and whether it is quiet now |
| PUT | `/api/admin/alerts` | Save the alert settings |
| GET | `/api/admin/sources/{id}/alerts` | One service's alert settings and the resolved values |
| PUT | `/api/admin/sources/{id}/alerts` | Save one service's alert settings |
| GET | `/api/admin/status` | The Status tab. Read-only |
| GET | `/api/admin/devices` | Every Presto the daemon keeps a durable record for, with the count that needs attention. Read-only |
| GET | `/api/admin/devices/{id}` | One device: identity, reported and expected revision, update state, and its newest attempts. Read-only |
| POST | `/api/admin/devices/{id}/retry` | Authorize a fresh bounded update budget. Answers `409 ota_not_enabled` while the updater is not enabled |
| POST | `/api/admin/devices/{id}/confirm-recovery` | Ask for a fresh confirmed check-in after a manual repair. Answers `409 ota_not_enabled` while the updater is not enabled |
| POST | `/api/admin/releases/check` | Queue a background check for a newer release and return at once: 202 queued or already running, 429 with `Retry-After` if one started within the last minute, 409 when this installation is never checked. It never contacts a registry on the request |
| GET | `/api/admin/releases` | The cached daemon-release result and this installation's own update commands. It contacts no registry. Read-only |
| GET | `/api/admin/logs` | The daemon's recent log lines, newest first, for the Status tab: `limit` (up to 2,000), `level` and `q` filters. Read-only |
| GET | `/api/admin/config` | The effective configuration and where each value came from. Read-only |
| POST | `/api/admin/session` | Exchange the admin token for a seven-day browser session |
| GET | `/api/admin/session` | Restore a valid cookie session and get the CSRF token |
| DELETE | `/api/admin/session` | Revoke the current session |
| DELETE | `/api/admin/sessions` | Revoke every browser session |
| GET | `/api/admin/presto-setup` | What **Set up a Presto** may offer: the bundled app revision, the release, and suggested daemon addresses |
| POST | `/api/admin/presto-setup` | Prepare the two setup downloads and a fresh unlock code. The code is in the answer, once |
| GET | `/api/admin/presto-setup/{id}/script` | Download `presto_setup.py` for one prepared handoff |
| GET | `/api/admin/presto-setup/{id}/handoff` | Download `presto-setup.enc`, the encrypted configuration |
| DELETE | `/api/admin/presto-setup/{id}` | Forget a prepared handoff. A copy already downloaded stays valid until it is used |

Except for the page's own files, a request needs the admin bearer token or a
valid browser session. A cookie-authenticated change also needs the exact page
`Origin` and the `X-CSRF-Token` header.

The actions that touch a provider account run only from this page, when you
press the button: installing and removing the Claude export, and the Codex
sign-in and sign-out. Polling and the Presto never call them.

### The setup downloads

**Set up a Presto** serves two files for the build that is actually running:
`presto_setup.py`, a standalone installer carrying the public Presto app
files and their SHA-256 hashes, and `presto-setup.enc`, that build's daemon
address, expected app revision and persistent shared device token. The
encrypted file uses a versioned envelope (HKDF-SHA256 into AES-256-GCM) with a
fresh salt and nonce. The **setup unlock code** is generated from 160 random
bits for each handoff, returned once in the `POST` answer, and is in neither
download. The admin token is never included.

These routes need the admin token or a browser session, and every response is
`Cache-Control: no-store`. Deleting the encrypted file, losing the code or
preparing another handoff does **not** revoke the shared device token: rotate
`daemon.shared_token` and update every device to do that. Unlock codes are
never part of the fixture payloads in this documentation.

## The tokens

| Token | Who has it | What it can do |
| --- | --- | --- |
| The shared token | The Presto, in its own config file | Read the status payload, and set one service's alerts |
| The admin token | You, from the startup log | Everything on the admin port |
| A remote token | A collector on another machine | Write readings for one account, and nothing else |

A bearer token that does not match is answered `401` after a pause of a
quarter of a second, so one connection cannot guess quickly; a correct token
is never delayed. The daemon logs the address of the client (never the token)
as a warning, which also appears in the admin page's Problems list. It writes
one line for a client's first failure in a minute and then one at the 10th,
100th and 1,000th, so a guessing run is visible without flooding the log.

The shared token is a bearer token with no identity and no expiry, and it
travels over plain HTTP. On an untrusted network, put the daemon behind a
reverse proxy with TLS or use a private network such as Tailscale.

## The documentation site

The [documentation site](../index.md) you are reading is published on GitHub
Pages. It is static files built from
Markdown. It uses Google Analytics to count page views, asks no other host for
anything in a current browser, and knows nothing about your installation.
[Privacy](../privacy/index.md#the-documentation-site) has the details and the
one old-browser exception.

## See also

- [Configuration keys and environment variables](configuration.md)
- [The status payload and its size budget](status-payload.md)
- [Privacy](../privacy/index.md)
