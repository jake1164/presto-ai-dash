# OpenCode Go

OpenCode Go is a hosted coding subscription. The dashboard reads the
allowance windows the provider reports for your key.

## Set it up

1. Open the admin page and choose **Add source -> OpenCode Go**.
2. Give the account a name.
3. Paste the API key into the key field and press **Save source**.

The key is encrypted before it is stored and is never sent back to the
browser. Leave the field blank to keep the key you saved earlier; use
**Remove stored key** to delete it.

## What it reads

One authenticated `GET` against a fixed HTTPS reporting route, with
redirects disabled. No model request is ever made.

Three windows come back:

| Window | Label |
| --- | --- |
| A short rolling window | `5h` |
| A weekly window | `Weekly` |
| A calendar-month window | `Monthly` |

Each one carries a percentage and a reset time. The monthly window has no
fixed length, so its period is not stated.

A window the provider does not report is shown as *unavailable*, never as
`0%`. A window the provider reports as rate limited makes the account
*rate limited*.

## Limits

There is no raw dollar allowance and no per-model quota attribution in the
verified response. The dashboard does not invent either of them.

The reporting route is defined by the provider, and it is not a guarantee of
every account's future reporting shape. A change in the schema produces
unavailable data rather than guessed percentages, and the account keeps its
last good reading, visibly stale.

## Refresh

The page offers a request timeout of 1 to 60 seconds and a refresh interval
of 30 to 3600 seconds, 60 by default. Collection follows the daemon's
polling cadence, so the real interval can be a little longer than the
minimum you chose. A cached read keeps its original observation timestamp.

## See also

- [Sources](../concepts/sources.md)
- [Quotas and where numbers come from](../concepts/quotas-and-provenance.md)
