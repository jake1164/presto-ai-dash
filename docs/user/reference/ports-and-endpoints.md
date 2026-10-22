# Ports and endpoints

The daemon serves two ports, and the device uses one of them. The
documentation site is a third thing entirely: it is static files on GitHub
Pages and never talks to your daemon.

## The ports

| Port | Default | Who uses it | Bind address |
| --- | --- | --- | --- |
| Status | 8787 | The Presto, and the host-side preview | `daemon.host`, `0.0.0.0` by default |
| Admin | 8788 | Your browser | `127.0.0.1` only |
| Preview | 8790 | Your browser, on the host-side preview tool | `127.0.0.1` only |

The preview port is not part of the daemon. It belongs to the development
tool and is never started by the daemon.

## The status port

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
| `401` | Missing or wrong token |
| `404` | Unknown path, a disabled ingest route, or an unknown card |
| `405` | The wrong method on a known path |
| `413` | The body is too large |
| `429` | Too many requests in a short window |

The ingest route answers `204` when it accepts a report.

## The admin port

The admin port serves the configuration page and its API. It binds to
`127.0.0.1` and refuses a request whose `Host` header is anything other
than `127.0.0.1` or `localhost`.

| Method | Path | What it does |
| --- | --- | --- |
| GET | `/api/admin/sources` | The connector list and the saved instances. Only `has_key` is returned |
| POST | `/api/admin/sources` | Add an instance |
| PUT | `/api/admin/sources/{id}` | Edit, replace or remove the key, enable or disable |
| DELETE | `/api/admin/sources/{id}` | Delete the instance and its key |
| PUT | `/api/admin/source-order` | Save the account order |
| GET | `/api/admin/display` | The Home display settings, and whether the environment locks them |
| PUT | `/api/admin/display` | Save the Home display settings |
| GET | `/api/admin/alerts` | The alert settings and whether it is quiet now |
| PUT | `/api/admin/alerts` | Save the alert settings |
| GET | `/api/admin/sources/{id}/alerts` | One service's alert settings and the resolved values |
| PUT | `/api/admin/sources/{id}/alerts` | Save one service's alert settings |
| GET | `/api/admin/status` | The Status tab. Read-only |
| GET | `/api/admin/config` | The effective configuration and where each value came from. Read-only |
| POST | `/api/admin/session` | Exchange the admin token for a seven-day browser session |
| GET | `/api/admin/session` | Restore a valid cookie session and get the CSRF token |
| DELETE | `/api/admin/session` | Revoke the current session |

A request needs the admin bearer token or a valid browser session. A
cookie-authenticated change also needs the exact page `Origin` and the
`X-CSRF-Token` header.

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
Markdown. It loads nothing from a third party, sends no analytics, and
knows nothing about your installation.

## See also

- [Configuration keys and environment variables](configuration.md)
- [The status payload and its size budget](status-payload.md)
- [Privacy](../privacy/index.md)
