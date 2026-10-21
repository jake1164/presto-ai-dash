# LiteLLM

LiteLLM is a gateway in front of your models. The dashboard reads the usage
it routed.

## Set it up

1. Open the admin page and choose **Add source -> LiteLLM**.
2. Give the account a name.
3. Set `base_url` to the gateway, for example `http://192.168.1.50:4000`.
4. Paste the master key into the key field, or point `api_key_env` at an
   environment variable that holds it.
5. Press **Save source**.

Two LiteLLM accounts can sit side by side for two different gateways or two
different keys. Their usage is reported separately, never added together,
because two keys may watch overlapping traffic.

## What it reads

| Endpoint | Auth | Used for |
| --- | --- | --- |
| `GET /health/liveliness` | none | Is the process alive |
| `GET /health/readiness` | none | Is it ready, and is the database there |
| `GET /user/daily/activity` | bearer key | Requests, tokens, spend, errors, top model |

The card shows:

```text
requests_today
tokens_today
spend_today          or "unavailable"
top_model            or "unavailable"
errors_today
```

## Three things that matter

**`GET /health` is not used.** That endpoint runs a real test request
against every configured model, and it therefore costs tokens. The dashboard
never makes a paid model call, so it uses the two cheap health endpoints
instead.

**Spend needs a database.** LiteLLM's spend tracking needs a
`DATABASE_URL`. If the proxy runs without one, there is nothing to report,
and the card says **unavailable**. It never shows `$0.00`: zero spend and
unknown spend are different facts.

**`GET /daily_metrics` no longer exists.** Daily spend and usage moved to
`GET /user/daily/activity`. Anything still calling the old path is stale.

## Fail-soft by design

An unavailable endpoint or a missing field makes that one metric
*unavailable* and does not take the daemon down. Response metadata totals
are preferred when they are present; absent fields are never reported as
zero.

`Errors today` stays visible, but a non-zero daily count alone does not turn
a healthy source into a warning: it cannot tell earlier failures from
current ones. Current authentication or permission failures still warn, and
liveness failures still report *offline*.

## Limits

- **Only routed traffic is counted.** Usage that bypasses the gateway is not
  in these totals, and the dashboard never presents them as complete.
- **Rate-limit warnings are not synthesised.** There is no verified
  LiteLLM endpoint that reports "you are approaching a rate limit", so the
  dashboard does not invent one.
- **"Recent errors" is a count, not a list.** `failed_requests` is a number
  for the day. The individual records would need an endpoint the dashboard
  does not use.
- **The API is a moving target.** Only endpoints in the OpenAPI spec are a
  public contract. Anything else may change or disappear in any release
  without notice.

## See also

- [Ollama](ollama.md)
- [Sources](../concepts/sources.md)
