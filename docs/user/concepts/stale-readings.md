# Stale readings

A reading goes stale when it stops being true. The dashboard has to decide
when that is, and it has to say so on the screen, because a stale number
shown as a live one is worse than no number at all.

## Two kinds of staleness

| Kind | What it means | How you see it |
| --- | --- | --- |
| **Old** | The reading was fine when it was taken, but it is now too old to trust | The value is dimmed, and a chip says `STALE` |
| **Expired** | The window it belongs to has reset, so the value no longer applies | `--` and an empty bar on Home |

An expired window is never shown as a dimmed number on Home. It is not zero
either. The detail card keeps the last known value and says what it was.

## When a reading becomes stale

Every account has a freshness threshold. For a quota source it is three
times the refresh interval, or three minutes, whichever is longer. For
Claude it is different for each route:

| Route | Threshold | Default | Why |
| --- | --- | --- | --- |
| Terminal export | `Mark idle readings stale after (seconds)` | 900 | The status line only runs after a Claude reply |
| Usage cache | `Mark usage cache readings stale after (seconds)` | 7200 | The cache is only refreshed when `/usage` is opened |

Each window of a merged report is judged by the route it came from, so a
fresh cache reading next to an old export reading is not dragged down by the
export's threshold.

A reading also becomes stale the moment a collection fails, and again when
its window's reset time passes.

## What makes a reading old

| Cause | Effect |
| --- | --- |
| The account was never collected | No reading at all; the account shows *no data* |
| The collector stopped | The reading ages and goes stale |
| The service became unreachable | The reading goes stale immediately |
| The window reset | The window shows `--` |
| The provider stopped reporting a field | That window becomes *unavailable*, not zero |
| The reader cannot read its file | The reader reports why; the account keeps its last reading |

## The hints

When there is nothing fresh to show, the daemon computes one short line, at
most 28 ASCII characters, and sends it with the payload. It ages between
polls, so the advice is current when the device draws it.

### Claude, terminal export only

| Situation | Hint |
| --- | --- |
| No reading yet, and no collector report | `Run claude in a terminal` |
| The collector is alive but no window was ever measured | `Waiting for a Claude reply` |
| Every measured window's reset has passed | `Awaiting refresh` |
| Measured windows exist, none fresh, collector seen recently | `Reply in claude to refresh` |
| Measured windows exist, none fresh, collector quiet | `No report 6d - run claude` |
| At least one window is fresh and measured | no hint |

### Claude, with the usage cache reader on

The cache rules speak first, because opening `/usage` is what refreshes it:

| Situation | Hint |
| --- | --- |
| A fresh cache reading | no hint |
| Every measured reset has passed | `Reset passed - open /usage` |
| Claude is in use | `In use - quota 2h old` |
| Anything else | `Quota 2h old - open /usage` |
| No cache file and no reading yet | `Run /usage in Claude Code` |

### Claude, with the local activity reader on

| Situation | Hint |
| --- | --- |
| In use, quota readings exist | `In use - quota 6d old` |
| In use, no quota ever | `In use - no quota data` |
| Used recently, quota old | `Used 3h ago - quota 6d old` |
| Used recently, every reset has passed | `Used 3h ago - quota expired` |
| Quota fresh | no hint |

## A stale reading is never an alert

Only a **fresh, available** window can raise an alert. A stale window, an
unavailable window, a window whose reset has passed, and a window that
resets within the next 30 minutes all keep the presentation they have and
raise nothing. A window that is about to clear is not a warning.

The status may still be raised to `warning` by the staleness itself, but
never higher because of it, and never to `offline`: a provider that is not
reporting is not the same as a provider that cannot be reached.

## Where the age is shown

| Surface | What it shows |
| --- | --- |
| Home | The hint in the note slot, in dim ink, in place of the reset note |
| Detail card | `Quota age: <age>`, `Last reply: <age> ago` or `Last reply: unavailable`, and `Reports: <host> <age> ago` or `Reports: never` |
| Admin page | A freshness chip on each account: `fresh 3m`, `stale 2d`, `no data` |

## See also

- [Claude](../guides/claude.md)
- [Stale readings in practice](../troubleshooting/stale-readings.md)
