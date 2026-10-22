# How a source works

A source is a class in `daemon/src/presto_ai_dash/sources/`. This page
describes the contract: what you declare, what you return, and what the
daemon does with it.

## Registration

The daemon discovers the trusted modules in `sources/` at startup. A module
whose name starts with `_` is skipped.

```python
from . import register


@register
class YourSource(Source):
    id = "your_source"
    name = "Your Source"
```

`@register` adds the class to the registry, keyed by its `id`. The registry
is what the admin page's **Add source** list is built from, so a source that
is not registered cannot be configured - and cannot be discovered by
accident either.

The `id` is the connector type. Each configured account is an *instance* of
it with its own stable ID, name, settings and key.

At runtime the managed collector replaces the class's `id` and `name` with
the account's, so a snapshot always carries the account's identity.

## `config_fields`

Each setting the admin page offers is one descriptor:

```python
config_fields = (
    {"key": "base_url", "label": "Service URL", "type": "url",
     "default": "http://localhost:9000"},
    {"key": "timeout", "label": "Request timeout (seconds)", "type": "int",
     "default": 10, "minimum": 1, "maximum": 60},
    {"key": "refresh_seconds", "label": "Refresh interval (seconds)", "type": "int",
     "default": 60, "minimum": 30, "maximum": 3600},
    {"key": "activity_logs", "label": "Read the local logs", "type": "bool",
     "default": False},
)
```

| Key | Meaning |
| --- | --- |
| `key` | The name in the stored settings |
| `label` | What the admin page shows |
| `type` | `string`, `url`, `int` or `bool` |
| `default` | The value used when the account does not set one |
| `minimum`, `maximum` | The bounds for an `int` |

The management API accepts only the exact type. A checkbox setting must be
a real JSON boolean, so `"true"` is a validation error. That is deliberate:
an opt-in reader can never be half-enabled.

## `secret_field`

At most one setting is the secret:

```python
secret_field = "api_key"
```

It is encrypted with Fernet before it is stored, bound to the account ID so
it cannot be swapped between records, and never returned by the admin API -
the page only learns whether a key is present. The base class redacts it
from every message a source produces.

A source with no key sets `secret_field = None`.

## `collect()` and `SourceSnapshot`

```python
def collect(self):
    return self.snapshot("ok", metrics={"quota": report})
```

A `SourceSnapshot` has:

| Field | Meaning |
| --- | --- |
| `source_id` | Filled in by the collector |
| `status` | One of the canonical statuses |
| `updated_at` | When the reading was taken |
| `metrics` | The source's own measurements. May be partial |
| `alerts` | A list of `{"severity", "message"}` dicts |
| `cards` | Normally empty: the daemon builds the cards |
| `error` | The reason, when the status is `offline` |

The contract:

- **Never raise.** A source that fails returns an `offline` snapshot with
  the reason. `collect_safely()` catches anything that slips through and
  turns it into one too, but a clean source says what it knows.
- **`offline` means unreachable.** A metric that is missing is
  *unavailable*, not *offline*. Connection health, availability and quota
  exhaustion are three different facts.
- **Never invent a number.** An absent field is `null`, never `0`.

## Quota reports

`quotas.py` owns the quota model. A quota source builds a report:

```python
from ..quotas import quota_report, quota_window

report = quota_report(
    "example", "Example",
    [quota_window("five_hour", "5h", 12, "2026-10-20T15:00:00+00:00",
                  observed, 300)],
    stale_after_seconds=180)
```

`quota_window` takes the window ID, the provider's **own** label, the
percentage (or `None`), the reset time, the observation time, the period in
minutes and the state. It validates them: a negative or non-finite
percentage is refused, a duplicate window ID is refused, and a state other
than `available`, `unavailable` or `unlimited` is refused.

The label is what the user sees. Call a five-hour allowance `5h`, never
`daily`.

A window the provider did not report is `state="unavailable"` with a
`None` percentage. It is never `0`.

The report goes into the snapshot's metrics under the key `quota`:

```python
self.snapshot("ok", metrics={"quota": report})
```

## Provenance labels

A number always says where it came from.

- A window may carry a `source` label: a short ASCII string of at most 32
  characters, such as `terminal export` or `usage cache`. The label is the
  source's identity, never a value.
- The newest reading per window wins, whatever its route. A tie goes to the
  more supported route.
- A report may carry `collector_seen_at`, `collector_host`,
  `collector_version` and `last_progress_at`: the collector trail. Its
  presence is what makes the daemon compute a freshness hint, so a source
  without it is never decorated with advice that does not apply to it.

The provenance lines themselves are built in `quotas.py`, next to the
labels they name.

## The read-only rule

**The app observes provider accounts. It never acts on them.**

A source may read documented usage and limit data. It may not:

- send a prompt or a model request to produce a number,
- spend, redeem, reset or top up credits,
- change a plan, a limit or a setting,
- log in, sign out or refresh a token.

The only exceptions are the setup the *user* starts in the admin page:
connecting or disconnecting an account (Codex sign-in and sign-out) and
installing or removing the Claude status-line export. Those never run from
a poll and never run from a remote request.

If the provider's API offers an action - Codex's
`ConsumeAccountRateLimitResetCredit`, for example - do not wire it up.

## What the HTTP client enforces

A source that reads over HTTP uses `presto_ai_dash.http_client`
(`get_json`, `get_text`), which holds the rules so a source does not have to:

- **Redirects stay on the host.** A redirect is followed only to the same host
  and port, or from `http` to `https` on the same host. Anything else is an
  error, so a key in a header never reaches another server.
- **An answer is at most 4 MB**, and an error body is read only as far as its
  first 64 KB. A larger answer is refused as unreachable, not read into memory.
- **A body that is not JSON, or is nested too deeply to parse, is
  "unreachable"**, never an exception that escapes the source.
- A request has a timeout, and the source decides how long.

## The device payload whitelist

The daemon does not send the full payload to the device. It sends a **device
view**: only the fields the device reads, and each quota report once.

The whitelist is in `dashboard.py`:

```python
DEVICE_SOURCE_KEYS = ("id", "type", "name", "status", "quota", "metrics", "alerts", "marks")
DEVICE_QUOTA_KEYS = ("windows", "activity", "hint", "reset_credits")
DEVICE_WINDOW_KEYS = ("label", "used_percent", "state", "period_minutes",
                      "resets_at", "observed_at")
DEVICE_TOP_KEYS = ("sources", "card_order", "updated_at", "display", "alerts")
```

**The guard test** is `daemon/tests/test_device_payload.py`. It parses the
device's own source with `ast`, finds every key the device reads, and
requires each one to be on the matching whitelist. If you add a key to the
device and forget the whitelist, that test fails.

It also proves that the device draws exactly the same screens from the slim
payload as from the full one, and that the payload stays inside the size
budget: 5000 bytes in total, 1000 bytes per account.

So a quota source needs **no device change at all**. The daemon builds the
card, the Home row and the bars from the same contract every quota source
uses.

## Push sources

A source that accepts readings from another computer sets:

```python
accepts_push = True
push_window_ids = ("five_hour", "seven_day")
```

The ingest validator then only accepts those window IDs for that source.
See [Writing a collector](writing-a-collector.md).

## The checklist

1. The class is in `sources/`, is decorated with `@register`, and has a
   unique `id`.
2. `config_fields` declares every setting, with a type and a default.
3. `secret_field` names the credential, or is `None`.
4. `collect()` never raises, and `offline` means unreachable.
5. A missing measurement is `None`, never `0`.
6. Every window carries the provider's own label.
7. The provenance label says where the reading came from.
8. There is no provider write anywhere in the code path.
9. Tests cover the happy path, a missing field, a bad shape, an
   unreachable service and a rejected key.
10. The account appears in the admin page, the card draws correctly, and the
    payload stays inside the budget.

## See also

- [Writing a collector](writing-a-collector.md)
- [The worked example](worked-example.md)
