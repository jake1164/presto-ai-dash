# No Claude data

The Claude account shows no quota, no activity, or a hint that says there is
nothing yet. This page lists the messages the dashboard can produce and what
each one means.

## The hints

| Hint | What it means | What to do |
| --- | --- | --- |
| `Run claude in a terminal` | Nothing has ever been measured and no collector has reported | Run Claude Code in a terminal, or turn on the usage cache reader |
| `Waiting for a Claude reply` | The collector is alive but no window was ever measured | Carry on working in Claude Code; the fields arrive after a response |
| `Awaiting refresh` | Every measured window's reset has passed | Use Claude again, or open `/usage` |
| `Reply in claude to refresh` | There are old readings and the collector was seen recently | Reply in Claude Code to get a fresh reading |
| `No report 6d - run claude` | Nothing has reported for six days | Run Claude Code in a terminal |
| `Run /usage in Claude Code` | The usage cache reader is on but Claude Code has not written the file yet | Open `/usage` once |
| `Reset passed - open /usage` | The measured windows have reset | Open `/usage` |
| `Quota 2h old - open /usage` | The cache reading is two hours old | Open `/usage` |
| `In use - quota 2h old` | Claude is in use, the quota reading is old | Nothing; the hint says what is stale |
| `Used 3h ago - quota expired` | Claude was used three hours ago and every window has reset | Use Claude again, or open `/usage` |

## The status line produces nothing

The status line only runs in a terminal, and only after a response.

1. Is the profile right? The source's **settings_path** must point at the
   `settings.json` of the profile you actually use. Each separately
   authenticated account needs its own profile and its own source instance.
2. Is the export still installed? Press **Set up Claude export** in the
   account's row again. It is idempotent.
3. Did the settings file change? **Remove Claude export setup** restores the
   previous command, and then you can install it again. If you changed the
   command yourself afterwards, removal keeps your version, which is the
   right behaviour but means the collector is no longer installed.
4. Did the profile path change? Remove the setup *before* changing the
   settings path.

## The usage cache reader complains

These messages come from the reader's log. The reader names the file it
reads and never a value.

| Message | What it means |
| --- | --- |
| `FILE NOT FOUND` | Claude Code has not written the cache yet. Open `/usage` once |
| `FILE TOO LARGE` | The file is above the 32 MB safety limit and was not read |
| `NOT A FILE` | The path is not a regular file |
| `ACCESS DENIED` | The daemon user cannot read the file. A lock, antivirus or a shared-ACL problem. It goes away by itself after a few polls; only a persistent one is reported |
| `FORMAT CHANGED` | The file no longer has the expected shape. The message names the field path and the type it found, never a value. Claude Code was probably updated |
| `NO QUOTA READING` | The file is valid but holds no quota for either window |
| `ACCOUNT CHANGED` | The account behind the cache is not the one the daemon started with, so the readings are hidden. Restart the daemon or re-save the source |
| `RECOVERED` | The file is readable again after a problem |

A problem is warned about once, then at most once every 30 minutes. A change
of problem is reported at once, and a recovery always is. None of it stops
the other sources.

## The activity reader complains

| Symptom | What it means |
| --- | --- |
| `Log tokens 5h/7d: scanning` | The first scan is still working through a week of session logs. The totals are lower bounds until it finishes |
| `Activity: unavailable (reason)` | The reader cannot read the profile's `projects` directory. The reason is in the message |
| No activity at all | Claude was not used on *this* machine since the reader was turned on |

The activity reader is a **lower bound**. Use on claude.ai, on a phone or on
another machine never appears in these files.

## Checking it by hand

The usage cache reader has a command-line mode that prints what it
extracted:

```bash
python -m presto_ai_dash.collectors.claude_usage_cache --path /path/to/.claude.json
```

It prints the windows, the fetch time, the credit and how many refreshes it
has seen, or the same clear message it would log. It exits 1 when nothing
could be read. Without `--path` it searches the default layout.

## The admin page's view

The account card lists the quota, the activity and the usage cache
separately, each labelled with where its numbers come from:

```text
Quota: usage cache - last reading 2026-10-20 14:22 (2 h old)
Activity: local logs - last used 3m ago (VS Code) - tokens 5h/7d 120k/2.1M
Usage cache: ok - fetched 2026-10-20 14:22 (2 h old), /home/you/.claude.json
```

The **Status** tab names the last problem in plain words, and its
**Problems** list holds the recent warnings.

## See also

- [Claude](../guides/claude.md)
- [The VS Code panel](vs-code-panel.md)
- [Stale readings](stale-readings.md)
