# Ollama Cloud

Ollama Cloud is the hosted side of Ollama. It is a separate connector from the
local Ollama server, and it needs an API key.

## A warning about the key

**An Ollama API key is not read-only.** Ollama's documentation describes keys
that can be created and revoked, and says they do not expire. It describes no
scopes or permission levels, and the same key authenticates model requests to
`https://ollama.com/api` and `https://ollama.com/v1`. A key you give Presto can
therefore also run models on your account.

Presto only makes the two read requests listed below, but that is a promise
about Presto, not a limit on the key. If the key leaked, someone could use it
for anything a key can do. So:

- Create a **dedicated key** for Presto, named `presto-ai-dash-monitor`, and use
  it nowhere else. That lets you revoke it alone.
- Treat it like a password. It stays on the computer running the daemon, encrypted;
  it is never sent to the Presto.
- Do not describe it as "read-only" or "restricted". It is neither.

## What Presto collects

Two authenticated read requests, with redirects disabled:

- [`GET /api/balance`](https://docs.ollama.com/api/balance): the included
  monthly allowance, what remains, the billing period, and the purchased credit
  balance. The quota bar shows the fraction consumed:
  `(allowance - remaining) / allowance`, and its reset is the end of the billing
  period. Older plans report a session and a weekly limit instead, shown as
  percentages used.
- [`GET /api/usage?range=7d`](https://docs.ollama.com/api/cloud-usage): rolling
  activity totals: requests, input, cached and output tokens, and the value of
  the requests, over seven complete UTC days and today so far.

Presto never calls model-generation, payment or account-management endpoints.
The card shows these as separate things, so one is never mistaken for another:

| On the card | What it is |
| --- | --- |
| Monthly credits (the bar) | Included allowance used this billing period |
| Included left / Purchased | Remaining balances, shown apart |
| Requests, tokens | Measured activity over the rolling range |
| Requests valued at | The **value** of those requests, including usage covered by your plan and usage paid from purchased credits. It is not necessarily a charge |

## What it does not tell you

- **Pay-as-you-go spending.** Ollama's usage route describes `usage_usd` as the
  value of requests, not a bill, and a public report
  (<https://github.com/ollama/ollama/issues/18653>) says its activity cost and
  older limit fields do not reflect charges drawn from a credit balance. Presto
  does not infer spend, included credits or a plan name from activity.
- **A purchased balance as a limit.** An exhausted included allowance reads 100%
  consumed even when purchased credit remains. Presto raises an
  exhausted-allowance alert in that case without calling the account rate
  limited.
- **Team usage.** Only your own activity is requested.

A missing or invalid balance shows as *unavailable*, never as zero. A passed
reset needs a fresh reading from Ollama; it is never turned into a made-up 0%.

## Set it up

1. Sign in at <https://ollama.com/settings/keys> and create a key named
   `presto-ai-dash-monitor`.
2. Copy the key. Ollama's documentation says keys do not expire, so note when
   you created it and rotate it yourself.
3. In the admin page choose **Add source -> Ollama Cloud**, give the account a
   name, paste the key into the key field and press **Save source**.

The key is encrypted before it is stored and is never sent back to the browser.
Leave the field blank to keep the saved key; use **Remove stored key** to delete
it.

## Check that it works

The card fills in on the next poll. If not, it says why:

| You see | Meaning and fix |
| --- | --- |
| **API key rejected (invalid or revoked)** | The key is wrong or was revoked. Create a new one |
| **Account suspended or access denied (HTTP 403)** | Ollama refused the account itself. Ollama keys have no permission levels, so this is not a missing permission on the key |
| **Rate limited (HTTP 429)** | Ollama allows 10 requests a minute across all your keys and devices. Presto waits for `Retry-After` |
| **Balance reporting is temporarily unavailable (HTTP 503)** | Ollama's side. The last reading stays, marked stale |
| **Cannot reach it / reporting schema changed** | Network from the daemon's computer, or Ollama changed a reply. The last good reading stays |

The balance can fail without blanking a good activity reading, and the other way
round; the card notes which one is missing.

## Revoke or replace the key

Revoke the key on <https://ollama.com/settings/keys>. Presto then shows *API key
rejected*. To rotate: create the new key, paste it into the source and save, then
revoke the old one.

## Refresh

The page offers a request timeout of 1 to 60 seconds and a refresh interval of
60 to 3600 seconds, 60 by default (older shorter settings are clamped to 60).
Ollama limits these routes to 10 requests a minute across all keys, so a lower
interval would not help. `Retry-After` on an HTTP 429 defers the next poll. Each
reading keeps its original observation time, and a failure keeps the last known
values marked stale.

## See also

- [Ollama](ollama.md), the local server connector
- [Sources](../concepts/sources.md)
