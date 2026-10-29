> Public, user-facing version: <https://jake1164.github.io/presto-ai-dash/developers/architecture/>. This file is the internal reference and may go into more detail.
>
> Corrections (2026-10-21):
>
> - There is a write API: `POST /api/v1/ingest` (pushed readings, off by default) and `POST /api/v1/alerts/<id>` (one service's alerts on/off, from the Presto). There are also `GET /api/v1/cards/<id>`, `GET /api/v1/status?cards=lazy` and the unauthenticated `GET /healthz`.
> - Alert evaluation is built (M12): `alert_policy.py` maps percentages to statuses; the "draft" and "planned beyond M2" notes below are historical.
> - Seven source types are implemented, including Codex, Claude, OpenCode Go and Ollama Cloud; remote collectors push readings over the ingest route.
> - The optional Docker image is built; the tray runner and binaries are not.
> - The admin listener binds to loopback by default and can be bound elsewhere with an allowed-host list. The Presto app also has `popup.py` (pop-to-top rules).

# Architecture

## Runtime Architecture

The Presto is the physical dashboard/controller: touchscreen card rendering,
swipe/tap navigation, LEDs, optional sound, and local HTTP polling over Wi-Fi.
The desktop/server daemon is the brain: source collection, provider secrets,
normalization, SQLite history, and `/api/v1/status`.

M1 runs static data entirely on the device. M2 added the daemon API and a
demo source; M3 added internal SQLite history, UTC ordering, alert observations,
and 72-hour retention. M4/M5 add Ollama health and LiteLLM routed usage.
The connected Presto app polls with cooperative asyncio tasks. On-device Wi-Fi,
authenticated polling, and recovery are verified in `milestones.md`.
Trend calculations remain later work. The device's built-in static demo is gone:
sample data is served by the daemon's demo mode instead.

The simulator is a host adapter around the same `presto/` App and renderer.
`tools/presto_bundle.py` is the shared source/deployment inventory; no separate
simulator UI exists. The explicit `tools/presto_device.py` USB command copies
and verifies those app bytes on the device while preserving its configuration
and firmware. PSL reports its source revision and the dated last USB check,
so working-source changes after deployment are visible. Code parity does not
assert identical Wi-Fi, memory, CPU, sound or LED hardware behavior.

### Device setup and build identity

Installing a device must not need a checkout. The authenticated admin page's
`/api/admin/presto-setup` routes serve two artifacts for the build that is
actually running:

- `presto_setup.py`, rendered by `device_setup.render_setup_script` from the
template in `presto_ai_dash/setup/` plus the verified public bundle, so it
imports nothing from this package and runs on a computer with only Python,
`cryptography` and `mpremote`;
- `presto-setup.enc`, a versioned envelope (`HKDF-SHA256` from a 160-bit
one-time code into `AES-256-GCM`, fresh salt and nonce) holding the reachable
daemon address, the expected app revision and the persistent shared device
token. The unlock code is displayed once in the page and is in neither
download; the admin token is never included.

`device_install.app_snapshot()` is the single source of the app bytes: a
release wheel and the container image both carry `presto_ai_dash/presto_app/`
staged through `tools/presto_bundle.py` (`tools/build_release.py` and
`docker/stage_app.py`), and a source checkout falls back to the same inventory
plus `presto/`. `release.release_version()` reports the running artifact's
identity: `DASH_RELEASE_VERSION` when the image was built from a tag (so an RC
such as `0.50.0-rc3` is not reported as `0.50.0`), otherwise the package
version.

The layout on the device is unchanged: `/ai_dash.py`, the modules in
`/ai_dash_files/`, the generated `revision.txt`, and the private
`config.py`/`secrets.py` outside the replaceable app files. The revision is the
**legacy-v1** aggregate that includes the launcher, displayed as its first 12
hexadecimal characters. A stable random `DEVICE_ID` is written into the private
config so later work can group telemetry without relying on a name or address.
The gated A/B bootstrap and its separate `slot-v1` revision are not implemented;
moving an existing device to them is a deliberate one-time USB migration using
this setup utility, not a Wi-Fi update.

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

This table includes later modules. Provider HTTP clients are active for M4/M5;
the separate daemon alert-rule module remains a draft. M3 storage records
observations reported by active sources and does not calculate alert rules.

| Responsibility | Module |
| --- | --- |
| Load and validate config | `config.py` |
| Edit source instances and apply changes | `management.py` |
| Encrypt provider credentials and protect the master key | `credentials.py` |
| Serve the local configuration page and admin API | `admin.py`, `web/` |
| Define the shared vocabulary | `models.py` |
| Talk to sources over HTTP | `http_client.py` |
| Collect from each source | `sources/` |
| Persist snapshots and alerts | `storage.py` |
| Decide severity and trends | `alerts.py` |
| Build the status payload | `dashboard.py` |
| Serve HTTP with auth | `api.py` |
| Wire it together and run | `app.py` |

## Collection model

Legacy TOML mode uses one daemon thread per enabled source. Each thread loops:

```text
sleep(poll_interval)
  -> source.collect()          # returns a SourceSnapshot
  -> storage.save_snapshot()
  -> alerts.evaluate(snapshot) # may raise an Alert
```

The alert-evaluation step is planned beyond M2.

After the first admin start, SQLite source instances become authoritative.
The managed collector probes enabled instances concurrently (up to four at a
time), caches unchanged source objects, and wakes when configuration changes.
Each observation is checked against the instance revision before storage;
results from edited or deleted configurations are discarded. Status reads
exclude disabled/deleted instances while retaining their history for cleanup.
See [configuration and encryption](configuration.md).

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
      "secondary": "Ollama active - LiteLLM OK",
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

M3 uses `snapshots` and `alerts`:

```text
snapshots   one row per source per collection
alerts      one observation per snapshot and distinct severity/message pair
```

Existing M2 databases migrate transactionally. Snapshot times are stored as
fixed-width UTC; alert observation time comes from the parent snapshot. History
queries are bounded and internal. Retention defaults to 72 hours and is swept
at startup and every 300 seconds. A settings table and trend endpoints are not
part of M3.

## Deployment Model

Normal Python 3.11+ first: source run or pip/pipx installation. The core
targets Windows 10/11, macOS, and Linux with SQLite, TOML, and `pathlib` paths.
Headless use is first-class; no systemd-only or Windows-only service dependency.
Current execution has been checked on Windows; other OS checks remain to do.

The current console command is `presto-ai-dash`; the planned v0 name is
`presto-ai-dash-daemon`. Optional deployment targets are a Docker image in
v0.1, a separate `presto-ai-dash-tray` wrapper in v0.2, and binaries/installers
in v1 if useful. These are unimplemented; Docker must not be the only mode.
See [Future Features](future.md) for the tray boundary.

## Topology and Telemetry Limits

```text
home-server -> LiteLLM -> desktop-pc -> Ollama
desktop-pc -> Ollama directly
Presto -> HTTP daemon on home-server
```

LiteLLM measures only traffic routed through it. Full direct Ollama usage
needs a future desktop collector, shim, wrapper, or voluntary reporting;
polling health cannot reconstruct request history. An optional future
`desktop-pc` helper reports those samples to the server daemon.

## Scope / Non-goals

Implemented source types are `ollama`, `litellm`, `opencode_go`, `ollama_cloud`,
`codex`, and `claude`; the real
local/gateway integrations were delivered in M4/M5. The active M6–M11 plan
adds subscription quota monitoring for OpenAI/Codex, Claude, and OpenCode Go,
subject to verified reporting paths. Multiple configured accounts share a quota
window contract rather than fixed daily/weekly fields. See
[subscription monitoring](subscription-monitoring.md) for readiness and boundaries.

Direct API billing, Ollama Cloud, OpenCode session analytics, MQTT, multi-host
collectors, desktop browser helpers, external plugins, Prometheus/Grafana,
advanced alert rules, and installers remain separate future work.

## Presto app

```text
main.py     the only file that imports presto/machine
net.py      HTTP fetch (requests wrapper, injectable for tests)
cards.py    card model + status -> accent mapping        (pure)
home.py     static quotas, live source rows, scrolling    (pure)
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
- In managed mode, provider secrets are authenticated ciphertext in SQLite.
  The master key is outside SQLite, protected with Windows DPAPI or an
  owner-only Unix file; an externally managed environment key is supported.
  Legacy TOML/environment keys are imported once. No provider secrets go to
  the Presto, status endpoint, or admin read responses.
- Configuration uses a separate admin bearer token and loopback-only listener,
  with Host/Origin validation. The device token cannot edit sources.
- The daemon never makes a paid model call. LiteLLM's `GET /health` endpoint
  *does* make test calls and costs tokens, so it is not used.
