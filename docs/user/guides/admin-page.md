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

The order arrows, the **Enabled** checkbox, **Edit** and **Add source** stay
where they were. Everything on this tab applies to one account at a time.

### Status

Read-only, and it answers "is everything working?". It refreshes itself
every ten seconds while the tab is open and the browser tab is in the
foreground.

| Section | What it shows |
| --- | --- |
| Daemon | Version, database schema version, uptime, poll interval, start time, the bind address, the log level |
| Clients | Every device that fetches the status endpoint: its name, address, when it last fetched, how often, and the app revision it reports |
| Accounts | One row per account: when it was last collected, its status, and the last problem in plain words |
| Readers and collectors | The local activity reader, the usage cache reader, and the remote collectors with their last report and host |
| Problems | The recent `WARNING` and `ERROR` messages, de-duplicated |

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

The **Problems** list holds only the log message: paths and
credential-shaped text are replaced and an exception contributes its type
only, so no file contents and no secrets can end up on the page. The buffer
is memory only and is empty again after a restart.

### Settings

What applies to every service, in collapsible sections. The first one is
open, the rest are folded away. Every section is headed *Applies to all
services*.

| Section | What is in it |
| --- | --- |
| Home display | The overflow strip setting |
| Remote reports | The ingest switch, the collector download and its SHA-256, the remote tokens, the collector trail and version, the plain-HTTP warning |
| Alerts | The master switch, the thresholds, the sound switch (off until you turn it on), quiet hours, pop to top, the mute length |
| Effective daemon configuration | Read-only: every value and where it came from |
| About and help | What the sources are, the order note, where the logs and the diagnostics commands are |

The effective configuration names the origin of every value:
`environment`, `dash.toml`, `database`, or `default`. Those values need a
daemon restart, so they are edited in the configuration file or the
environment rather than on the page.

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

A small **?** beside each tab, each section and each notable setting opens
the documentation site in a new tab, at the page and the heading that
explain that part of the page.

The links are plain anchors, so they work with the keyboard and without
JavaScript. They carry nothing identifying: the URL has no query string, no
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
