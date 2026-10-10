# Future Features

Deliberately out of scope for v0. Recorded here so the boundary is explicit and
so nobody has to re-derive it.

## Deployment roadmap

| Target | Direction |
| --- | --- |
| v0 | Normal Python daemon: source run or pip/pipx; target command `presto-ai-dash-daemon` |
| v0.1 | Optional Docker image for server/headless use, especially `toontown-int-srv1` |
| v0.2 | Optional separate `presto-ai-dash-tray` runner/wrapper |
| v1 | Packaged binaries/installers if the project proves useful |

Today's installed command is `presto-ai-dash`; the new command names and these
packaging targets are unimplemented. Docker is an optional mode. Headless
Python use must remain first-class on Windows 10/11, macOS, and Linux.

## Optional tray (v0.2 target)

Keep the tray outside the daemon core. Windows/macOS are primary; Linux is
best-effort because desktop environments differ. `pystray` is the likely
candidate; `rumps` is optional later macOS polish, never required.

The small menu contains status, open dashboard/status endpoint, open config
folder, copy Presto endpoint URL, restart daemon, stop daemon, and about.
It must not grow into a second full UI.

## Desktop telemetry and display refinements (unscheduled)

| Item | Notes |
| --- | --- |
| Desktop Ollama collector | The only clean way to measure direct Ollama usage. Writes JSON the daemon reads. |
| Basic local Ollama shim | Alternative to the collector: point clients at the shim, it forwards and records. |
| Better trend indicators | Still no charting. Just clearer up/down/flat. |
| Manual refresh action | A touch gesture that forces an immediate poll. |
| Configurable polling interval | Already in config; expose it on the device. |
| Simple web preview | Render the same status JSON in a browser for debugging. |

## Providers and messaging (unscheduled)

| Item | Notes |
| --- | --- |
| OpenCode JSON source | OpenCode or a helper writes JSON, the daemon reads it, the Presto shows a card. Same pattern as the Ollama samples file. |
| Ollama Cloud research | Research before implementation. No brittle scraping as a default. |
| OpenAI usage source | Official usage/cost/status APIs only. Read-only or restricted keys. No paid test calls unless explicitly enabled. No dashboard scraping. |
| Codex / Claude sources | Research supported or user-exported telemetry first; placeholders only. |
| MQTT output/input | Publish status, accept commands. |
| Multiple Presto devices | The daemon already serves many clients; this is about per-device config. |

## Other expansion (unscheduled)

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
