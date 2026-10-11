# Cloud accounts and quota readings

Add and manage accounts at **http://127.0.0.1:8788**. Each account has its own
stable source ID, display name, enabled flag, and refresh interval in SQLite.
API keys, where required, are encrypted there. Codex OAuth stays with Codex's
credential cache/keychain. Multiple accounts of the same provider have separate
configuration and observations.
Blank key fields preserve the saved key; entering a replacement preserves the
account ID and history. No separate provider TOML file is needed at runtime.

| Source | Current measurements | Limitations |
| --- | --- | --- |
| OpenCode Go | Provider-reported 5h, Weekly, Monthly percentages and reset times | No raw dollar allowance or per-model quota attribution in the verified response |
| Ollama Cloud | Requests, USD activity, input/cached/output tokens for the provider-returned date range | Monthly allowance, balance, and reset are not returned; the Monthly credits meter is unavailable |
| Local Ollama | Server health and loaded models | Separate from Ollama Cloud; no API key required |
| OpenAI / Codex | Reported quota buckets, durations, percentages, and resets through an existing Codex ChatGPT login | [Dashboard-managed login remains pending](codex.md); other ChatGPT message/model limits are not implied |
| Claude | Documented 5h/Weekly terminal-export percentages and resets | [Live terminal readings verified](claude.md). Cached/idle observations stay stale; graphical-panel coverage unverified |

OpenCode Go and Ollama Cloud issue only authenticated GET requests to fixed
HTTPS reporting routes, with redirects disabled. They make no model requests.
The UI offers request timeouts of 1–60 seconds and refresh intervals of
30–3600 seconds (default 60). Collection follows the daemon's polling cadence,
so the actual interval can be longer than the chosen minimum. Cached reads
preserve their original observation timestamp.

The [OpenCode Go route](https://github.com/anomalyco/opencode/blob/dev/packages/console/app/src/routes/zen/go/v1/usage.ts)
defines the response verified for the current account. This source-backed route
is not a guarantee of every account's future reporting shape. Schema changes
produce unavailable/stale data rather than guessed percentages.

Ollama's [current pricing](https://ollama.com/pricing) describes monthly included
credits and separate top-ups. The authorized read of `https://ollama.com/api/usage`
on 2026-10-11 returned a dated activity total, without monthly allowance,
remaining balance, or reset. An arbitrary reporting period is not the monthly
credit period. The adapter stores selected numeric totals and date bounds;
it does not store account identity, model names, request records, or raw bodies.
The screenshot's rounded spend and API activity value need not match exactly.

## Status contract

In managed live mode, `/api/v1/status` returns Home followed by one swipe card
per enabled source instance, using that instance's stable ID and configured
name. Adding, disabling, or deleting accounts changes the sequence on the next
poll. Accounts of the same type remain distinct. `card_order: "accounts"` tells
the device to preserve the daemon's account order. The selected card survives
refreshes by ID; removal returns to Home. Static/legacy payloads retain their
original order. Managed responses add `quota_schema_version: 1`. Each enabled source can have
a `quota` object with `provider`, `product`, `windows`, `stale_after_seconds`,
and a safe `error`. Ordinary local/LiteLLM metrics retain their existing shape.

Each window contains:

| Field | Meaning |
| --- | --- |
| `id` | Stable identity within this account's report |
| `label` | Actual provider period, such as `5h`, `Weekly`, or `Monthly credits` |
| `period_minutes` | Positive duration when known; null for a calendar month/unspecified duration |
| `used_percent` | Finite nonnegative percentage, or null when unknown/unlimited; values over 100 are retained |
| `resets_at` | Provider reset in UTC ISO format, or null |
| `observed_at` | Original UTC observation time |
| `state` | `available`, `unavailable`, `unlimited`, or the read-time state `stale` |

Adapters may include numeric `used_amount`, `limit_amount`, and `currency`
when actually reported. Ollama Cloud instead includes an `activity` object
with date bounds, observation time, selected totals, and a read-time state.
Activity is separate from allowance consumption; it is not converted to a quota.

Read-time presentation marks old readings stale after the greater of three
minutes or three configured refresh intervals. A reporting failure immediately
marks retained readings stale. Passing a reset time also marks a window stale
until the next provider reading; it never fabricates zero usage. Connection
health, availability, and quota exhaustion are distinct. Unavailable monthly
credits alone do not turn otherwise successful Ollama Cloud activity into a
health warning. Accounts and percentages are never summed.

The Presto Home renderer puts quota accounts first and groups their reported
windows under one account heading, with separate stacked bars and actual period
labels. Cyan identifies short windows, purple weekly windows, and orange monthly
windows. Rows expand to fit additional windows, and accounts scroll vertically.
Swipe cards show account measurements and UTC reset details. Missing values
have no filled bar; historical stale fills are dimmed. Large carousels use a
bounded dot indicator and page count. Static samples remain DEMO. Codex live
values come from its reporting source; Claude demo values do not imply a live adapter.

## Implementation verification — 2026-10-11

- 256 host tests pass, including encrypted account CRUD through cookie/CSRF
  authentication, multiple accounts, cached freshness, reset, failures, schema
  changes, redirect rejection, and Presto layout/overflow.
- The dynamic carousel and grouped Home were backed up and deployed to the
  connected Presto on COM3. Its authenticated live poll returned four accounts
  and five cards. Real-font overlap/bounds, grouped bars, clipped scrolling,
  every swipe card, wraparound, and selected-account preservation pass on-device.
  The app was left running. The user confirmed the bars and all five cards look good.
- Direct frontend smoke checks pass against an isolated real admin HTTP server
  for account creation, refresh settings, key replacement, deletion, meters,
  and remembered login. Visual browser verification was unavailable.
- Fresh authenticated Go reads show 0% for all three windows; the earlier
  readiness check was compared with the user's Console. Nonzero/reset boundary
  behavior is covered by fixtures rather than an actual quota reset.
- Ollama Cloud returns two requests, 52 input tokens, 32 output tokens, and
  USD 0.000020 for its returned reporting range. Monthly quota remains unknown.
- Both supplied keys were imported into encrypted SQLite, verified after
  reopening, and the two temporary key intake TOML files removed. A consistent
  database backup remains in the ignored local backup directory.
- After the user's daemon restart, the live local status endpoint returns 401
  without authentication and 200 with the device token. It exposes quota schema
  version 1, three Go windows, and the explicit monthly-credits window for Ollama
  Cloud; both cloud sources report healthy. Obsolete source bootstrap sections
  were removed from private `dash.toml` without changing its connection tokens.

Bootstrap network/database settings remain local daemon configuration. The
encryption master key stays outside SQLite, protected with Windows DPAPI or
owner-only file permissions, so a database copy alone cannot decrypt keys.
See [configuration and recovery](configuration.md).
