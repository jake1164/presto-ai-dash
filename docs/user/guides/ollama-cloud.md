# Ollama Cloud

Ollama Cloud is the hosted side of Ollama. It is a separate connector from
the local Ollama server, and it needs an API key.

## Set it up

1. Open the admin page and choose **Add source -> Ollama Cloud**.
2. Give the account a name.
3. Paste the API key into the key field and press **Save source**.

The key is encrypted before it is stored and is never sent back to the
browser.

## What it reads

Two authenticated read-only requests, with redirects disabled:

- [`GET /api/balance`](https://docs.ollama.com/api/balance) supplies monthly
  included allowance, remaining included credits and the billing period.
  The quota bar shows the fraction consumed: `(allowance - remaining) / allowance`.
  Its reset comes from the billing period's end, never from a guessed calendar month.
  Purchased credits are shown separately. Legacy accounts show the reported
  session and weekly limits instead; their remaining percentages are converted
  to percentages used without assuming a session duration.
- [`GET /api/usage?range=7d`](https://docs.ollama.com/api/cloud-usage) supplies
  rolling activity totals:

- the number of requests,
- input, cached and output tokens,
- the reported USD activity,
- the date range the provider returned.

The card shows the totals separately, with the date range, so an activity
total is never mistaken for an allowance.

## Reporting periods and limits

The rolling activity period includes seven complete UTC days and today so far.
It is not the billing month, so its request count can differ from the website's
monthly model breakdown. Activity spend is not used to infer included credits:
purchased funds and team usage can make the totals differ.

Missing or invalid balances stay unavailable; a zero allowance has no percentage.
An exhausted included balance reads 100% consumed even if purchased funds remain.
With purchased funds available, this raises an exhausted-allowance alert without
claiming the account is rate limited. Legacy request-only activity shows requests;
missing spend and token totals are omitted rather than displayed as zero.
The connector does not infer a plan name from an allowance, fetch recent individual
requests, or call model-generation, payment or account-management endpoints.

## Refresh

The page offers a request timeout of 1 to 60 seconds and a refresh interval
of 60 to 3600 seconds, 60 by default (older shorter settings are clamped to 60).
`Retry-After` on HTTP 429 defers the next poll. Each reading keeps its original
observation timestamp; failures retain last-known values marked stale. Activity
can fail without blanking a successful balance. A passed billing reset needs a
new provider reading; it never becomes a fabricated 0%.

## See also

- [Ollama](ollama.md), the local server connector
- [Sources](../concepts/sources.md)
