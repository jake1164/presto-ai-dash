> Public, user-facing version: <https://jake1164.github.io/presto-ai-dash/concepts/sources/>, <https://jake1164.github.io/presto-ai-dash/developers/how-a-source-works/>. This file is the internal reference and may go into more detail.
>
> Corrections (2026-10-21):
>
> - Six source types are registered: `ollama`, `litellm`, `opencode_go`, `ollama_cloud`, `codex` and `claude`. The demo is a daemon mode, not a source.
> - Claude's terminal export is live verified (M9), not pending.
> - "Adding a source" now has a full guide and a template that runs (`daemon/templates/source_template.py`, kept honest by `daemon/tests/test_source_template.py`): see How a source works and The worked example on the site.

# Sources

The daemon ships six registered sources: `ollama`, `litellm`, `opencode_go`,
`ollama_cloud`, `codex` and `claude`. A fresh database enables none of them.
Manage saved accounts, settings,
and encrypted keys in the [local UI](configuration.md). Sample data for demos
is the daemon's demo mode (`DASH_DEMO` or the admin page), which overrides the
readings without touching these sources.

A source is anything that can answer "what is the state of this thing right
now". Each source returns a `SourceSnapshot`:

```python
class SourceSnapshot:
    source_id: str
    status: str          # ok | active | warning | near_limit |
                         # rate_limited | critical | offline
    updated_at: datetime
    metrics: dict        # source-specific, may be partial
    alerts: list
    cards: list          # usually empty; the daemon builds cards
```

Adding a source means adding one module under `daemon/src/presto_ai_dash/sources/`
and registering its class. Trusted modules in `sources/` are discovered at
startup; there are no external entry points or package installation through the
browser.

The [configuration page](configuration.md) creates multiple instances of these
types. A class's `id` identifies the connector type; each configured instance
has a separate stable ID used for its snapshots and encrypted credential.
Renaming or replacing a key preserves that identity and history. Instances of
one type are reported separately; their usage is never automatically summed.

## Status vocabulary

Canonical values, most severe last:

```text
ok  <  active  <  warning  <  near_limit  <  rate_limited  <  critical  <  offline
```

`normal` is accepted as an alias for `ok` because the project plan uses both
spellings. It is normalised to `ok` on input.

| Status | Meaning | LED |
| --- | --- | --- |
| `ok` | Normal | green |
| `active` | Local model working right now | blue |
| `warning` | Degraded, or nearing a configured threshold | yellow |
| `near_limit` | Approaching a limit | yellow |
| `rate_limited` | A limit has been hit | red |
| `critical` | Broken, or a limit exceeded | red |
| `offline` | Unreachable | red |

## Demo mode (not a source)

**Purpose:** exercise every UI state without any real integration.

Demo mode is a switch on the daemon, not a registered source: `daemon/src/presto_ai_dash/demo.py`
builds its payloads with the same builders a real daemon uses, and the API
serves them while the switch is on. The scenario loops through every status in
the vocabulary above, in small steps, and never contacts a provider.

Because it is a mode, it needs no account row, cannot be confused with an
account, and can never write a provider-side setting. See
[the admin page](user/guides/admin-page.md) and the demo page on the site.

## ollama

**Purpose:** local model health and performance.

Endpoints used by the implemented source:

| Endpoint | Used for |
| --- | --- |
| `GET /api/version` | reachability and version |
| `GET /api/tags` | models available locally |
| `GET /api/ps` | models currently loaded in memory |
| (external sample file) | last observed request timing; the daemon itself sends no prompt |

Reported metrics (where returned by the configured proxy):

```text
reachable            yes | no
version              e.g. "0.5.1"
loaded_model         name from /api/ps, or null
available_models     count from /api/tags
last_measured_*      only if a samples file is configured and readable
```

### The honest limitation

**Ollama exposes no request history.** There is no endpoint that reports past
prompts, token counts or durations. The project plan states this in §6.3 and it
is correct.

So `last measured tokens/sec` can only come from something that *observed* a
request: a collector, shim, wrapper, or voluntary reporter. That observer is
future work and is not part of these source milestones.

The daemon reports reachability, version, available and loaded models. If
`sources.ollama.samples_file` points at a JSON file written by such a helper,
it reads the last sample and computes tokens/sec. It does not send prompts.

When no readable sample exists the card reads **"last measured: unavailable"**.
It does not guess, and it does not show zero.

### tokens/sec

From the Ollama documentation, verbatim:

> To calculate how fast the response is generated in tokens per second
> (token/s), divide `eval_count` / `eval_duration` * `10^9`.

All Ollama durations are in **nanoseconds**.

## litellm

**Purpose:** routed AI usage through the gateway.

LiteLLM only sees traffic routed through LiteLLM; direct `desktop-pc` calls
to Ollama are outside those usage totals. The v0 daemon is intended to run on
`home-server`; desktop direct-usage reporting is a future helper.

Endpoints used by the implemented source:

| Endpoint | Auth | Used for |
| --- | --- | --- |
| `GET /health/liveliness` | none | process liveness |
| `GET /health/readiness` | none | readiness, plus DB state |
| `GET /user/daily/activity` | Bearer key | daily spend, tokens, requests, errors, top model |

Reported metrics:

```text
online               yes | no
requests_today       from metrics.api_requests
tokens_today         from metrics.total_tokens
spend_today          from metrics.spend, or "unavailable"
top_model            from breakdown.models, or "unavailable"
errors_today         from metrics.failed_requests
```

### Notes that matter

- **`GET /daily_metrics` no longer exists.** Daily spend and usage moved to
  `GET /user/daily/activity`. Anything still calling the old path is stale.
- **`GET /health` is not used.** It runs a real test request against every
  configured model and therefore costs tokens. The project plan forbids paid
  test prompts unless explicitly enabled.
- **Spend needs a database.** If the LiteLLM proxy has no `DATABASE_URL`,
  spend tracking does not work at all. The card shows **"unavailable"**, never
  `$0.00`, because zero and unknown are different facts.
- **Fail-soft by design.** An unavailable endpoint or missing field makes that
  metric unavailable and does not take down the daemon. Response metadata
  totals are preferred when present; absent fields are never reported as zero.
- **Daily failures are history.** `Errors today` stays visible, but a nonzero
  daily count alone does not change a healthy source to warning. Current usage
  authentication/permission failures still warn, and liveness failures still
  report offline or critical. HTTP 403 reports missing usage permissions;
  HTTP 401 reports a rejected key.

### What is deliberately not reported

- **Recent errors.** `failed_requests` is a count, not a list. `GET /spend/logs`
  would give individual records but is not needed for a glanceable card and is
  out of scope for v0.
- **Rate limit warnings.** There is no verified LiteLLM endpoint that reports
  "you are approaching a rate limit". The `rate_limited` and `near_limit`
  statuses remain first-class in the model and in the demo scenario, so the UI,
  LED and sound paths are all exercised, but LiteLLM does not synthesise them.
  No endpoint was invented to fill the gap.

## opencode_go and ollama_cloud

OpenCode Go reads provider-reported 5h, Weekly, and Monthly percentages and
reset times from `GET https://opencode.ai/zen/go/v1/usage`. Each account has its
own encrypted key and configurable refresh interval in the database.

Ollama Cloud is separate from the keyless local Ollama connector. Its authorized
`GET https://ollama.com/api/usage` returns dated activity totals. Monthly credit
allowance, remaining balance, and reset are absent from the verified response;
those stay unavailable instead of being inferred from the plan price. Requests,
tokens, and reported USD activity are shown separately.

Both connectors use fixed HTTPS reporting routes with redirects disabled and
make no inference requests. See [quota contract, freshness, and verified coverage](quotas.md).
Codex subscription reporting is implemented for an existing local ChatGPT
login, with UI/database source settings and provider-owned OAuth. See
[Codex behavior, setup, and remaining M8 work](codex.md). Claude's
[terminal quota exporter](claude.md) is implemented with UI-managed settings,
SQLite telemetry, and real-export verification pending.

## Adding a source

1. Create `sources/mysource.py` with a `Source` subclass exposing a unique
   type `id`, `name`, and `collect() -> SourceSnapshot`.
2. Decorate it with `@register`. Its module is imported at daemon startup.
3. Declare `config_fields` with keys, labels, types (`int`, `url`, `string`),
   defaults, and integer bounds. The admin page and settings validation use
   these descriptors. Declare `secret_field` for an optional encrypted API key;
   the collector passes the decrypted key into that constructor setting.
4. Add fixture tests based on response shapes from the official documentation.

The managed collector assigns each object its instance ID and display name.
Use `self.id` in snapshots. New types appear in the status `sources` array and
overall summary. Each enabled instance automatically gets a swipe card; quota
reports get grouped Home bars through the shared contract. No provider-specific
device navigation code is needed for a new type.
Modules are trusted Python code installed with the daemon, not uploaded by users.

Do not mock an external API from assumption. Record a real response from the
official docs, or from the live service, and test against that.
