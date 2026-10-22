# The VS Code panel

If you use Claude Code in the VS Code panel, two things are true that are not
true in the terminal:

1. **The panel does not run the status line.** That is a property of Claude
   Code, not of the dashboard. The status line is the only *supported* source
   of Claude quota, so a panel-only user gets no quota from it.
2. **The panel writes the same session logs as the CLI.** So the dashboard
   can still tell you that Claude is in use, and roughly how hard.

## The symptom

The Claude account shows old quota, or none, and the hint says something
about a terminal.

## The fixes

If the daemon runs on another computer or in a container, it cannot read these
files. Run the collector's `watch` mode on the computer where the panel runs:
it pushes the usage cache and the activity to the daemon. See
[Remote collectors](../guides/remote-collectors.md).

### Use the usage cache

Claude Code caches the numbers behind its own `/usage` dialog on disk. The
dashboard can read that file.

1. Open the Claude account on the admin page.
2. Tick **Read Claude Code's local usage cache (usage numbers only)**.
3. Save, then open `/usage` in Claude Code once.

Now you get quota again. The reading is labelled `usage cache`, and it ages
between refreshes, because Claude Code refreshes the cache at irregular
moments. Opening `/usage` refreshes it at once.

The **Set up for VS Code** button in the Edit form ticks the two readers for
you. It only ticks the boxes: press **Save source** afterwards.

### Use the activity logs

1. Tick **Read local Claude Code activity (timestamps and token counts
   only)**.
2. Save.

Now the card shows `Last used: 3m ago (VS Code)` and the account gets a
solid marker and a blue accent while a request is newer than the active
window. While the reader is working, a stale quota alone does not raise a
yellow warning: the hint says what is stale instead.

## What this does not give you

- **Quota from the panel directly.** The panel does not run the status line,
  and there is no supported way to ask it for the allowance.
- **Activity from other machines.** The session logs are on the machine
  where Claude Code ran. Use on claude.ai, on a phone or on another computer
  never appears.
- **A percentage from tokens.** Subscription limits are not published, so
  tokens are shown as load, never as a share of a limit.

## If you want both

The terminal status line and the panel can both feed the same account, as
long as they use the same profile. The dashboard keeps the newest reading
per window, whatever route it came from, and says which route produced each
number. The terminal route wins a tie, because it is the supported one.

If you use two separately authenticated accounts - a terminal one and a
panel one - create two source instances with two profiles. One profile
cannot be attached to two export accounts.

## See also

- [Claude](../guides/claude.md)
- [No Claude data](no-claude-data.md)
