# Subscription quota monitoring

Planning update agreed on 2026-10-11. The shared quota model, OpenCode Go adapter,
and Ollama Cloud activity adapter are now implemented. Accounts and keys are
managed through the UI and encrypted database; [current readings and limitations](quotas.md)
describe what is available. [Codex reporting](codex.md) now uses an existing
local subscription login; dashboard-managed sign-in remains pending. Claude
[has a live verified terminal exporter](claude.md) for 5h/Weekly readings. The
[M6 readiness review](m6-readiness.md) now verifies Codex reporting for the
current account and authenticated Go quota reads matching the user's Console
meters. Claude's real terminal export also reaches its Home meters and swipe card
on the Presto. Graphical-panel export coverage remains unverified.

## Priority and scope

The active milestones monitor subscription allowances for OpenAI/Codex, Claude,
and OpenCode's own Go subscription, plus Ollama Cloud monthly credits where
reported. Ollama Cloud currently provides activity, not the credit balance.
These sources precede alert polish and the demo
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
| OpenCode Go subscription | M6 verified authenticated `GET /zen/go/v1/usage`: HTTP 200, three percentages and reset times. User confirmed all three 0% readings match the subscribed account's Console meters. | Implementation must verify nonzero readings, reset/recovery, and compatibility for migrated or other accounts. Per-model breakdown and every plan's bucket coverage remain unverified. |

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

M7 implements the [shared API/config contract](quotas.md) used by the first adapters:

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

- Home has no header. Quota accounts fill it (four fit without scrolling;
  more scroll). Each row: a status marker (solid only when the daemon reports
  `active`; there is no last-used signal yet), the name, and on the right the
  binding window's labelled reset (`Wk resets 4d19h`; the most-used window,
  on a tie the longer one). Below, each window as a short tag and a large
  percentage (`5h 72%  Wk 96%  Mo 41%`), coloured by period (cyan up to a day,
  purple longer, orange monthly, red at limit), then one stacked bar per
  window. Countdowns use the payload's `updated_at`, not the device clock.
- Home follows the saved account order, the same order as the swipe cards:
  the admin page can re-order the accounts and Home shows them in that order,
  quota accounts and local services alike. A local service among quota
  accounts is a compact one-line row at its own place (status marker, name,
  and on the right the same status or usage the old bottom strip showed).
  When no quota account exists the local services are the list and keep their
  two-line rows. The list scrolls by its total height, so the mixed row
  heights scroll correctly and the last row stays fully reachable.
- Honest states: a stale window reads `last known 6d ago` (no age while the
  device itself is cut off), a passed reset reads `awaiting refresh`, an
  unknown window shows `--` (distinct from a measured `0%`), and an
  unidentified empty window such as Codex's null `Secondary` stays off Home.
- Freshness hints (Phase 4): Claude-style reports that carry a collector trail
  add a short ASCII hint (at most 28 characters) the daemon computes at payload
  build time, such as `Run claude in a terminal`, `Waiting for a Claude reply`,
  `Awaiting refresh`, `Reply in claude to refresh`, or `No report 6d - run
  claude`. When a row has a hint and no window is fresh, the line is drawn
  right-aligned in the dim ink in place of the reset note (it wins over a
  cutting long name); with a fresh window no hint is drawn. A window whose
  reset has passed draws `--` with an empty bar on Home — never a dimmed or
  `0%` value; its last measured value stays on the detail card.
  Stale-but-unexpired windows keep today's dimmed value. Rows without a hint
  behave exactly as before.
- Local activity (Phase 5): with the Claude account's activity-log reader on
  and working, in-use Claude reads `In use - quota 6d old` (or
  `In use - no quota data`) and becomes `active` — a solid marker and the
  blue accent while a request is within the configured active window;
  `Used 3h ago - quota expired` shows recent but out-of-window use. While
  the reader works, stale or missing quota alone never raises the row to a
  warning; the hint text carries the staleness. After a quiet 24 hours the
  Phase 4 hints apply again, and with the reader off or failing every line
  and status is exactly Phase 4. The detail card then leads with
  `Wk reset ~4 days (est)` (only when the real weekly reset has passed),
  `Last used: <age> ago (<surface>)` in place of `Last reply`, and
  `Log tokens 5h/7d: 120k/2.1M` (from the local session logs) — token load only, never a percentage; no quota
  is derived from them. The estimate is labelled and never shown as a
  measured reset.
- Banked resets (Phase 9): a Codex account whose provider reports banked
  reset credits shows `Resets avail: 3`, `Next exp: 10-24 12:00Z (4 days)` and
  `Credits: 0` on its detail card, and a small `R3` badge on Home that turns
  amber with the time left (`R3 2d`) when the earliest expiry is 72 hours
  away or less; the project never redeems a reset.
- Local services (Ollama, LiteLLM) are health, not allowance: among quota
  accounts each one is a compact one-line row at its own place in the saved
  order, and they become the list, with two lines each, when no quota
  account exists.
- Show additional windows, such as Go's monthly allowance, in the same reusable
  grouped Home row. Do not drop them or label them as weekly to fit two bars.
- Generate one swipe card per enabled account, including new source types and
  multiple accounts of one type. Preserve selected identity across refreshes;
  adding/disabling/deleting accounts updates the sequence on the next poll.
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
