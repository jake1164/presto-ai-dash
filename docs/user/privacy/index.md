# Privacy

This page says what Presto AI Dash reads, what it never does, and what
leaves your machine. It is the short version; the
[developer documentation](../developers/how-a-source-works.md) has the rules
the code follows.

## The rule in one sentence

**The dashboard observes your accounts and never acts on them.**

## What it reads

Everything the dashboard reads is a documented usage or limit reading, or a
file that a tool on your own machine wrote.

| What | Where it comes from | What is taken |
| --- | --- | --- |
| Claude quota | The Claude Code status line | The 5-hour and weekly percentages, their reset times, a version |
| Claude usage cache | `~/.claude.json`, which Claude Code writes for its own `/usage` dialog | The fetch time, the two windows, the included credit |
| Claude activity | The local session logs | Timestamps, the surface, the model name, token counts |
| Codex allowance | A local Codex app-server | Allowance buckets, percentages, resets, banked reset credits |
| OpenCode Go | The provider's usage endpoint | Three windows with their percentages and resets |
| Ollama Cloud | The provider's usage endpoint | Requests, tokens, spend, the date range |
| Ollama | The local server's API | Version, loaded model, available models |
| LiteLLM | The gateway's daily activity endpoint | Requests, tokens, spend, errors, top model |
| Ollama speed | A samples file a collector wrote | The last measured request |

## What it never reads

- **Prompt text, responses and transcripts.** The Claude session logs
  contain them. The reader opens the files, extracts the four fields listed
  above, and never stores, logs, returns or sends anything else.
- **Your account's identity.** No email, no account ID, no organisation, no
  project history, no server list. The usage cache reader compares a hash of
  the account ID **in memory only**, to notice that the account behind the
  cache changed, and stores nothing.
- **Credentials.** No provider token, OAuth credential, cookie or
  keychain item is read, copied, mounted or forwarded. Codex owns its own
  credential cache and the dashboard never reads it.
- **Your prompts to the provider.** The dashboard never sends a prompt or a
  model request to produce a number. LiteLLM's `GET /health` endpoint *does*
  make test calls and costs tokens, so it is not used.
- **Anything outside the profile directory.** The Claude readers never
  follow a symlink out of the profile tree, and the usage-cache reader does not
  follow a link in place of its own file: it reports it and reads nothing.

## What it never does

- It does not spend, redeem, reset, top up or cancel anything.
- It does not change a plan, a limit or a setting on a provider account.
- It does not log in, sign out or refresh a token on its own.
- It does not buy anything.

There is exactly one exception, and it is one **you** start by hand in the
admin page: connecting or disconnecting an account (the Codex sign-in and
sign-out) and installing or removing the Claude status-line export. Those
never run from a poll and never run from a remote request.

If a provider's API offers an action - Codex's method that redeems a reset
credit, for example - the dashboard does not wire it up. There is no code
path that could spend one.

## What is stored, and where

| What | Where |
| --- | --- |
| Your accounts and their settings | SQLite, in plain text |
| Your provider keys | SQLite, encrypted with Fernet, bound to their account |
| The encryption master key | Outside SQLite: a DPAPI-protected file on Windows, an owner-only file elsewhere, or `DASH_ENCRYPTION_KEY`. The database and the key file are created readable and writable by your account only |
| Your readings and a short history | SQLite, in plain text |
| Your Claude status-line command | Claude Code's own settings file, the provider's file, with the previous value kept encrypted in SQLite |
| The usage cache's account ID | In memory only, as a hash, and never stored |
| The clients' names, revisions and diagnostics | In memory only, and never stored |
| The remote collectors' last report | SQLite, with the host label you chose |

Only the credentials are encrypted. Ordinary settings and readings are
readable in the database file, which is your own file on your own machine.

## What leaves your machine

**The app sends nothing to the project, its author or any third party.** There
is no telemetry, no analytics, no crash reporting, no counter and no license
check. (The documentation website is a separate matter; see
[The documentation site](#the-documentation-site).) The daemon never contacts a server that is not one of the services you
configured. It does not follow a redirect to another host, and a saved key is
never sent to a new address: changing a source's URL asks for the key again.

The only traffic is the traffic you set up: requests from the daemon to the
providers you added, which carry the key you gave for that provider (OpenCode
Go, Ollama Cloud and LiteLLM are the ones that use a key), and requests from
the Presto, the admin page and any remote collector to your own daemon. The
documentation site you are reading is covered below.

## The documentation site

The site is static files on GitHub Pages, so GitHub, which hosts them, sees the
visits as any web host does. On top of that, **the site uses Google Analytics 4**
(Google's `googletagmanager.com` script) to count visits, **but only if you
accept it**.

- **Your choice comes first:** the first time you visit, a banner asks whether
  Google Analytics may run. Until you accept, the script is not loaded, no
  request goes to Google and no analytics cookie is set. If you reject, none of
  that ever happens. Your choice is remembered in your browser, and **Change
  cookie settings** in the footer of every page lets you change it.

- **What it collects:** the standard page-view and visit data Google Analytics
  gathers: which page was read, roughly when, the browser and device type, an
  approximate location, and where the visitor came from. It sets cookies in
  your browser to tell visits apart.
- **Why:** only to see which help pages are read and which are ignored, so
  low-value pages can be trimmed or rewritten. There is no advertising and the
  data is not sold.
- **What it does not do:** Google Signals is turned off, so the data is not
  joined to the Google accounts of signed-in visitors.
- **Opting out:** reject the banner, or use **Change cookie settings** later.
  A content blocker also works. The application itself never loads this script,
  and nothing on this site is needed to run the daemon or the Presto.
- **Google's policy:** [policies.google.com/privacy](https://policies.google.com/privacy).

Nothing else is requested from another host: no web fonts, no other scripts,
and the repository link in the header is a plain link that fetches nothing.
The one exception is a browser too old to have `ResizeObserver` (before about
2020): the theme then fetches a small polyfill from `unpkg.com`. The build
checks all of this on the finished site each time it runs, and fails if any
other host appears or if a page loads Google Analytics without the cookie banner
and the footer notice that links this page.

## The one write the Presto can make

The Presto can switch one service's alerts on or off. That write goes to
**your daemon**, not to a provider: `POST /api/v1/alerts/<id>` with
`{"enabled": false}`.

It sets a state and never toggles, it changes only that one value, it is
rate-limited, and it is logged. No provider is contacted, no quota changes,
and nothing is read or written that the device could not read anyway.

The shared token it authenticates with travels over plain HTTP on your LAN,
so the worst an eavesdropper could do with it is flip one service's alerts.
On an untrusted network, put the daemon behind a reverse proxy with TLS or
use a private network such as Tailscale.

## Remote reports

When you turn on remote reports, a collector on another computer sends
quotas to your daemon. The body carries window percentages, reset times,
evidence hashes and a host label you chose. It carries no credentials, no
session identities and no prompt text.

The collector's `watch` mode adds what the daemon's own readers would read:
for Claude, the time of the last request, its model and surface, and request
and token counts, and the usage cache's quota windows, included credit and
fetch time; for Codex, the limits, the count and expiry of reset credits and
the credit balance. It never sends a path, a project name, an account id, an
e-mail address, an organisation, a prompt or a credential, and the daemon
refuses a report that carries any field outside that list. The collector only
reads: it does not sign in, refresh a login or send a prompt.

A remote token is write-only for one account: it cannot read the status
payload, list your sources or touch another account. Revoke it and that
computer stops reporting immediately.

The route is off by default and speaks plain HTTP, so keep it on a private
network.

## The device's own diagnostics

Each request from the Presto carries three extra headers: its name, the app
revision it runs, and `X-Presto-Diag`, a short summary of the device's own
state.

`X-Presto-Diag` says only what the device already knows about itself: the
type and errno of the last failed fetch and where it stopped, how many times
the fetch has failed since the device was switched on, the free heap in KiB,
the uptime in seconds, the Wi-Fi signal in dBm and the number of Wi-Fi drops.
It never carries the text of an exception, so no address, token or response
body can travel in it, and it is at most 120 characters long.

The daemon checks every pair against a fixed list and keeps only what it
knows. **It is kept in memory only**: it is never written to the database,
never sent anywhere, and is forgotten when the daemon restarts. The Status tab
shows it, so you can see why a device is unhappy without a serial cable.

## A note on the shared token

The shared token is a bearer token with no per-user identity and no expiry.
It is meant for a trusted LAN and nothing else. Do not expose the daemon's
port to the internet. [Securing a deployment](../guides/securing-a-deployment.md)
says how to narrow who can reach it, where to keep the database, and what to
do if a token leaks.

## See also

- [Remote reports](../concepts/remote-reports.md)
- [Ports and endpoints](../reference/ports-and-endpoints.md)
- [Claude](../guides/claude.md)
