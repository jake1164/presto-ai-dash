# Stale readings

A reading that was fine when it was taken but is now too old is **stale**.
The dashboard shows it dimmed, marks it `STALE` on the card, and never
presents it as a live number.

## Why it happened

| Cause | How to tell | What to do |
| --- | --- | --- |
| The service was unreachable for a while | The Status tab names the last problem | Fix the service, or check its `base_url` |
| The daemon was stopped | No collection, no new reading | Start the daemon |
| The collector stopped | `No report <age> - run claude` | Run Claude Code in a terminal |
| The usage cache is simply old | `Quota <age> old - open /usage` | Open `/usage` in Claude Code |
| The window's reset has passed | The window shows `--` | Use the service again |
| The provider stopped reporting a field | The window says *unavailable* | Nothing you can do; it is the provider's shape |
| A reader cannot read its file | The hint names the reason | See [No Claude data](no-claude-data.md) |

## What "too old" means

The threshold depends on the route:

| Route | Threshold |
| --- | --- |
| A normal quota source | Three times the refresh interval, or three minutes, whichever is longer |
| The Claude terminal export | `Mark idle readings stale after (seconds)`, 900 by default |
| The Claude usage cache | `Mark usage cache readings stale after (seconds)`, 7200 by default |

Each window of a merged report is judged by the route it came from, so a
fresh cache reading next to an old export reading is not dragged down by the
export's threshold.

Both Claude thresholds are in the account's Edit form, on the admin page.

## The difference between stale and expired

An **expired** window has passed its reset time. Its last value is no longer
true, so Home shows `--` with an empty bar, and the detail card keeps the
previous value and says what it was. It is never `0%` and never a dimmed
number on Home.

## A stale reading never alerts

Only a fresh, available window can raise an alert. A stale window, an
unavailable window, a window whose reset has passed, and a window that
resets within the next 30 minutes all keep their presentation and raise
nothing.

## Where to look first

The admin page's **Status** tab:

- **Accounts** - one row per account, with the last collection time, the
  status and the last problem in plain words.
- **Readers and collectors** - whether the local activity reader is
  scanning, when the usage cache was fetched, and when each remote collector
  last reported.
- **Problems** - the recent warnings and errors, de-duplicated.

## See also

- [Stale readings, the rules](../concepts/stale-readings.md)
- [No Claude data](no-claude-data.md)
