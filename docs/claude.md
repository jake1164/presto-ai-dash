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
  an awaiting-export explanation. Restarting the daemon alone cannot supply data.

This is a **Claude Code terminal export**. Coverage from the VS Code graphical
panel has not been verified, and terminal inactivity must not be presented as
fresh panel telemetry. Use terminal mode for the verified reporting route.

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
A separate comparison against Claude's usage page and graphical-panel workflow
coverage remain unverified; terminal observations must not imply panel coverage.
