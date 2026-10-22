# Codex

The Codex source reads the allowance windows of an existing ChatGPT
subscription login, through the local Codex app-server. It does not cover
ordinary ChatGPT message limits, and it does not cover API billing.

## Set it up

1. Open the admin page and choose **Add source -> OpenAI / Codex**.
2. Give the account a display name and enable collection.
3. Leave **Codex executable** blank for path discovery, followed by the
   discovery of a locally installed VS Code Codex extension binary. You can
   also save a custom executable path.
4. Leave **Codex profile directory** blank to use the existing default
   login. For another account, point it at that separately authenticated
   Codex profile directory.
5. Save.

The account gets a Home quota group and its own card automatically.

## The login stays with Codex

Codex owns its OAuth credential cache and keychain, and refreshes them
itself. The dashboard never reads or copies a credential file into its
database, and there is no API key to paste into the source settings.

Sign in with Codex's own ChatGPT login flow. The daemon's admin API also has
routes to start a browser or device-code sign-in, cancel it and sign out for
a selected Codex profile: the daemon starts a bounded app-server process,
returns an authorization URL or a verification code, and stores no OAuth
credential. The admin page has no buttons for these routes yet, so in
practice you sign in with Codex itself.

An API-key login is refused for this source, because it cannot report
subscription allowances.

!!! note "Separate accounts need separate profiles"
    Each account has its own instance ID and its own process. Two accounts
    that point at the same Codex profile directory watch the same login, not
    two different accounts.

## What it reads

Each fresh probe starts a bounded local app-server process, initialises it,
asks `account/read` to use the supported token refresh, and then reads
`account/rateLimits/read`. It closes and reaps only its own process
afterwards.

No thread, turn, model prompt, credit purchase or quota-reset action is
sent. Notifications and raw account data are discarded. Account emails,
identity fields and tokens are not saved and are not returned by the API.

The response can contain several allowance buckets. The dashboard uses the
bucket map when the provider supplies one and falls back to the legacy single
view, never showing both. Buckets and the primary and secondary slots keep
separate identities.

Labels come from the reported duration: a primary weekly window is labelled
`Weekly`, not *Daily* or *5h*. A missing optional window stays
*unavailable*, never `0%`.

## Banked reset credits

Codex reports **banked resets** - extra early resets the subscription has
banked - in the same read-only response the dashboard already calls. No
extra request is made.

The dashboard **never redeems a reset credit**. There is no code path that
spends one, and the provider's redeem method is deliberately not wired up.

What you see:

- `Resets avail: 3` - the provider's own count of banked resets,
- `Next exp: 10-24 12:00Z (4 days)` - the earliest expiry of the credits the
  provider lists as available,
- `Credits: 0` - the credit balance,
- `Source: local Codex app-server` - the last line of the card.

A count the provider does not report shows no line at all. An unknown count
is never shown as a count of zero, and the `R0` badge is never drawn. The
Home row carries a small `R3` badge while resets are banked; it turns amber
and adds the time left when the earliest expiry is 72 hours away or less.

## Refresh and failure

The default refresh is 60 seconds and the reporting timeout is 15 seconds. A
cached read keeps its original timestamp, so the reading never looks fresher
than it is.

A failed probe keeps the last known quota, visibly stale with a fixed
diagnostic; a later success clears the error. Crossing a reset never
fabricates a new zero reading.

## Troubleshooting

| Symptom | What to check |
| --- | --- |
| No account card | The source is disabled, or the Codex executable was not found |
| "Codex executable" errors | Set the path to the `codex` binary, or to the VS Code extension binary |
| The same account twice | Both point at the same profile directory; give one its own |
| The reading is stale | The probe failed; the card keeps the last known quota and says so |
| Sign-in fails | Sign in again with Codex's own ChatGPT login flow |

The **Status** tab names the last problem in plain words, and
[the troubleshooting pages](../troubleshooting/stale-readings.md) explain the
freshness rules.

## See also

- [Sources](../concepts/sources.md)
- [Quotas and where numbers come from](../concepts/quotas-and-provenance.md)
