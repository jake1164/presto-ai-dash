# Claude subscription source

Claude is a reusable, UI-managed source for Claude Code's documented 5-hour
and weekly subscription quota fields. It uses the
[status-line reporting contract](https://code.claude.com/docs/en/statusline#rate-limit-usage).
These fields appear after a normal Claude Code response for supported
subscription logins. An API billing key is not needed.

## Setup and storage

1. In the local configuration UI, add a **Claude** source, choose its name and
   Claude Code `settings.json` profile path, and enable collection.
2. Save, then click **Set up Claude export** in that account's row.
3. Reopen Claude Code in a terminal using that profile and continue normal work.
   Quota readings appear after a response supplies the fields.

Account settings, idle freshness threshold, quota exports, and encrypted
status-line recovery data live in SQLite. There are no provider TOML files or
quota export files. The sole external setup change is the provider-owned
`statusLine` command in the selected Claude settings file, which tells Claude
where to deliver telemetry. No login credentials are read or copied.
The helper runs locally under the same OS account, writing directly to the
configured dashboard database; it works while the daemon is stopped too.
If the vault uses `DASH_ENCRYPTION_KEY`, the Claude terminal must inherit that
environment setting to preserve an existing encrypted status-line setup.
The normal Windows DPAPI-protected key file needs no extra terminal setting.

Existing status-line output is chained with its original JSON input. Other
Claude settings and status-line options are preserved; the previous status-line
setting is encrypted in SQLite before the file is atomically replaced.
**Remove Claude export setup** restores it if our command is still installed.
If the user changed the command afterward, removal preserves their new setting.
Deleting the source also removes its export setup. Change a profile path only
after removing its setup. Each separately authenticated account needs its own
settings profile and source instance; one profile cannot be attached to two
export accounts.

## Readings and freshness

- Only `rate_limits.five_hour` and `rate_limits.seven_day` subscription fields,
  normalized percentages/resets/timestamps, and a bounded Claude Code version
  are stored. Session/transcript identities, prompts, model response text, and
  credentials are discarded. Response-progress evidence is stored only as hashes.
- Optional windows are independent. Missing fields remain unavailable, not 0%.
  Context-window percentage and estimated session cost are not subscription usage.
  Gateway spend limits are outside this subscription source's current scope.
- Cached redraws, timer ticks, wall-clock session duration, and a new prompt
  alone do not refresh quota timestamps. Changed quota values/reset times or
  API-response progress provide evidence for a new observation. With no progress
  markers, unchanged values conservatively keep their prior timestamps.
- Idle readings become visibly stale after the UI-configured interval, default
  900 seconds. Reset times also invalidate old windows; no new zero is inferred.
  A later normal response restores fresh readings.
- Until a first real export, Claude shows warning and unavailable meters with
  an awaiting-export explanation. Restarting the daemon alone cannot supply
  data.
- Freshness hints explain what to do. The daemon computes a short ASCII hint
  (at most 28 characters) when the payload is built, so it ages between polls,
  and sends it in the quota report. First match wins: no report at all, or no
  collector report and no window ever measured, reads `Run claude in a
  terminal`; heartbeats only read `Waiting for a Claude reply`; every measured
  window's reset passed reads `Awaiting refresh`; measured windows exist but
  none fresh while the collector was seen within the stale threshold reads
  `Reply in claude to refresh`; otherwise `No report <age> - run claude`, where
  the age is that of the newest signal of any kind. A fresh window draws no
  hint. These cases are warnings, never provider `offline`.
- Detail cards for reports that carry a collector trail add three lines after
  the per-window reset lines, within the existing six-line budget (reset lines
  are never dropped; the lowest-priority line goes first when a line must go):
  `Quota age: <age>` (the age of the quota reading), `Last reply: <age> ago` (or `Last reply: unavailable`
  without a response-progress time), and `Reports: <host> <age> ago` (or
  `Reports: never`). Reset times stay UTC; other providers' cards are
  unchanged.

This is a **Claude Code terminal export**. Coverage from the VS Code graphical
panel has not been verified, and terminal inactivity must not be presented as
fresh panel telemetry. Use terminal mode for the verified reporting route.
The optional usage cache reader below adds a second, unofficial route for
panel users; every number it produces says which source it came from.

## Remote computers

A Claude Code setup on another computer (Windows, macOS, or Linux) can report
to this daemon through the optional observation ingest route, `POST
/api/v1/ingest`. The route and this feature are off by default; nothing new
listens until an administrator turns them on. The computer running the daemon
needs no extra setup, and the local status-line export above keeps working
as before.

1. On the daemon's admin page, tick **Accept remote reports** (or start the
   daemon with `DASH_INGEST_ENABLED=1`; see [setup.md](setup.md)).
2. Download the collector `claude_statusline.py` from the same page — it is
   the single-file Python 3.8+ script that also drives the local export.
3. In the remote account's row, click **Create remote token**. The token is
   shown once; copy it into a file on the remote computer, for example
   `~/.config/presto-ai-dash/claude.token`. The daemon stores only its hash.
4. On the remote computer, set the `statusLine` command in that profile's
   Claude settings file to the paste-ready snippet shown with the token, then
   edit the URL placeholder to the daemon's real address and check the token
   file path. Claude Code runs the collector after normal responses, exactly
   as it runs the local export.

The envelope carries quota windows, progress evidence hashes, and a short
host label only — no credentials, session identities, or prompt text. The
daemon owns time and applies the same freshness rules as the local export, so
a quiet remote computer ages into stale readings instead of a fresh 0%.
One account may report from several computers; the newest reading wins.

**Network safety.** The ingest route serves plain HTTP beside the status
endpoint. Anything on the network that can reach the daemon port may attempt
posts, so enable ingest only on a private network such as Tailscale, or put
it behind a reverse proxy with TLS. A token is write-only for one account:
it cannot read the status payload, list sources, or touch any other source,
and revoking it stops that computer's reports immediately.

## Local activity (VS Code panel and CLI)

The status line is the only source of Claude **quota**, and the VS Code panel
does not run it; panel users therefore see stale quota or none at all. The
optional local activity reader closes that visibility gap without quota: it
proves Claude **is in use on this machine** even while its quota is stale.
It changes no provider setting and makes no provider request.

1. Open the Claude account on the admin page and tick **Read local Claude
   Code activity (timestamps and token counts only)**. Off by default.
2. Set **Treat Claude as in use for (seconds) after a request** (default
   300) if you want a longer or shorter "in use" window.
3. Nothing else. The daemon re-reads only bytes appended since its last
   poll; no restart or Claude-side setup is needed.

**What is read.** The session log files Claude Code and the panel write under
`<profile>/projects/**/*.jsonl` next to the account's `settings.json`. The
reader keeps only each request's timestamp, entrypoint (surface), model name
and token counts, deduped by request id: the same API response is written on
several lines, so counting lines would inflate tokens about 2.7x. Tokens are
`input + output + cache creation`; cached reads are excluded as cheap against
limits. The first scan skips files untouched for eight days and is byte-capped;
memory keeps at most the last seven days.

**Cold start.** A week of session logs is around 70 MB, so the first refresh
cannot read all of it at once. The daemon reads it in bounded chunks across
the following polls (at most 64 MB per poll), and the reader says so: its
`complete` flag is false until every file inside the eight-day window has been
read to the end. Until then the token totals are lower bounds and the detail
card shows `Log tokens 5h/7d: scanning` instead of numbers. The last use is
never delayed: the newest files are read from their end first, so
`Last used: <age> ago (<surface>)` and the `active` marker are right on the
first refresh. Later refreshes only read what was appended and stay cheap.

**What is never read, stored or sent**: prompt text, tool inputs, file paths,
session identities — anything else those files contain. The on-screen result
is only an age, a surface label (`VS Code`, `CLI`), a model name and token
totals. The reader opens the files directly from the profile path in the
account's settings; it never follows symlinks out of that `projects` tree and
never sends the data anywhere (this machine only).

**What it shows.** In use: a solid marker and blue accent while a request is
newer than the active window, with the hint `In use - quota 6d old` (or
`In use - no quota data`) replacing the stale-quota warning — while the
reader works, stale quota alone never raises a yellow warning (the text says
what's stale instead). Recently used: `Used 3h ago - quota expired`. After a
quiet day the Phase 4 hints apply unchanged. The detail card adds
`Last used: <age> ago (<surface>)`, `Log tokens 5h/7d: 120k/2.1M` (the "Log" says the numbers come from the local session logs), and an
`Wk reset ~4 days (est)` line when the weekly reset has passed: the weekly
cycle is fixed, so the next reset is the newest real one plus whole weeks —
an estimate, never presented as a provider reading. The 5h window is not
estimated (its start rule is undocumented).

**About the weekly estimate.** It assumes a fixed 7-day cycle, so it is only
as good as that assumption: the provider may anchor the reset to local
midnight instead of a fixed UTC instant, which would move it by an hour
across a daylight-saving change. The estimate is replaced by the next real
reading, it is always labelled `(est)`, and it may be an hour off across a
clock change until it is checked against a real reading. The clocks change on
2026-11-01; the next real weekly reset after that is the first one to check
against. The estimate is not changed by this note.

**Limits.** The session file format is **not an official interface**; it can
change without notice, and the reader then degrades to "activity
unavailable" rather than breaking quota display. Activity is a **lower
bound**: use on claude.ai, a phone, or another computer never appears in
these files. No percentage is ever derived from tokens — subscription limits
are not published, so tokens are shown as load only. Quota percentages still
come only from the terminal status-line export above.

## Usage cache (VS Code panel and CLI)

The status line is the only *supported* source of Claude quota, and the VS
Code panel does not run it. Claude Code does, however, cache the numbers
behind its own `/usage` dialog on disk: the JSON file `~/.claude.json` (a
sibling of the `~/.claude` directory, not inside it) holds a
`cachedUsageUtilization` object with the fetch time, the 5-hour and weekly
percentages with their reset times, and the included credit. The optional
usage cache reader turns that file into a quota source, so a panel user sees
quota numbers again. It changes no provider setting and makes no provider
request; it only reads a file Claude Code wrote itself.

1. Open the Claude account on the admin page and tick **Read Claude Code's
   local usage cache (usage numbers only)**. Off by default.
2. Leave **Claude Code state file** blank for the automatic search, or point
   it at the file yourself.
3. Nothing else. The daemon re-reads the file only when its size or its
   modification time changed.

**What is read.** Only `fetchedAtMs`, the `five_hour` and `seven_day`
utilization percentages and their reset times, and the included credit. The
credit is found by *shape* (a dict with numeric `limit_dollars` and
`used_dollars`), never by its obfuscated key, which Claude Code changes.

**What is never read, stored, logged or sent.** The same file also holds the
account id, the email, the organisation, the project history and the MCP
server list. The reader opens the file read-only, parses it in memory and
extracts only the fields listed above. It never writes, renames, locks or
touches the file, and it never hashes those private fields for storage. The
account id is only compared, as a SHA-256 kept in memory, to notice that the
account behind the cache changed since the daemon started; when it did, the
readings are hidden until the daemon restarts or the source is re-saved.

**Where the file is.** Claude Code's `CLAUDE_CONFIG_DIR` (documented at
<https://code.claude.com/docs/en/env-vars>) overrides the configuration
directory, which defaults to `~/.claude`; the documentation does not name the
state file itself. The reader therefore looks in two places: inside the
settings file's own directory (the custom `CLAUDE_CONFIG_DIR` layout) and,
only when that directory is the default `.claude` folder, in its parent (the
default `~/.claude.json` layout). The admin page names the file it used, and
the **Claude Code state file** setting overrides the search.

**Where the numbers come from.** Every window says so: the detail card shows
`Quota via cache, 2 h old` or `Quota via export, 6 days old`, and the admin
page labels each window `usage cache` or `terminal export`. The newest
reading per window wins; when both sources report the same moment, the
terminal export wins. The included credit appears as `Credit: $100 of $100
(exp 11-05)`.

**The cache refreshes at irregular moments**, not on every response: it is
written when Claude Code fetches usage, which happens now and then, and
opening `/usage` in Claude Code forces a refresh. A reading that is older
than seven days is still shown, but it is labelled as old, and the status
note says that opening `/usage` refreshes it.

**The log messages.** The reader logs under `presto_ai_dash.claude_usage_cache`
and never logs a value or any file content — only paths, key names, JSON
types and exception class names.

| Message | What it means |
| --- | --- |
| `reading <path>` (INFO) | The file it reads. Logged once per file. |
| `FILE NOT FOUND` (WARNING) | No usage cache exists yet. It appears when Claude Code first fetches usage: open `/usage` once. |
| `FILE TOO LARGE` (WARNING) | The file is above the 32 MB safety limit, so it was not read. |
| `NOT A FILE` (WARNING) | The path is not a regular file, so it was not read. |
| `ACCESS DENIED` (WARNING) | The daemon user cannot read the file (a lock, antivirus or a shared-ACL problem). It goes away by itself after a few polls; only a persistent one is reported. |
| `FORMAT CHANGED` (WARNING) | The file no longer has the expected shape. The message names the field path and the type that was found, never a value. Claude Code was probably updated. |
| `NO QUOTA READING` (WARNING) | The file is valid but holds no quota for either window. |
| `ACCOUNT CHANGED` (WARNING) | The account behind the cache is not the one the daemon started with, so the readings are hidden. Restart the daemon or re-save the source. |
| `RECOVERED` (INFO) | The file is readable again after a problem. |
| `transient read problem` (DEBUG) | A poll hit a file that was being rewritten; the last known reading is kept. Two of these in a row are tolerated, the third becomes a persistent problem. |

A problem is warned about once, then at most once every 30 minutes; a change
of problem is reported at once, and a recovery always is. Nothing here ever
stops the quota reporting of the other sources.

**Diagnostics.** `python -m presto_ai_dash.collectors.claude_usage_cache
--path <file>` prints what the reader extracted (the windows, the fetch time
and the credit) or the same clear message, and exits 1 when nothing could be
read. Without `--path` it searches the default layout.

**Limits.** The file format is **not an official interface** (assumption E):
it can change without notice, and the reader then reports the usage cache as
unavailable instead of breaking the quota display. The banked "Full reset"
of Claude's usage page is **not** in this file. The reader is a local pull
source: on a container or another computer the file is not there, and the
cache would have to be pushed from the Claude machine (a later phase).


## Implementation checkpoint — 2026-10-11

The connector, UI setup/removal, SQLite schema v4 migration, and helper are
implemented. All **285 host tests pass**, including 16 focused Claude tests:
real Windows helper execution with synthetic fixtures, chained prior output,
minimal data, independent windows/accounts, cached/idle/reset freshness,
restart persistence, disabled/deleted accounts, setup rollback, settings
preservation, and authenticated cookie/CSRF setup/removal. Direct frontend
checks also pass for selectable Claude, hidden API-key fields, and setup/removal
buttons. These checks do not claim browser visual verification.

The live database was backed up before migration. After the user restarted the
daemon and completed normal work in a Claude Code terminal, a real export from
Claude Code **2.1.283** arrived at **2026-10-11 15:20:29 UTC**: **5h 0%**
(reset 2026-10-11 20:20 UTC) and **Weekly 34%** (reset 2026-10-15 04:00 UTC).
Actual status auth returns 401/200, and Claude reports healthy with both windows
available. No throwaway model prompt was sent to obtain a sample.

The connected Presto fetched these same percentages and reset times. All six
accounts/seven cards passed real-font layout, swipe/wraparound, selected-account
preservation, and clipped Home scrolling checks. The dashboard was restarted
after verification and COM3 released. M9 is **terminal implemented; live verified**.
A separate comparison against Claude's usage page remains unverified.

**Graphical-panel coverage (settled 2026-10-20).** The VS Code panel does not
run the status line: the author used the panel continuously for over a week with
the export installed and received no reading, while a single terminal session
produced real 5h and weekly numbers within minutes. Quota therefore arrives
from terminal sessions. Terminal observations must not imply panel coverage;
the optional local activity reader (below) shows panel use without quota.
The optional usage cache reader (below) is the third route: it reads the
numbers Claude Code caches on disk for its own `/usage` dialog, so a panel
user can see quota again, from an unofficial file that is labelled as such.
