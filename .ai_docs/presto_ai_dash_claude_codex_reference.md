# Presto AI Dash
## Claude & Codex Usage, Activity, and Reset Countdown — Agent Reference

**Date:** October 17, 2026  
**Purpose:** Implementation reference for the local development agent.  
**Status:** Design and integration guidance; **not** a claim that the repository has these features.

> **Primary objective:** Make the 480 × 480 Pimoroni Presto dashboard useful at a glance during coding: **How much allowance remains? When does it reset? Is the tool currently active, or when was it last used?** Preserve honest data-quality indicators. Do not build an analytics platform to deliver this.

## 1. Screenshot review — observed issues

The screenshots supplied on October 17 show:

- The overview combines OpenCode Go, Codex, Claude, Ollama Cloud, local Ollama, and LiteLLM in one vertically scrolling list; cloud pressure and local service health compete for attention.
- The Codex overview already shows **53%** and `4d19h`; its detail page has a weekly reset timestamp. **Reset information exists but needs clearer labels, placement, and local-time formatting.**
- Claude's detail view shows **5h: 0% (stale)** and **weekly: 34% (stale)**. Its visible reset timestamps (October 11 and 15) precede the October 17 screenshot. Displaying these as current or forthcoming is misleading.
- `secondary unavailable`, `stale`, `--`, and `0%` must not be conflated. An absent quota window is **unknown**, not zero.
- Seven navigation dots and tall list rows use valuable screen space. The cloud subscriptions are the user's primary page while programming; local stack/app usage are secondary.

**Scope constraint:** Improve the underlying data and the cloud overview first; avoid a wholesale UI rewrite until the data contracts are verified against the existing repo.

## 2. Confirmed official sources and limitations

**Claude Code subscription (status-line JSON)**

- **Quota fields:** `rate_limits.five_hour.used_percentage`, `rate_limits.five_hour.resets_at`, `rate_limits.seven_day.used_percentage`, `rate_limits.seven_day.resets_at`. Also includes `model.id`, `model.display_name`, and `session_id`.
- **Limit:** Event-driven and local to the Claude Code process; fields may be absent. This is not a remotely pollable subscription API.

**Claude Code activity (OpenTelemetry)**

- **Activity fields:** `claude_code.api_request` logs include `event.timestamp`, `model`, duration, and token counts.
- **Limit:** Requires telemetry configuration. A status-line refresh alone **does not prove a model request occurred**.

**Codex subscription (App Server JSON-RPC)**

- **Quota fields:** `account/rateLimits/read` exposes `rateLimits` and optional `rateLimitsByLimitId`; each available window contains `usedPercent`, `windowDurationMins`, `resetsAt`.
- **Limit:** Identify actual quota durations and bucket IDs; `primary` does not inherently mean 5h and `secondary` does not inherently mean weekly.

**Codex activity and trends (App Server)**

- **Activity fields:** `thread/list` offers `updatedAt`, `status`; thread lifecycle/status/turn notifications can expose live activity. `account/usage/read` provides optional token summaries/daily buckets.
- **Limit:** Thread update time is not proof of inference; a separate App Server may not automatically observe every running VS Code/CLI session. Verify the target environment.

**Keep subscription quotas separate from API-platform billing.** Claude Pro/Max and Codex/ChatGPT subscription allowance must not be replaced by Anthropic Admin API or OpenAI developer Usage API spend, which measure different accounts/products.

**Official references (verify again if CLI versions change):**

1. Claude Code — Status line: https://code.claude.com/docs/en/statusline
2. Claude Code — Monitoring usage / OpenTelemetry: https://code.claude.com/docs/en/monitoring-usage
3. Codex — App Server: https://developers.openai.com/codex/app-server (redirects to https://learn.chatgpt.com/docs/app-server)

### Claude-specific notes

- The status-line command consumes JSON from `stdin` and may be invoked on updates, on an optional `refreshInterval`, or on window expiry. Preserve any existing status-line behavior rather than clobbering it.
- The documented subscription `rate_limits` data is primarily for Claude.ai Pro/Max sessions and can be **missing**. A missing 5h window must render as `--` or `Unknown`, never `0%`.
- Use a local status-line bridge for **last quota observation**, not unconditionally for **last model use**.
- If precise `Last used` is required, take the most recent confirmed `claude_code.api_request` event timestamp (or explicitly label a less precise heuristic). Collect only the minimal event metadata; do not enable prompt/response or raw API-body logging.
- Windows and macOS/Linux launch/setup differ; keep the bridge cross-platform in design.

### Codex-specific notes

- App Server method: `account/rateLimits/read`. A window contains `usedPercent`, `windowDurationMins`, `resetsAt` (Unix seconds). The service can expose multiple limit IDs.
- A weekly-only account may have the weekly bucket in `primary`, with `secondary = null`. Do not treat null as 0% or 5h.
- `account/rateLimits/updated` is available as a notification in a connected App Server session.
- `thread/list` can return `updatedAt` and `status`; `thread/status/changed`/turn events can support observed activity. Label the observation correctly and handle thread `notLoaded`/idle distinctly from offline or API quota exhaustion.
- Do not create a new full Codex client merely to get this telemetry; first check whether the repository already has a supported collector or App Server connection.

## 3. Small normalized data contract

**Reuse the repo's current schemas where possible.** The following is an illustrative data shape, not a mandated migration.

```json
{
  "provider_id": "claude",
  "quota_source": "claude_code_statusline",
  "quota_observed_at": "2026-10-17T14:55:00Z",
  "quota_freshness": "fresh",
  "windows": [
    {"kind": "five_hour", "duration_minutes": 300, "used_percent": 62, "resets_at": "2026-10-17T16:30:00Z"},
    {"kind": "weekly", "duration_minutes": 10080, "used_percent": 34, "resets_at": "2026-10-22T10:32:00Z"}
  ],
  "activity": {
    "last_confirmed_request_at": "2026-10-17T14:54:00Z",
    "last_observed_at": "2026-10-17T14:55:00Z",
    "model": "claude-sonnet",
    "state": "recent",
    "source": "claude_code_otel"
  }
}
```

For **Codex**, use the same normalized `windows` list with source `codex_app_server` and actual `windowDurationMins` values. Treat any `resetsAt` as Unix **seconds**, converting it to an ISO-8601 UTC string only at the normalization boundary. Record `source`, `observed_at`, and quality per signal, not just a provider-wide OK flag.

**Do not aggregate overlapping values** from unrelated telemetry sources, or treat session-cost estimates as subscription spend. Do not overwrite good historical readings with fictitious zeroes when fields disappear.

## 4. Required freshness, reset, and activity rules

1. **Reset countdowns:** Display `Resets in 1h 24m`, `Resets in 4d 19h` etc. using `resets_at - current_time` only for a **future** timestamp from a sufficiently recent reading. Also show exact local reset time in detail. Prefer the daemon's UTC/server-time reference and ensure Presto's clock is synchronized before calculating on-device.
2. **Expired reset:** When a recorded reset time passes, **do not infer usage reset to 0%**. Treat the old quota as `stale`/`awaiting refresh` until new data arrives.
3. **Unknown is not zero:** A provider/window never reported, a returned null, and a genuine 0% are three different cases. The UI must distinguish them.
4. **Different reset windows:** For Claude show 5h and weekly if present. For Codex show whatever is actually reported (weekly-only is valid). No hard-coded `primary`/`secondary` meaning.
5. **Age and source:** Keep `quota_observed_at` separate from `activity.last_confirmed_request_at`. A periodic poll/status-line refresh **is not user activity**.
6. **Activity labels:** Use `Active` only with a verifiable live event/status; otherwise use `Last used 8m ago` (confirmed request), `Thread updated 8m ago` (heuristic), or `Activity unavailable` (no signal). Define any active timeout and label its source.
7. **Staleness policy:** Make freshness thresholds configurable; test old timestamps and daemon clock drift. A stale percentage may be shown as `Last known 34%`, visually muted, but never as current.
8. **Auth and privacy:** No OAuth/session tokens, API keys, raw prompts, request bodies, or transcripts on Presto or in public dashboard JSON. The optional collector runs locally and sends only required normalized values.

## 5. 480 × 480 layout guidance

**Proposed cloud home page:** Four large, touchable tiles (Claude, Codex, Ollama Cloud, OpenCode Go), or a two-row density equivalent if the existing renderer makes four tiles unreadable. Make **quota pressure + reset countdown** more prominent than model metadata. An offline or not-subscribed OpenCode tile can be hidden via configuration, not shown as broken. Keep Local Runtime and App Usage as the other two top-level pages.

Example mock content — **illustrative values, not live account data**:

```text
CLOUD LIMITS                           10:42
+---------------------+---------------------+
| CLAUDE              | CODEX               |
| 5h 62% used         | Week 53% used       |
| [=========----]     | [=======------]     |
| Resets 1h 24m       | Resets 4d 19h       |
| Week 34% / used 8m  | Last used 3m ago    |
+---------------------+---------------------+
| OLLAMA CLOUD        | OPENCODE GO         |
| $18 / $60 budget    | Weekly 12%         |
| 30% used            | Resets in 3d        |
|                     | (hide if inactive)  |
+---------------------+---------------------+
```

Layout rules:

- Render into a true 480 × 480 logical viewport; screenshot at 1:1 pixels even when browser scales the simulator.
- Ensure text is legible on the **physical display**, not only enlarged in a browser. Prefer fewer larger numbers and short countdown strings.
- Do not overflow, shrink to illegibility, or require vertical scrolling for the cloud overview.
- Cloud quotas and local operational status are separate concepts. A local `OK` checkmark must not look like unlimited cloud allowance.
- Use color and text together: red/yellow for quota warning, gray for stale/unknown, but never rely on color alone.
- Detail views can show both windows, exact local reset date/time, last successful update, last activity, model, source, and age of the data.
- Browser preview is an **approximation** of hardware, not proof of matching MicroPython fonts/rasterization. Require real Presto verification before declaring the UI done.

## 6. Recommended incremental implementation

**Phase A — Audit only (no code changes):** Inspect current Claude/Codex adapters, timestamp handling, activity reporting, screen renderer and simulator. Identify existing official interfaces in use, missing data, stale-data bugs, and any credentials/privacy risks. Return a small proposed diff first if behavior is unclear.

**Phase B — Reset/freshness correctness:** Preserve provider reset timestamps in a typed normalized representation, add correct relative countdowns and exact local reset strings, distinguish unknown/zero/stale, and test expired resets and weekly-only Codex windows. This is the highest-value first coding pass.

**Phase C — Local activity (narrow):** Prefer a Claude Code status-line bridge for quota snapshots and optional OTel events for confirmed last request. Use Codex App Server data for quota and narrowly scoped thread events for recent activity. If exact last-used requires more machinery than the demo warrants, show `Last updated` and document the missing signal rather than inventing it.

**Phase D — Visual polish:** Improve the cloud overview based on real and fake fixtures, keeping 480 × 480 legibility. Defer new architectures, additional providers, MQTT, browser-launching from Presto, generalized event ingestion, and advanced historical analytics.

### Acceptance checklist

- [ ] Claude 5h and weekly windows display when both are present; one absent window is shown as unknown.
- [ ] Codex weekly-only window correctly displays as weekly even if it is in `primary`.
- [ ] Both detail and overview show a **clearly labeled** reset countdown for known future resets.
- [ ] Past reset timestamps show `Stale` / `Awaiting refresh` and never a negative countdown or assumed 0%.
- [ ] Confirmed 0% is visually distinct from unknown and stale.
- [ ] `last_observed_at` and `last_confirmed_request_at` remain separate.
- [ ] A status-line timer refresh alone cannot mark Claude as `Active`.
- [ ] If Codex thread activity is used as a proxy, it is labeled `Thread updated`, not silently promoted to `Last model used`.
- [ ] No sensitive tokens/prompts leak to the Presto/status JSON.
- [ ] 480 × 480 simulator screenshots for fresh, stale, unknown, weekly-only, and four-tile views have no clipping and are readable at 100% scale.
- [ ] Real hardware smoke check is explicitly recorded as passed or not yet run.
- [ ] No changes to Ollama/LiteLLM integrations beyond what is strictly needed to preserve the shared UI schema.

## 7. Questions for the local agent to settle from the repository

1. Where exactly do Claude and Codex quota values currently come from? Does each already use an official interface or a cached/unofficial integration?
2. Does the backend retain provider reset timestamps, and does the simulator discard or incorrectly format them?
3. Is the preview generated by the same card definitions/layout logic as the MicroPython UI, or is there a separate mock renderer that can drift?
4. Are there already per-source enable/disable settings for an intermittent OpenCode subscription?
5. Does current deployment place Claude Code/Codex on a different machine from the main daemon? If so, would collection require an optional per-host helper?

**Instruction to the local agent:** First inspect and report current behavior, sources, gaps, and minimal proposed changes. Preserve existing working features; do not broaden this into a plugin framework or rebuild the UI. Ask for approval before implementing any change that alters authentication, credential handling, or collector installation.

---

**Sources checked:** October 17, 2026. Refer to the official documentation URLs in §2 for live schema/version updates. The screenshots and example values above are supplied context or illustrative fixtures, not live provider readings.
