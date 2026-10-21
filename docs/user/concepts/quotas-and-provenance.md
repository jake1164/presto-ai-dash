# Quotas and where numbers come from

Every number on the Presto has an origin. This page is the rule book for
those origins, so a reading never looks more certain than it is.

## Allowance windows

A **window** is one period of an allowance: Claude's five hours, Claude's
week, Codex's primary bucket, OpenCode Go's month. Each window carries:

| Field | Meaning |
| --- | --- |
| `label` | The provider's own name for the period: `5h`, `Weekly`, `Monthly` |
| `period_minutes` | How long the period is, when the provider says so |
| `used_percent` | How much of it is used, or nothing at all when it is unknown |
| `resets_at` | When it resets, in UTC |
| `observed_at` | When the dashboard saw the reading |
| `state` | `available`, `unavailable`, `unlimited`, or `stale` at read time |

The label is the provider's, not ours. A five-hour allowance is called `5h`
and never "daily", because it is not a daily quota.

`used_percent` is `null` when the provider did not report it. It is never
`0`. An unknown number and a zero number are different facts, and only one of
them is a measurement.

## The provenance rule

> Every number or state shown to the user says where it came from. When a new
> source of information is added, it is labelled, not blended into an
> existing line.

In practice this means a reading is labelled with its source, and the
newest reading per window wins.

### Claude's three routes

Claude is the one account that can be read three ways, so it shows the
origin of every window:

| Label | What it means |
| --- | --- |
| `terminal export` | The quota the Claude Code status line reported |
| `usage cache` | The numbers Claude Code caches on disk for its own `/usage` dialog |
| `local logs` | Timestamps and token counts from the local session logs (activity, not quota) |

The newest reading per window wins, whatever its route. When the terminal
export and the usage cache report the same moment, the terminal export wins,
because it is the supported one.

The detail card says it in words: `Quota via export, 6 days old` or
`Quota via cache, 2 h old`. The admin page labels each window with its
route as well.

### Everything else

| Source | Provenance |
| --- | --- |
| Codex | `Source: local Codex app-server` on the card |
| OpenCode Go | The provider's own usage endpoint, read over HTTPS |
| Ollama Cloud | The provider's own usage endpoint, read over HTTPS |
| Ollama | The local server's own API, plus a samples file if you have one |
| LiteLLM | The gateway's own daily activity endpoint |
| Remote collectors | `Reports: <host> <age> ago`, with the host label you chose |

## Where the numbers come from, exactly

| Number | Source of truth | Read as |
| --- | --- | --- |
| Claude 5h and weekly quota | The Claude Code status line, or the local usage cache | HTTP from Claude Code, or a local file |
| Claude last used | The local session logs | A local file, read for timestamps and token counts |
| Claude included credit | The local usage cache | A local file |
| Codex allowance | The Codex app-server on this machine | A local subprocess, read-only methods |
| Codex banked resets | The same Codex response | The same read, no extra call |
| OpenCode Go windows | The provider's usage endpoint | One authenticated HTTPS GET |
| Ollama Cloud activity | The provider's usage endpoint | One authenticated HTTPS GET |
| Ollama health | The local Ollama API | One HTTP GET per endpoint |
| Ollama speed | A samples file written by a collector | A local file; never measured by prompting |
| LiteLLM usage | The gateway's daily activity endpoint | One authenticated HTTPS GET |

The daemon always owns the clock. A reading that arrives from another
machine is stamped with the time the daemon received it, so a remote
collector's clock cannot make a stale number look fresh. The one exception
is the Claude usage cache, which carries its own fetch time because it can
be hours old; the daemon caps it at the receive time and labels it.

## What is never derived

- **No percentage is ever computed from tokens.** Subscription limits are not
  published, so tokens are shown as load, never as a share of a limit.
- **No request history is reconstructed.** Polling a service's health tells
  you it is up, not what it did.
- **No amount is inferred from a plan.** Ollama Cloud's monthly allowance is
  not derived from its price.
- **No window is invented.** A provider that reports one window gives one
  window.

## Resets

Reset times are UTC, because the device has no time zone. The device shows
them as a countdown first (`resets in 3h`) with the absolute time dimmed
next to it.

A window whose reset time has passed is *not* zero. It shows `--` with an
empty bar on Home, and the last known value only on the detail card, where
it is labelled as the previous reading. The next real reading replaces it.

## See also

- [Stale readings](stale-readings.md)
- [Alert statuses and thresholds](alerts.md)
- [The status payload](../reference/status-payload.md)
