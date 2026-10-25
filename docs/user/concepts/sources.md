# Sources

A **source** is anything the dashboard can read to answer "what is the state
of this thing right now". Each source is one account you configured: a Claude
profile, a Codex login, an OpenCode Go key, a LiteLLM gateway, a local Ollama
server.

## One type, many accounts

The daemon ships a fixed set of source *types*. Each one you add is an
*instance* of a type, with its own stable ID, its own display name, its own
settings, its own encrypted key and its own readings.

That is why two LiteLLM gateways can sit side by side: they see different
traffic, they may use different keys, and their usage is reported
separately. The dashboard never adds two accounts of the same type together,
because two keys may watch overlapping traffic and the sum would be a lie.

Renaming an account or replacing its key keeps its ID and its history.
Deleting an account removes its key and stops the collection; its past
readings stay until the retention sweep removes them.

## The source types

| Type | Reads | Needs a key |
| --- | --- | --- |
| `ollama` | A local Ollama server: version, loaded model, available models | No |
| `litellm` | A LiteLLM gateway: requests, tokens, spend, errors, top model | Usually |
| `claude` | Claude Code: quota windows, local activity, usage cache | No |
| `codex` | A local Codex login: allowance buckets, resets, banked credits | Its own login |
| `opencode_go` | The OpenCode Go usage endpoint: three quota windows | Yes |
| `ollama_cloud` | The Ollama Cloud usage endpoint: activity totals | Yes |

Each type has its own guide under [Guides](../guides/claude.md).

## Where a source runs

Every source is a *pull* source by default: the daemon calls it, on its own
schedule, from the machine the daemon runs on.

Two exceptions exist, and both are opt-in:

- **Claude** can be reported by a *remote* collector on another computer
  (Windows, macOS or Linux) that pushes its readings to the daemon. See
  [Remote reports](../concepts/remote-reports.md).
- **Claude** can also read two local files on the machine where Claude Code
  runs: the session logs and the usage cache. Both readers are off by
  default and read only.

## The status vocabulary

Every account has one status. They are ordered, least severe first:

```text
ok  <  active  <  warning  <  near_limit  <  rate_limited  <  critical  <  offline
```

| Status | Meaning | LED |
| --- | --- | --- |
| `ok` | Normal | green |
| `active` | In use right now | blue |
| `warning` | Degraded, or something is stale | yellow |
| `near_limit` | Close to a configured threshold | yellow |
| `rate_limited` | A limit has been reached | red |
| `critical` | Broken, or a limit exceeded | red |
| `offline` | Unreachable | red |

The dashboard takes the status of the worst window of an account, so a
service with one window at 30% and another at 97% is *critical*, not *ok*.

`normal` is accepted as an alias for `ok` on input and normalised to `ok`.

## What a source is allowed to do

The rule is short and it is not negotiable: **the dashboard observes
provider accounts and never acts on them.**

A source may:

- read documented usage and limit data,
- read a file the user's own tool wrote,
- report a status and a set of measurements.

A source may not:

- send a prompt or a model request to produce a number,
- spend, redeem, reset or top up credits,
- change a plan, a limit or a setting,
- log in, sign out or refresh a token.

The one exception is the setup the *you* start in the admin page: connecting
or disconnecting an account (the Codex sign-in and sign-out) and installing
or removing the Claude status-line export. Those never run from a poll and
never run from a remote request.

If a provider's API offers an action - for example a method that redeems a
reset credit - the dashboard does not wire it up.

## Settings and secrets

Each source type declares the settings it accepts. A setting has a key, a
label, a type and a default:

- `string` and `url` become text boxes,
- `int` becomes a number box with the type's minimum and maximum,
- `bool` becomes a checkbox, and the stored value is a real boolean. A
  string `"true"` is refused, so an opt-in reader can never be half-enabled.

One setting per type may be the secret: the API key. A secret is encrypted
with Fernet before it is stored, is bound to its account ID so it cannot be
swapped between records, and is never returned by the admin API. The page
only ever tells you whether a key is present.

Plain settings and usage history are not encrypted. They are readable in the
SQLite file. That is deliberate: the encryption protects credentials, not
your own readings.

## What a source does not do to the dashboard

Adding a quota source needs no change on the Presto. The daemon builds the
card, the Home row and the window bars from the same quota contract that the
other sources use, so a new type appears on the device with no device code
at all.

See [How a source works](../developers/how-a-source-works.md) if you want to
write one.
