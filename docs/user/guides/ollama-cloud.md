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

One authenticated `GET` against the provider's usage endpoint, with
redirects disabled. The response carries dated activity totals:

- the number of requests,
- input, cached and output tokens,
- the reported USD activity,
- the date range the provider returned.

The card shows the totals separately, with the date range, so an activity
total is never mistaken for an allowance.

## What it does not show

The verified response does **not** contain the monthly credit allowance, the
remaining balance, or a reset time. Those stay *unavailable*. They are not
inferred from the plan price, because that would be a guess presented as a
measurement.

The Monthly credits meter on the Home row therefore shows `--` with an empty
bar. The account's own activity row still shows the real activity totals.

## Refresh

The page offers a request timeout of 1 to 60 seconds and a refresh interval
of 30 to 3600 seconds, 60 by default. A cached read keeps its original
observation timestamp, and a failed read makes the retained activity visibly
stale rather than resetting it.

## See also

- [Ollama](ollama.md), the local server connector
- [Sources](../concepts/sources.md)
