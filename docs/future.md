# Future

Deliberately out of scope for v0. Recorded here so the boundary is explicit and
so nobody has to re-derive it.

## v0.2

| Item | Notes |
| --- | --- |
| Desktop Ollama collector | The only clean way to measure direct Ollama usage. Writes JSON the daemon reads. |
| Basic local Ollama shim | Alternative to the collector: point clients at the shim, it forwards and records. |
| Better trend indicators | Still no charting. Just clearer up/down/flat. |
| Manual refresh action | A touch gesture that forces an immediate poll. |
| Configurable polling interval | Already in config; expose it on the device. |
| Simple web preview | Render the same status JSON in a browser for debugging. |

## v0.3

| Item | Notes |
| --- | --- |
| OpenCode JSON source | OpenCode or a helper writes JSON, the daemon reads it, the Presto shows a card. Same pattern as the Ollama samples file. |
| Ollama Cloud research | Research before implementation. No brittle scraping as a default. |
| OpenAI usage source | Official usage/cost/status APIs only. Read-only or restricted keys. No paid test calls unless explicitly enabled. No dashboard scraping. |
| MQTT output/input | Publish status, accept commands. |
| Multiple Presto devices | The daemon already serves many clients; this is about per-device config. |

## v1.0

| Item | Notes |
| --- | --- |
| External source package interface | Only once there is more than one third-party source to justify it. |
| Provider icon support | Plugin authors supply their own assets and are responsible for rights. |
| Desktop browser opener helper | Open the relevant dashboard from the desk. |
| Prometheus export | Out of scope until someone actually wants it. |
| Advanced alert rules | v0 has thresholds, not a rule engine. |
| Multi-host collector model | One collector per host, many sources. |

## Explicitly not planned

- Provider logo packs, mascots, fonts or artwork in the core repository.
- A plugin marketplace or external package system.
- Per-project billing breakdown.
- A cloud-hosted dashboard or user accounts.
- Claude Code / Codex telemetry via credential scraping or reverse-engineering
  private CLI state. Only official or user-exported paths.

## The rule that keeps this small

> This is a small Presto-first desk display, not a general AI observability
> platform.

If a proposed feature does not make the Presto more useful at a glance, it
belongs somewhere else.
