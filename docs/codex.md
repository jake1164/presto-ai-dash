# Codex subscription source

The first M8 delivery reads live **Codex** allowance windows through the
[supported local app-server protocol](https://learn.chatgpt.com/docs/app-server).
It uses an existing ChatGPT subscription login in Codex. It does not cover all
ordinary ChatGPT model/message limits or API billing.

## Configure in the dashboard

1. Open the local configuration UI and choose **Add source → OpenAI / Codex**.
2. Give the account a display name and enable collection.
3. Leave **Codex executable** blank for PATH discovery, followed by discovery
   of a locally installed VS Code Codex extension binary. A custom installed
   executable path can also be saved here.
4. Leave **Codex profile directory** blank to use the existing default login.
   For another account, select its separately authenticated, absolute Codex
   profile directory. Each source has its own instance ID and process; duplicate
   references to the same profile monitor the same login, not different accounts.
5. Save. The account gets a Home quota group and its own swipe card automatically.

Names, paths, timeout, enabled state, and refresh interval live in SQLite and
are managed through the UI. There is no new provider TOML file or API key to
enter. Codex owns its OAuth credential cache/keychain and refresh; the daemon
does not read or copy credential files into its database. This is an explicit
exception for provider-managed OAuth, rather than a second source-settings
store. See [Codex authentication](https://learn.chatgpt.com/docs/auth).

Browser and device-code sign-in, cancellation, and logout are now available from
the dashboard for a selected Codex source profile. The daemon starts a bounded
app-server process, returns an authorization URL or verification code, and
does not store OAuth credentials; Codex owns its credential cache. If no
subscription login is present, sign in through one of those flows or Codex's
normal ChatGPT login flow. An API-key login is rejected for this source
because it cannot report subscription allowances. Do not paste OAuth
credentials into source settings.

## Reporting behavior

- Every fresh probe starts a bounded local app-server process, initializes it,
  asks `account/read` to use supported token refresh, then reads
  `account/rateLimits/read`. It closes and reaps only its own process afterward.
- No thread, turn, model prompt, credit purchase, or quota-reset action is sent.
  Notifications and raw account/error data are discarded. Account emails,
  identity fields, and tokens are not saved in observations or returned by the API.
- Use the bucket map when supplied; the legacy single view is a fallback,
  never a second copy. Buckets and primary/secondary slots retain separate IDs.
- Labels derive from the reported duration. A primary weekly window is labelled
  Weekly, not Daily or 5h. Missing optional windows stay unavailable, not 0%.
- Default refresh is 60 seconds, reporting timeout 15 seconds. Cached reads
  retain their original timestamps. Failed probes retain the last known quota
  visibly stale, with a fixed diagnostic; subsequent success clears the error.
  Crossing a reset never fabricates a new zero reading.

## Reset credits

Codex reports **banked resets** - extra early resets the subscription has
banked - in the same read-only `account/rateLimits/read` response the daemon
already calls. No extra request is made, and **the project never redeems a
reset credit**: there is no code path that spends one, and the provider's
redeem method is deliberately not wired up.

What is shown:

- The Codex detail card adds `Resets avail: 3` (the provider's own count of
  banked resets), `Next exp: 10-24 12:00Z (4 days)` (the earliest expiry of
  the credits the provider lists as available) and `Credits: 0` (the credit
  balance) when the provider reports them. The last line names the source:
  `Source: local Codex app-server`.
- A count the provider does not report shows no line at all. An unknown count
  is never shown as a count of zero, and `R0` is never drawn.
- The Presto Home row carries a small `R3` badge after the account name while
  resets are banked. It turns amber and adds the time left (`R3 2d`) when the
  earliest expiry is 72 hours away or less, counted against the payload's
  `updated_at` rather than the device clock. The badge is skipped when it
  does not fit between the name and the note; the detail card is the source
  of truth.
- The credit balance is normalised: the provider's `0E-10` is shown as `0`,
  never in exponent notation.

The count is the provider's `availableCount`; only rows whose status is
`available` supply an expiry, and at most eight expiries are kept, earliest
first. Missing or malformed data means "not reported", never a count of zero.
The numbers reach the status payload unchanged.

## Verification and remaining M8 work

On 2026-10-11, the installed CLI `0.162.0-alpha.17.2` returned a live weekly
window at **14%**, resetting `2026-10-18T09:55:34Z`, with no secondary window.
This is a checkpoint observation, not a configured limit or permanent value.
The verified account was saved in the live database after a consistent backup.

Thirteen focused tests cover map/legacy responses, independent buckets,
optional and unusual periods, malformed data, rate limiting, reset aging,
failure/recovery, redaction, exact reporting-only methods, subprocess timeout
and cleanup, profile routing, and multiple UI-configured accounts across a
database restart. All 269 host tests pass. Direct frontend smoke checks verify
source selection, profile fields, hidden API-key inputs, cookie-authenticated
save, and reload. These frontend checks do not claim browser visual verification.
An isolated runnable daemon also passes a real Codex end-to-end smoke check:
status auth returns 401/200, the admin catalog includes Codex and its reading,
and the API creates the separate account card. After the user restarted the
VS Code daemon, its actual status API returns 401 without authentication and
200 with the device token; Codex reports healthy, fresh weekly usage at 14%.
The connected Presto fetched that reading and its reset, with five accounts
and six cards. Real-font layouts, every swipe card, wraparound, selected-account
preservation, grouped quota bars, and clipped scrolling pass on-device.
The live app was left running and COM3 released. No device code change was
needed for the additional account.

M8 remains **in progress**: dashboard-managed sign-in/logout/cancellation, real
expired-login recovery, a second real account/profile, other plans/buckets,
and compatibility with later CLI protocol versions still need acceptance work.
Claude remains a separate M9 source with a real-export readiness gate.
