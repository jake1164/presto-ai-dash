# Subscription quota monitoring

Planning update agreed on 2026-10-11. This is planned work, not an implemented
integration or a completed account-level readiness check.

## Priority and scope

The next milestones monitor subscription allowances for OpenAI/Codex, Claude,
and OpenCode's own Go subscription. They precede alert polish and the demo
release. API billing and LiteLLM request/token/spend totals are separate metrics;
they cannot establish how much of these subscription allowances remains.

Support multiple configured accounts through the existing source interface and
source-instance configuration. No provider or account is a single hard-coded
instance. Application monitoring, including multiple book or other apps,
remains a separate planned feature in [future.md](future.md#application-usage-monitoring).

The completed M0–M5 milestones and connected Presto work remain unchanged.
See [the tracker](milestones.md) for M6–M13 and implementation status.

## Reporting paths and readiness gaps

Official documentation reviewed on 2026-10-11 establishes these candidates:

| Target | Candidate reporting path | Required verification before implementation |
| --- | --- | --- |
| OpenAI/Codex subscription | Codex app-server `account/rateLimits/read`, using its supported account login. Reports usage percentage, window duration, reset time, and potentially multiple quota buckets. | Installed protocol version, account/plan coverage, supported login and refresh, and a real redacted reading. This does not establish coverage of every ordinary ChatGPT model/message limit. |
| Claude subscription | Claude Code's documented status-line JSON exposes `rate_limits.five_hour` and `rate_limits.seven_day` percentages and reset times for Pro/Max subscribers when available. A local helper can export only those fields to the daemon. | Installed Claude Code version and plan, a real reading, preservation of any existing status-line command, and freshness when no session is updating the export. Fields appear only after the first API response and may be independently absent. |
| OpenCode Go subscription | Go documents 5-hour, weekly, and monthly allowances. Monitor Go itself, independently of any OpenAI/Claude account used in the OpenCode app. | A supported way to retrieve current usage and reset times, authentication, account/model scope, period semantics, and a real redacted reading. The limits documentation alone does not prove automated retrieval is available. |

Primary references:

- [OpenAI Codex app-server: account and rate-limit protocol](https://learn.chatgpt.com/docs/app-server)
- [Claude Code status line: rate-limit usage](https://code.claude.com/docs/en/statusline#rate-limit-usage)
- [OpenCode Go: usage limits](https://opencode.ai/docs/en/go/)

M6 must report each candidate as ready, conditional, or blocked with evidence.
Do not invent an endpoint, infer subscription quota from token counts, or
silently substitute API spend. If Go has no supported retrieval path, record
that blocker and a proposed user-exported alternative for review rather than
claiming the source is implemented. Readiness work stops at its deliverable;
implementation requires an explicit request.

Use supported login, read-only interfaces, or user-authorized exports. Do not
scrape credentials or reverse-engineer private CLI state. Do not make a model
request merely to obtain a quota reading; inspect telemetry from normal use.
Provider credentials remain off the Presto. A local helper should export
quota fields, not prompts, transcripts, access tokens, or account email.

## Reusable quota contract

M7 specifies the API/config contract before source implementation:

- A stable account/source-instance identity, configurable name, provider, and
  subscription product. Rotation or reconnect preserves identity and history.
- A list of quota windows per account/bucket, not fixed `daily` and `weekly`
  fields. Each has its identity, reported period/duration, usage percentage,
  reset time, observation time, and availability/freshness state.
- Preserve provider window semantics. Display `5h`, `Weekly`, or `Monthly`
  when those are the actual periods; use `Daily` only for an actual daily limit.
  Do not assume every account has the same windows or a fixed allowance.
- Distinguish missing telemetry, stale readings, and an explicitly unlimited
  allowance. An unknown percentage or an expired reading is not zero usage.
  Resetting a timer does not establish the next window's usage without a new
  provider reading.
- Keep separate provider/model buckets and accounts separate. Never sum
  percentages or successive cumulative snapshots. Existing 72-hour history
  does not reconstruct a weekly quota; use the provider's current window data.
- Supported account login may be owned by the provider's CLI/helper instead of
  an API key in SQLite. Specify that lifecycle per adapter. Reuse encrypted
  storage for daemon-held secrets where applicable; keep all secrets out of
  status responses and exported quota files.

## Presto acceptance

M11 connects the reusable windows to Home:

- Reuse the scrolling account list, pinned header, and horizontal navigation.
  Two stacked bars remain visually separate, with percentages and period labels.
- Show additional windows, such as Go's monthly allowance, in the same reusable
  row/detail design. Do not drop them or label them as weekly to fit two bars.
- Show reset information and stale/unavailable states without synthetic fallback.
  Static quota samples remain explicitly labelled DEMO.
- At least two accounts work independently; adding another account needs
  configuration rather than provider-specific rendering code. Test overflow
  beyond one screen, account removal/rotation, optional windows, resets, stale
  exports, authentication failures, and unavailable data.
- Verify actual labels, stacked bars, scrolling, and card switching on the Presto.
  M12 then adds quota alert thresholds and reset/recovery/mute behavior.

Keep this a small desk display. Cloud account billing, application budgets,
subscription quotas, and connection health must remain distinguishable.
