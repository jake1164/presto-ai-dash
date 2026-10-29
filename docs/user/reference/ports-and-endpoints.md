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

- A whole request must arrive within 10 seconds, and each read within 3.
- At most 64 requests are handled at once, and at most 16 from one address. The
  next one is answered `503` and closed at once.
- A body is refused with `413`, before it is read, over 16 KiB on the ingest
  route, 256 bytes on the alerts write and 64 KiB on the admin port.

### The headers a client may send

A request to the status port may carry three extra headers. They say who is
asking and how the device is doing. They change nothing about the answer: the
status payload is the same with or without them.

| Header | What it carries |
| --- | --- |
| `X-Presto-Client` | The client's name, at most 32 printable ASCII characters |
| `X-Presto-Revision` | The app revision the client runs |
| `X-Presto-Diag` | The client's own diagnostics, at most 120 characters |

A header value that is not printable ASCII, or that is too long, is dropped
rather than partly trusted. Nothing about a client is stored: the daemon keeps
all three in memory only, and forgets them when it restarts.

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
| GET | `/api/admin/logs` | The daemon's recent log lines, newest first, for the Status tab: `limit` (up to 2,000), `level` and `q` filters. Read-only |
| GET | `/api/admin/config` | The effective configuration and where each value came from. Read-only |
| POST | `/api/admin/session` | Exchange the admin token for a seven-day browser session |
| GET | `/api/admin/session` | Restore a valid cookie session and get the CSRF token |
| DELETE | `/api/admin/session` | Revoke the current session |
| DELETE | `/api/admin/sessions` | Revoke every browser session |

Except for the page's own files, a request needs the admin bearer token or a
valid browser session. A cookie-authenticated change also needs the exact page
`Origin` and the `X-CSRF-Token` header.

The actions that touch a provider account run only from this page, when you
press the button: installing and removing the Claude export, and the Codex
sign-in and sign-out. Polling and the Presto never call them.

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

The site you are reading is published on GitHub Pages at
<https://jake1164.github.io/presto-ai-dash/>. It is static files built from
Markdown. It uses Google Analytics to count page views, asks no other host for
anything in a current browser, and knows nothing about your installation.
[Privacy](../privacy/index.md#the-documentation-site) has the details and the
one old-browser exception.

## See also

- [Configuration keys and environment variables](configuration.md)
- [The status payload and its size budget](status-payload.md)
- [Privacy](../privacy/index.md)
