> Historical planning document (written about 2026-10-09, with later updates to 2026-10-20); the current state is in [docs/milestones.md](milestones.md) and the [documentation site](https://jake1164.github.io/presto-ai-dash/).
>
> Corrections (2026-10-21):
>
> - Subscription quota monitoring, described below as "not implemented yet", is built: Codex, Claude, OpenCode Go and Ollama Cloud (M7 to M11).
> - The optional Docker image ("v0.1" below) is built. The tray runner, binaries and the new command names are still unimplemented.
> - "Simple web preview" below exists: `tools/presto_preview/live.py` runs the real app against an emulated display in a browser.
> - "Multiple Presto devices": the daemon already lists every client that polls it on the admin page's Status tab; per-device tokens and settings are still not built.

# Future Features

Deliberately out of scope for v0. Recorded here so the boundary is explicit and
so nobody has to re-derive it.

OpenAI/Codex, Claude, and OpenCode Go **subscription quota monitoring** moved
into the active M6–M11 plan on 2026-10-11, ahead of alert polish and release.
It is not implemented yet. See [subscription monitoring](subscription-monitoring.md)
for supported reporting candidates, unresolved readiness checks, reusable
accounts/windows, and Presto acceptance. OpenCode means its own Go subscription.
The deferred API billing and application features below are separate work.

## Deployment roadmap

| Target | Direction |
| --- | --- |
| v0 | Normal Python daemon: source run or pip/pipx; target command `presto-ai-dash-daemon` |
| v0.1 | Optional Docker image for server/headless use, especially `home-server` |
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

## Application usage monitoring

Planned as a separate checkpoint after M5, outside the current v0 milestones.
This does not expand M3 or require application-specific storage now.

Monitor multiple configured applications, independently of which model or
provider they use. A book app that verifies titles and recommends books is one
example, not a special case in the daemon or UI.

- Each application has a stable `app_id` and configurable display name.
  Provider/model changes and credential rotation must preserve its identity
  and history. Adding another application requires configuration and telemetry
  attribution setup, not new application-specific code.
- Collection adapters map measured usage to that identity. Start with several
  applications routed through LiteLLM, using a dedicated virtual key per app.
  Optional operation labels such as `book-verify` and `book-recommend` describe
  work within an app; they do not replace the app identity.
- A later direct-call adapter can read summaries exported by an app wrapper
  for Ollama or another provider. It feeds the same normalized application
  metrics. Health polling alone cannot reconstruct direct-call usage.
- Keep provider/gateway totals and application views distinct. An app's usage
  is a subset of the gateway total; never add those two together. A request
  matched by both a key and a tag counts once. Proxy and direct reporting of
  the same request require an explicit ownership/deduplication rule.
- Provider credentials and attribution configuration stay on the daemon.
  Keys or prompts must not appear in the Presto payload; request contents are
  unnecessary for this display.

Reusable metrics are request count, input/output tokens, reported spend when
available, errors, latency, last activity, and current/recent model. Show
unavailable values honestly; missing telemetry is not zero usage. Collection
freshness is separate from last application activity.

Optional daily/weekly application budgets can drive the stacked usage bars.
Label these as app-defined budgets, with a configured metric/unit, period and
reset/timezone convention; they are not provider account quotas. Weekly totals
need a complete seven-day upstream window or retained daily aggregates. M3's
72-hour snapshot retention alone cannot support them. Never sum successive
cumulative counter snapshots as if they were individual requests.

The future Presto view uses a scrolling Apps summary and one reusable detail
renderer for any configured app. It should cope with more rows than fit on
screen and preserve horizontal card navigation. API/config shapes and any
additional storage are to be specified in that checkpoint's readiness review.

First checkpoint acceptance: at least two configured apps have isolated usage
and shared rendering; adding a third needs no code changes; key rotation and
provider changes retain app identity; unknown metrics and duplicate attribution
are tested. Direct-call reporting is a later adapter, not a prerequisite.

Before implementation, verify the installed LiteLLM version, database-backed
usage availability, reporting permissions, time windows, and supported filters.
The intended approach follows its official [virtual-key documentation](https://docs.litellm.ai/docs/proxy/virtual_keys),
[request tags](https://docs.litellm.ai/docs/proxy/request_tags), and
[cost tracking](https://docs.litellm.ai/docs/proxy/cost_tracking).

## Providers and messaging (unscheduled)

| Item | Notes |
| --- | --- |
| Ollama Cloud credit allowance reporting | Activity adapter is implemented in the [active cloud checkpoint](quotas.md); monthly balance/allowance remains unavailable. No scraping or guessed percentages. |
| Direct OpenAI / Anthropic API billing sources | Separate from the prioritized subscription quotas. Official usage/cost APIs only, restricted keys where available, and no paid test calls or dashboard scraping. |
| Other ChatGPT subscription limits | Codex quota reporting does not establish coverage of every ChatGPT model/message limit; research separately if requested. |
| OpenCode application/session telemetry | Separate from Go subscription allowances. A supported helper/export can report app activity later; no deep analytics parser. |
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
- Invoice generation, billing reconciliation, and chargeback accounting.
  Configured application usage summaries above are a bounded display feature.
- A cloud-hosted dashboard or user accounts.
- Claude Code / Codex telemetry via credential scraping or reverse-engineering
  private CLI state. Only official or user-exported paths.

## The rule that keeps this small

> This is a small Presto-first desk display, not a general AI observability
> platform.

If a proposed feature does not make the Presto more useful at a glance, it
belongs somewhere else.
