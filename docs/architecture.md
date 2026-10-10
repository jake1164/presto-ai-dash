# Architecture

## Shape

```text
                 +-------------------+
                 |  Pimoroni Presto  |
                 |  MicroPython app  |
                 +-------------------+
                   |   GET /api/v1/status
                   |   Authorization: Bearer <token>
                   v
                 +-------------------+
                 |   Python daemon   |
                 +-------------------+
                   |        |         |
              collect   store     serve
                   |        |         |
                   v        v         v
              +--------+ +-------+ +--------+
              | sources| |SQLite | | HTTP   |
              +--------+ +-------+ +--------+
```

## Daemon responsibilities

| Responsibility | Module |
| --- | --- |
| Load and validate config | `config.py` |
| Define the shared vocabulary | `models.py` |
| Talk to sources over HTTP | `http_client.py` |
| Collect from each source | `sources/` |
| Persist snapshots and alerts | `storage.py` |
| Decide severity and trends | `alerts.py` |
| Build the status payload | `dashboard.py` |
| Serve HTTP with auth | `api.py` |
| Wire it together and run | `app.py` |

## Collection model

One daemon thread per enabled source. Each thread loops:

```text
sleep(poll_interval)
  -> source.collect()          # returns a SourceSnapshot
  -> storage.save_snapshot()
  -> alerts.evaluate(snapshot) # may raise an Alert
```

`GET /api/v1/status` never blocks on a source. It reads the most recent
snapshot per source from SQLite and renders immediately. A slow or dead source
therefore shows a stale card, not a hung endpoint.

A source that raises is caught, logged, and recorded as an `offline` snapshot
with the error text. The daemon does not exit because a source failed.

## The status vocabulary

Canonical values, most severe last:

```text
ok  <  active  <  warning  <  near_limit  <  rate_limited  <  critical  <  offline
```

`normal` is accepted as an input alias for `ok` (the project plan uses both
spellings) and is normalised on the way in.

`overall_status` is the most severe status across all cards.

## The API

One endpoint.

```text
GET /api/v1/status
Authorization: Bearer <shared-token>
```

```json
{
  "app": "presto-ai-dash",
  "version": "0.1.0",
  "updated_at": "2026-10-09T11:00:00-04:00",
  "overall_status": "ok",
  "cards": [
    {
      "id": "dash",
      "title": "AI Dash",
      "status": "ok",
      "primary": "128k tokens today",
      "secondary": "Ollama active · LiteLLM OK",
      "detail": ["LiteLLM: 42 requests today", "Alerts: none"],
      "accent": "green",
      "alert": null
    }
  ]
}
```

`accent` is one of `green | blue | yellow | red | purple | white | grey` and
maps directly onto an LED colour. `alert` is `null` or an object with
`severity` and `message`.

Unauthenticated or badly authenticated requests get `401`. Unknown paths get
`404`. There is no write API in v0.

## Storage

Three tables, as the plan specifies.

```text
snapshots   one row per source per collection
alerts      one row per raised alert
settings    daemon-owned key/value state
```

Retention defaults to 72 hours and is swept on a timer, not on every write.

## Presto app

```text
main.py     the only file that imports presto/machine
net.py      HTTP fetch (requests wrapper, injectable for tests)
cards.py    card model + status -> accent mapping        (pure)
gestures.py tap / swipe / long-press state machine       (pure)
alerts.py   status -> LED colour + sound cue             (pure)
ui.py       PicoGraphics drawing                         (hardware)
```

The split exists so that everything decidable without hardware is unit-tested
on a development machine, and the hardware layer stays a thin adapter. See
`docs/limitations.md`.

## Security posture

- The daemon binds to the LAN by default and is never meant to be public.
- `/api/v1/status` requires the shared token. If none is configured, one is
  generated at startup and logged once.
- Provider secrets live in the daemon config or environment variables, never on
  the Presto.
- The daemon never makes a paid model call. LiteLLM's `GET /health` endpoint
  *does* make test calls and costs tokens, so it is not used.
