# The admin page

The admin page is where you configure the daemon. It is served on
`127.0.0.1` only, by the daemon itself, and it is plain HTML, CSS and
JavaScript with no dependencies and no third-party assets.

Open it with:

```bash
python -m presto_ai_dash.app --config dash.toml --admin
```

Then browse to <http://127.0.0.1:8788> and unlock it with the **admin**
token from the startup log. That token is not the shared token the Presto
uses.

## Logging in

The browser stays logged in for **seven days**, including page reloads,
browser restarts and daemon restarts with a freshly generated admin token.
The browser holds an HttpOnly, SameSite=Strict cookie; the database holds
only its hash, an expiry and a separate CSRF token. The startup token is
never stored in the browser.

**Lock configuration** revokes the current browser session on the server.
Expiring requires the current startup token to log in again. Sessions
survive only when the same database is reused. At most 25 sessions are kept.

**Settings > Browser sessions > Sign out of every browser** ends every session
at once, including the ones in browsers you no longer have. If you set the admin
token yourself, changing it also ends every older session. Behind a TLS proxy,
set `admin.secure_cookie` so the cookie is marked `Secure`.

Use the same browser address and port each time: `localhost` and
`127.0.0.1` have separate cookies.

On a headless server, forward the port over SSH:

```bash
ssh -L 8788:127.0.0.1:8788 you@your-server
```

## The three tabs

The active tab is in the URL hash (`#services`, `#status`, `#settings`), so a
link points at a tab. It is remembered in `localStorage` for the next visit,
and a browser that refuses `localStorage` still works.

The tabs follow the ARIA tab pattern: they are reachable with the keyboard,
the left and right arrow keys move between them, and Home and End jump to
the first and the last one.

### Services

Configure and arrange the accounts. Every account is a card that is collapsed
by default and shows, on one line:

- a status dot,
- the account name,
- its key numbers, such as `5h 6% / Wk 59%` or `in use 3m`,
- a freshness chip: `fresh 3m`, `stale 2d` or `no data`,
- a chip saying where the numbers came from: `terminal export`,
  `usage cache` or `local logs`.

Opening a card shows everything the page has always shown for that account:
the window meters, the labelled **Quota**, **Activity**, **Usage cache** and
**Status note** lines, and that account's own actions - the Claude export
setup, and a line saying whether remote reports are on, with a link to where they are set up.

Every card uses the same layout: **Readings**, **Setup**, **Alerts**, then
a footer containing its setup guide, account actions guide, and remote-report
navigation when supported. Provider-specific buttons stay in Setup; links
stay in the footer.

The order arrows, the **Enabled** checkbox, **Edit** and **Add source** stay
where they were. Everything on this tab applies to one account at a time.

### Status

Read-only, and it answers "is everything working?". It refreshes itself
every ten seconds while the tab is open and the browser tab is in the
foreground.

| Section | What it shows |
| --- | --- |
| Daemon | Version, database schema version, uptime, poll interval, start time, the bind address, the log level |
| Presto devices | Every device the daemon keeps a durable record for: online/last seen, the revision it reports and the one this daemon serves, whether they match, its update state, what to do about it, and its update history |
| Daemon release | Whether a newer published daemon exists, from the one registry this installation uses (PyPI for pip and pipx, the GitHub Container Registry for the Docker image). The daemon checks in the background about once a day; **Check for updates** asks it to look again now (at most once a minute). The block shows the running and newest eligible version, when it last checked, an official details link, and copyable commands for the actual target. Opening the page contacts no registry, a failed or old check says so instead of claiming you are up to date, and nothing is ever upgraded for you. Set `DASH_RELEASE_CHECK=off` to turn the checks off |
| Clients | Every device that fetches the status endpoint: its name, address, when it last fetched, how often, the app revision it reports, and what it says about itself: last error, failures since boot, free memory, signal and uptime. A client that has not fetched for a day drops off the list. The USB setup shows as `setup` while it checks that a Presto can reach the daemon |
| Accounts | One row per account: when it was last collected, its status, and the last problem in plain words |
| Readers and collectors | The local activity reader, the usage cache reader, and the remote collectors with their last report and host |
| Problems | The recent `WARNING` and `ERROR` messages, de-duplicated |
| Log | The daemon's recent log lines, newest first, with a level filter, a search box and **Show more** |

**Presto devices** and **Clients** answer different questions. A client row is a
name and an address seen in memory since the daemon started, and it is gone
after a restart. A device row is keyed by the device ID the setup utility
generated, it is stored in the database, and it survives a restart so an
unresolved problem is not forgotten. A device only gets a row once it runs a
build that sends its device ID; an older app still appears under **Clients**
with its revision.

The revision is what the device says about itself. It is a content hash of the
public app files, and a different hash is neither older nor newer: it is only
different. When it differs from the revision this daemon serves, the row says
**Update over USB** and points at **Set up a Presto** on the Settings tab; there
is no over-the-air update yet, and nothing here will start one.

The daemon has one shared token, so authentication cannot tell clients
apart. Each client therefore declares a name in the `X-Presto-Client`
header and may declare its app revision in `X-Presto-Revision`. The Presto
sends `DEVICE_NAME` from its own config (default `presto`); the host-side
preview sends `psl`. Header values are untrusted: a value is kept only when
it is printable ASCII of at most 32 characters, and anything else becomes
`unknown`. At most 16 records are kept, one per name and address, in memory
only.

The address is best effort. Behind NAT, a reverse proxy or a container
runtime several clients can share one address, so the name is what identifies
a client.

A client may also send `X-Presto-Diag`, a short summary of its own state:
the last failed fetch and where it stopped, the failure count, the free heap,
the uptime, the signal and the Wi-Fi drops. The daemon checks every pair
against a fixed list, keeps only what it knows, and forgets it all when it
restarts. A device that cannot reach the daemon cannot report a problem
either, so read **Last seen** first.

The **Problems** list holds only the log message: paths and
credential-shaped text are replaced and an exception contributes its type
only, so no file contents and no secrets can end up on the page. The buffer
is memory only and is empty again after a restart.

The **Log** shows the last 2,000 lines the daemon logged, at the daemon's own
log level (`INFO` unless `DASH_LOG_LEVEL` says otherwise; set it to `DEBUG`
to see more). It opens with the newest 100 lines: **Show more** adds 100 at a
time, the level menu keeps that level and above, and the search box keeps the
lines that contain your words, in the message or in the part of the daemon
that wrote it. A line that repeats, such as a source failing at every poll, is
listed once with a count (`x12`) and moves to the top each time it happens, so
it cannot push everything else out. The lines are cleaned the same way as the
Problems list. The log is memory only and is empty after a restart; for the
full history use `docker logs`.

### Settings

What applies to every service, in collapsible sections. The first one is
open, the rest are folded away. A section that applies to one device rather
than to the daemon says so in its badge.

| Section | What is in it |
| --- | --- |
| Home display | The overflow strip setting |
| Set up a Presto | The one-time USB setup download: the daemon address, the unlock code, `presto_setup.py` and `presto-setup.enc`. See [Set up the Presto](../getting-started/set-up-the-presto.md#the-guided-setup) |
| Remote reports | The ingest switch, the collector download and its SHA-256, the remote tokens, the collector trail and version, the plain-HTTP warning |
| Demo mode | The sample-data switch, and what it does and does not touch |
| Alerts | The master switch, the thresholds, the sound switch (off until you turn it on), quiet hours, pop to top, the mute length |
| Effective daemon configuration | Read-only: every value and where it came from |
| About and help | What the sources are, the order note, where the logs and the diagnostics commands are |

**Set up a Presto** prepares both files for *this running build*, so a device
installed from it carries the same app revision the daemon expects. The unlock
code it shows once is not in either download, and the encrypted file holds the
persistent shared device token, never the admin token. Losing the code or the
file costs a new download; it does not revoke the device token.

The effective configuration names the origin of every value:
`environment`, `dash.toml`, `database`, or `default`. Those values need a
daemon restart, so they are edited in the configuration file or the
environment rather than on the page.

## Demo mode

Demo mode puts a scripted set of six sample accounts in front of the real
ones. While it is on, the Presto shows the sample data with its own
*DEMO - SAMPLE DATA* chip, and the status API and the card-detail route
serve the same data. Nothing else changes: the real accounts keep their
settings and keep being read, and no provider is contacted because of demo
mode.

Demo mode shows the alerts as you have set them up, sound included. The
sound is off until you tick **Sound on a new warning**, so a demo never beeps
unless you asked for it; tick the box and the scenes chirp, one for a warning
and two for a critical. Quiet hours and the mute length apply too.

The switch is on the Settings tab. It is off by default. When the
demonstration is over, switch it off and the Presto goes back to the real
readings on its next poll, with no restart.

The Presto polls every three seconds while demo mode is on so the short
scenes are all visible. Its configured live polling interval resumes when
demo mode is turned off.

Two shortcuts save a new reader from hunting for it. While no account exists,
the **Services** tab offers **Want to see how it works first? Show sample
data.**, and the button turns demo mode on. While demo mode is on, the banner
at the top of every tab carries **Turn demo off**; when you do have real
accounts, the same banner says they are hidden while demo is on, because the
numbers on the Presto are not theirs.

- **It overrides, it does not replace.** The real sources are not disabled
  or edited while demo mode is on, and the readings that were collected are
  kept.
- **A bell tap is remembered in memory only.** The sample accounts have no
  stored settings, so a tap changes what this daemon serves until it
  restarts, and nothing in the database.
- **A real account cannot be tapped while demo mode is on.** The API answers
  `404` for its id, so the demo can never change a real service's alerts.
- **The environment wins.** `DASH_DEMO=1` turns demo mode on and locks the
  switch here; see [the configuration reference](../reference/configuration.md).

While demo mode is effective, a banner at the top of every tab says so, so a
reader never mistakes the sample numbers for their own.

## Adding and editing an account

**Add source** creates one, **Edit** changes it, and the **Enabled**
checkbox stops or resumes the collection without deleting anything.
Unchecking keeps the settings, the stored key, the history and the display
position.

Drag an account by its grip to reorder it, or use the up and down buttons.
The order saves automatically and survives restarts. The Presto Home list
and the swipe cards follow it on the next successful poll. Disabled accounts
keep their position but are hidden on the device.

For an existing key, leaving the key field blank preserves it. Entering a
new key replaces it. **Remove stored key** deletes it. Keys are never
returned by the API.

Changes take effect without restarting the daemon. An in-flight collection
may finish first; its result is discarded if the configuration changed or
the account was disabled in the meantime.

## The account's own actions

Some actions belong to one account and appear inside its card:

| Action | Which sources |
| --- | --- |
| Set up / Remove Claude export | Claude |
| Create, rotate and revoke a remote token | Claude (on Settings, under Remote reports) |
| Codex sign-in, sign-out and cancel | Codex (admin API routes only: the page has no buttons for them yet, so sign in with Codex itself) |

These are the only actions in the whole project that change something on a
provider account, and they only ever run when **you** press the button. They
never run from a poll and never run from a remote request.

## Privacy of the page

Every value that comes from the daemon is written with `textContent`, so a
report can never become markup. The page loads nothing from a third party:
no fonts, no scripts, no analytics.

## The help links

The **Help** button in the header opens a small menu with help for the
current tab, provider setup guides, and troubleshooting missing or stale
readings. Escape closes the menu and returns focus to Help; clicking
outside it also closes it.

Descriptive guide links also appear inside expanded settings, open account
cards and the source editor. Short explanations stay beside the fields.

The links are plain anchors and work with the keyboard. They carry nothing
identifying: the URL has no query string, no
install id, no host, no token and no account, and the page's own address is
never sent as a Referer. The links point at the public documentation site,
so nothing about your installation leaves the machine.

Everything the page explains is still written on the page: the help links
are an extra, not a replacement. The **Settings -> About and help** section
says so too.

## See also

- [Home display and the overflow strip](home-display.md)
- [Alerts and the card bell](alerts-and-the-bell.md)
- [The admin page will not open](../troubleshooting/admin-page.md)
