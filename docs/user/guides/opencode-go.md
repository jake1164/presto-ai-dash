# OpenCode Go

OpenCode Go is a hosted coding subscription. The dashboard reads the
allowance windows the provider reports for your key: how much of the 5-hour,
weekly and monthly allowance is used, and when each resets.

## What Presto collects

One authenticated, read-only `GET` to a fixed HTTPS route, with redirects
disabled: `https://opencode.ai/zen/go/v1/usage`. No model request is ever made,
and nothing is sent to OpenCode except the key.

Three windows come back:

| Window | Label |
| --- | --- |
| A short rolling window | `5h` |
| A weekly window | `Weekly` |
| A calendar-month window | `Monthly` |

Each one carries a percentage and a reset time. A window the provider does not
report is shown as *unavailable*, never as `0%`. A window reported as rate
limited makes the account *rate limited*.

OpenCode's public documentation does not describe this usage route. The source
reads the reply the route returns today, and if the reply changes it shows
*unavailable* rather than guessed numbers.

## What key to use

**Today: a dedicated Go key, with an expiry.** The Go subscription's API key is
what the usage route has been tested with. It is an inference-capable
credential: OpenCode uses the same kind of key for model requests, so treat it
like a password. Presto only ever makes the one read-only call above, but a
leaked key could be used for more.

**Least privilege: a restricted service-account key, if OpenCode lets it read
the Go allowances.** OpenCode's Console API documentation
(<https://opencode.ai/v2/docs/console/api>) describes keys with per-resource
permissions, including **Usage: Read**. It does not say which route that
permission opens, and it does not say whether a restricted key can see a Go
subscription's allowances. Go plans are for a single member, so check that
**Settings -> Service accounts** is offered for your workspace.

So treat the restricted key as something to try, and prove it before you rely
on it:

1. In the OpenCode Console, open **Settings -> Service accounts -> Add Service
   Account**. Name it `presto-ai-dash`.
2. Open the account and **Add API Key**. Name it `presto-ai-dash`, choose an
   expiry, and set **Permissions** to **Restricted** with:

   | Resource | Setting |
   | --- | --- |
   | Usage | **Read** |
   | Models | None |
   | Budgets | None to start with |
   | Billing, Members, Providers, Workspace settings, SSO | None |
   | Service accounts, this account's API keys | None |

3. Copy the key when it is shown. It is shown once.
4. **Test it before you add it to Presto.** From any computer:

   ```bash
   curl -s -H "Authorization: Bearer YOUR_KEY" https://opencode.ai/zen/go/v1/usage
   ```

   You want a reply with `rolling`, `weekly` and `monthly` entries that each
   have a `percent` and a `resetsAt`, and numbers that match the Console's Go
   usage page. A reply that succeeds but has no windows, or numbers for a
   different workspace, is **not** a pass.
5. If the test returns `401` or `403`, or a reply without the Go windows, the
   restricted key cannot see the subscription. If `Budgets: Read` is what
   unlocks it, add that and test again, and stop adding permissions as soon as
   it works. Otherwise use a dedicated Go key.

Leave **Allowed IP addresses** blank unless the collector has a stable public
address. OpenCode sees the address your traffic leaves your network from, not a
Docker or LAN address, so listing those would block Presto.

Set an expiry that suits unattended monitoring (the Console offers one day to a
year) and put the renewal date in your calendar: when the key expires the card
says the key was rejected.

## Set it up in Presto

1. Open the admin page and choose **Add source -> OpenCode Go**.
2. Give the account a name.
3. Paste the key into the key field and press **Save source**.

The key is encrypted before it is stored and is never sent back to the
browser. Leave the field blank to keep the key you saved earlier; use **Remove
stored key** to delete it. The key stays on the computer running the daemon: it
is never sent to the Presto.

## Check that it works

The card fills in on the next poll (within a minute). It should show three
percentages that match the Console. If not, the card says why:

| You see | Meaning and fix |
| --- | --- |
| **API key rejected (invalid, expired or revoked)** | The key is wrong, expired or revoked. Create a new one |
| **Key may not read subscription usage (HTTP 403)** | The key is valid but its permissions do not include usage. Add **Usage: Read**, or use a dedicated Go key |
| **The usage reply has no Go allowance windows** | The key is valid but cannot see the Go subscription: it may belong to a service account or another workspace |
| **Usage route not found (HTTP 404)** | OpenCode moved or removed the route. Update Presto |
| **Rate limited by the usage server (HTTP 429)** | Too many requests. Presto slows down by itself |
| **Cannot reach the OpenCode Go usage server** | Network or DNS from the computer running the daemon |
| **Subscription usage unavailable; the reporting format changed** | The reply has an unexpected shape. The last good reading stays, marked stale |

## Revoke or replace the key

Delete the key (or the service account) in the OpenCode Console. Presto then
shows *API key rejected*. To replace it, create the new key first, paste it into
the source and save, and then delete the old one, so there is no gap.

## Limits

- There is no raw dollar allowance and no per-model attribution in the reply.
  The dashboard does not invent either of them.
- The usage route is defined by the provider and is not documented publicly, so
  it is no guarantee of every account's reporting shape. A change gives
  *unavailable*, and the account keeps its last good reading, visibly stale.
- Presto cannot make the key narrower than OpenCode allows. Whether a
  restricted key shows the Go allowances has not been confirmed against a live
  account; use the test in step 4.
- **Extra usage** (falling back to a pay-as-you-go balance after the limits) is
  not tracked.

## Refresh

The page offers a request timeout of 1 to 60 seconds and a refresh interval of
30 to 3600 seconds, 60 by default. Collection follows the daemon's polling
cadence, so the real interval can be a little longer. A cached read keeps its
original observation timestamp.

## See also

- [Sources](../concepts/sources.md)
- [Quotas and where numbers come from](../concepts/quotas-and-provenance.md)
