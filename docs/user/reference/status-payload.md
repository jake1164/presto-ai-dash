# The status payload and its size budget

`GET /api/v1/status` returns one JSON object. The Presto reads it; so do
the host-side preview and anything else you point at it.

## The whole payload

```json
{
  "app": "presto-ai-dash",
  "version": "0.49.0",
  "overall_status": "ok",
  "updated_at": "2026-10-20T12:00:00+00:00",
  "card_order": "accounts",
  "quota_schema_version": 1,
  "cards": [ ... ],
  "sources": [ ... ],
  "display": {"overflow_rows": 1},
  "alerts": {"quiet": false, "sound": false, "pop_to_top": true, "enabled": true,
             "mute_hours": 4}
}
```

| Key | What it is |
| --- | --- |
| `app`, `version` | The daemon's name and version |
| `overall_status` | The worst status across the services that alert |
| `updated_at` | The moment of the newest reading in the payload |
| `card_order` | `accounts`, so the device keeps the daemon's order |
| `quota_schema_version` | The version of the quota contract |
| `cards` | The Home card, then one card per enabled account |
| `sources` | One entry per enabled account, with its own measurements |
| `display` | The Home display settings |
| `alerts` | The quiet flag, the sound switch, the pop-to-top switch, the master switch and the mute length in hours |

## One card

```json
{
  "id": "claude",
  "title": "Claude",
  "status": "ok",
  "primary": "5h 12%",
  "secondary": "Weekly 66%",
  "detail": ["Quota via export, 2 h old"],
  "accent": "green",
  "alert": null
}
```

`accent` is one of `green`, `blue`, `yellow`, `red`, `purple`, `white` or
`grey`, and maps directly onto an LED colour. `alert` is `null` or an object
with a `severity` and a `message`.

A card may also carry `tiles`: a short list of `{"label", "value", "tone"}`
items. The label is at most 8 ASCII characters, the value at most 10, and
the tone is one of `normal`, `good`, `warn`, `bad` or `dim`. A value the
report does not carry is left out, never shown as zero.

## One source

```json
{
  "id": "claude",
  "type": "claude",
  "name": "Claude",
  "status": "ok",
  "quota": {
    "windows": [
      {"label": "5h", "used_percent": 12, "state": "available",
       "period_minutes": 300, "resets_at": "2026-10-20T15:00:00+00:00",
       "observed_at": "2026-10-20T12:00:00+00:00"}
    ],
    "hint": "In use - quota 2h old",
    "reset_credits": {"available": 3, "expires_at": ["..."]}
  },
  "metrics": {},
  "alerts": true,
  "marks": [80, 95]
}
```

- `quota` is present for a quota source, and absent otherwise.
- `windows[*].state` is `available`, `unavailable`, `unlimited` or `stale`.
- `hint` is the short ASCII line, at most 28 characters, that says why a
  number is missing or old.
- `marks` is the pair of percentages `[warning, critical]` the service really
  uses, so the device can draw a tick on its bars. The device draws them and
  decides nothing.
- `alerts` is whether this service alerts at all.

## The device view

The daemon sends the device a **slim** view of the payload: only the fields
the device actually reads, and each quota report once.

The full payload carries the quota report twice - under `metrics.quota` and
under `quota` - because the admin page uses the first and the device uses
the second. The device view drops the duplicate, and drops the fields only
the admin page needs.

The whitelist lives in `daemon/src/presto_ai_dash/dashboard.py` as a set of
`DEVICE_*_KEYS` constants, and a test proves that it still covers every key
the device code reads. If you add a key to the device, add it to the
whitelist, or the guard test fails.

## The size budget

The Presto has very little memory, and it parses the whole response at
once. A payload that grows is a payload the device may fail to parse, and
the screen then says "Daemon offline" even though the daemon is fine.

So the budget is enforced:

| Budget | Value | What it covers |
| --- | --- | --- |
| One poll response | 5000 bytes | The device summary, with six accounts and both Claude readers on |
| Per account | 1000 bytes | The average per account **within that one response**, so one account cannot eat the whole of it |
| One card's detail | 8 KiB | Its own limit per request to `GET /api/v1/cards/<id>` |

These are limits on **one message**, not on a total. The Presto polls every
15 seconds and fetches a card's detail when the card is opened, so many
responses arrive over time, and there is no limit on how many. What the device
cannot do is parse one large response, which is what the budget protects.

A test fails the build when a poll response exceeds either limit, so it cannot
quietly grow back. The demo is held to the same rule: every message it sends
(each step of its scenario, and each card's detail) is tested against these
limits. The device accepts up to 64 KiB, but it parses that in a few hundred
kilobytes of RAM, which is the number that matters.

When a source needs more detail than fits, the answer is the lazy card
detail: the summary is polled, and a card's detail lines are fetched only
when the card is opened.

## The two shapes

| Request | What you get |
| --- | --- |
| `GET /api/v1/status` | The whole payload, exactly as above |
| `GET /api/v1/status?cards=lazy` | The same payload without the cards' `detail` lines |
| `GET /api/v1/cards/<id>` | `{"id": ..., "tiles": [...], "lines": [...]}` for one card |

The old status response is unchanged for a client that does not ask for lazy
cards.

## See also

- [Ports and endpoints](ports-and-endpoints.md)
- [How a source works](../developers/how-a-source-works.md)
