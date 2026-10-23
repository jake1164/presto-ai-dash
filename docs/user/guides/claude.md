# Claude

Claude is the one source that can be read three ways, and each way knows
something the others do not. This page explains all three, what each costs
you, and how to choose.

| Route | Gives you quota | Gives you activity | Needs |
| --- | --- | --- | --- |
| [Terminal status line](#the-terminal-status-line) | Yes | Yes (a reply happened) | One button in the admin page |
| [Local usage cache](#the-local-usage-cache) | Yes | No | One checkbox, and opening `/usage` now and then |
| [Local activity logs](#the-local-activity-logs) | No | Yes | One checkbox |

None of them sends a prompt, changes a provider setting or talks to a
provider you did not already use.

## The terminal status line

Claude Code can run a **status line**: a command it runs after a response,
which receives the session state on standard input. That state carries the
documented 5-hour and weekly allowance percentages and their reset times.
This is the supported route, and it is the one the dashboard was built
around.

Set it up in the admin page:

1. Add a **Claude** source, choose its name and the path to the Claude Code
   `settings.json` profile it should use, and enable it.
2. Save, then press **Set up Claude export** in that account's row.
3. Reopen Claude Code in a terminal using that profile and carry on working.

The quota appears after a response supplies the fields.

**What it does.** The helper runs under your own operating system account,
writes straight to the dashboard database, and works even while the daemon
is stopped. The only change on the provider side is the `statusLine`
command in the settings file you chose, which says where to deliver the
telemetry.

**What it preserves.** An existing status line is chained: its own JSON input
is passed to it and its output is printed. Other settings and status-line
options are kept. The previous status-line setting is encrypted in the
database before the file is atomically replaced, and **Remove Claude export
setup** puts it back if our command is still installed.

**Trade-offs.**

- The status line only runs in the terminal. The VS Code panel does not run
  it, so a panel-only user gets no quota this way. This is a property of
  Claude Code, not of the dashboard.
- It needs a terminal session to produce a reading.
- It is per profile. Two separately authenticated accounts need two Claude
  profiles and two source instances; one profile cannot be attached to two
  export accounts.

## The local usage cache

Claude Code caches the numbers behind its own `/usage` dialog on disk, in
`~/.claude.json` (a sibling of the `~/.claude` directory, not inside it).
The file holds a `cachedUsageUtilization` object with the fetch time, the
5-hour and weekly percentages with their reset times, and the included
credit.

Reading that file gives a panel user their quota back.

Set it up:

1. Open the Claude account on the admin page and tick **Read Claude Code's
   local usage cache (usage numbers only)**. It is off by default.
2. Leave **Claude Code state file** blank for the automatic search, or point
   it at the file yourself.
3. Open `/usage` in Claude Code once. That makes Claude Code fetch the usage
   and write its cache, so the quota appears immediately.

**What is read.** Only the fetch time, the two windows with their reset
times, and the included credit. The credit is found by *shape* - a dict with
numeric `limit_dollars` and `used_dollars` - never by its obfuscated key,
which Claude Code changes.

**What is never read, stored, logged or sent.** The same file also holds the
account id, the email, the organisation, the project history and the MCP
server list. The reader opens the file read-only, parses it in memory, and
extracts only the fields above. It never writes, renames, locks or forwards
anything else, and it never hashes those private fields for storage.

**Trade-offs.**

- **The format is not an official interface.** It can change without notice.
  The reader then reports the cache as unavailable and says which field
  changed. The terminal export and every other source are unaffected.
- **The cache refreshes at irregular moments**, not on every response. The
  numbers age between refreshes, and the card says how old they are. Opening
  `/usage` forces a refresh, and that is the usual fix.
- **The file is sensitive.** It is the one place in this project that holds
  data about your account that nobody asked for, which is why the reader is
  opt-in and reads only what it needs.

**Where the file is.** Claude Code's `CLAUDE_CONFIG_DIR` overrides the
configuration directory, which defaults to `~/.claude`; the documentation
does not name the state file itself. The reader therefore looks in two
places: inside the settings file's own directory (the custom layout) and,
only when that directory is the default `.claude` folder, in its parent (the
default layout). The admin page names the file it used.

**How often it refreshes.** The daemon keeps its own record of the distinct
fetch times it has seen, in memory only, at most fifty, empty again after
every daemon start. The admin page shows it per account as
`Usage cache refreshes seen: N in the last 24 h (last <age> ago, usually
every <gap>; since <time>)`. The gap is only shown with at least three
refreshes, because two do not make a rate.

## The local activity logs

Claude Code and its VS Code panel write session logs under
`<profile>/projects/**/*.jsonl`. The reader keeps only each request's
timestamp, the surface it came from, the model name and the token counts.

Set it up:

1. Open the Claude account on the admin page and tick **Read local Claude
   Code activity (timestamps and token counts only)**. It is off by default.
2. Set **Treat Claude as in use for (seconds) after a request** (default 300)
   if you want a different "in use" window.
3. Nothing else. The daemon re-reads only the bytes appended since its last
   poll.

**What it shows.** In use: a solid marker and a blue accent while a request
is newer than the active window. The detail card adds
`Last used: <age> ago (<surface>)` and `Log tokens 5h/7d: 120k/2.1M`. The
`Log` says the numbers come from the local session logs.

**What is never read, stored, logged or sent.** Prompt text, tool inputs,
file paths, session identities: anything else those files contain. The
reader opens the files directly from the profile path, never follows
symlinks out of that `projects` tree, and never sends the data anywhere.

**Trade-offs.**

- **It is activity, not quota.** No percentage is ever derived from tokens,
  because subscription limits are not published. Tokens are shown as load.
- **It is a lower bound.** Use on claude.ai, on a phone or on another
  machine never appears in these files.
- **The format is not an official interface.** A change degrades to "activity
  unavailable" rather than breaking the quota display.
- **A cold start takes a while.** A week of session logs is tens of
  megabytes, so the first refresh reads them in bounded chunks across the
  following polls. Until it is done, the card shows
  `Log tokens 5h/7d: scanning` instead of numbers, because the totals would
  be lower bounds. The last use is never delayed: the newest files are read
  from their end first.

**One response, several lines.** The same API response is written on several
lines, so counting lines would inflate the tokens several times over. The
reader counts each response once, by request id.

## Which one should I use?

| If you... | Use |
| --- | --- |
| work in a terminal and want accurate quota | the terminal status line |
| mostly use the VS Code panel and want quota | the usage cache, and open `/usage` now and then |
| want to know that Claude is in use, without quota | the local activity logs |
| want everything | all three; each is independent and each says which route a number came from |

The **Set up for VS Code** button in the account's Edit form ticks the two
readers for you. It only ticks the boxes; nothing is saved until you press
**Save source**.

## The weekly reset estimate

The weekly cycle is fixed, so when the last real weekly reset has passed the
card can show the next one as an estimate: `Wk reset ~4 days (est)`.

It is always labelled `(est)`, it is never shown as a provider reading, and
it is replaced by the next real reading. It assumes a fixed 7-day cycle
anchored in UTC, which can be about an hour out across a daylight-saving
change. The five-hour window is never estimated: its start rule is not
documented.

## Freshness and hints

Every route has its own threshold and its own advice. See
[Stale readings](../concepts/stale-readings.md) for the whole table.

Home keeps the labelled reset countdown (`Wk resets 5d`, for example) and
shows `Updated 8m` or `Updated 1h:05m` immediately to its left. Only the
update label changes colour: grey while idle or activity is unknown, green
while Claude is in use, and yellow while in use with a reading more than
15 minutes old. This is the quota reading's age, not the daemon's last poll.
The colour is an activity cue; the configured stale thresholds still apply.

The Claude card also carries the collector trail, as long as the report has
one:

- `Quota age: <age>` - how old the quota reading is,
- `Last reply: <age> ago` or `Last reply: unavailable`,
- `Reports: <host> <age> ago` or `Reports: never`.

## Privacy

Both readers work on this machine only. Nothing is written to either file,
and nothing is sent anywhere.

The reader's log messages name the file it reads and the problem it found,
never a value and never file contents. The messages are listed in the
[troubleshooting pages](../troubleshooting/no-claude-data.md).

## See also

- [Remote collectors](remote-collectors.md)
- [No Claude data](../troubleshooting/no-claude-data.md)
- [The VS Code panel](../troubleshooting/vs-code-panel.md)
