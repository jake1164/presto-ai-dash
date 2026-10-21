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
  follow a symlink out of the profile tree.

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
| The encryption master key | Outside SQLite: a DPAPI-protected file on Windows, an owner-only file elsewhere, or `DASH_ENCRYPTION_KEY` |
| Your readings and a short history | SQLite, in plain text |
| Your Claude status-line command | Claude Code's own settings file, the provider's file, with the previous value kept encrypted in SQLite |
| The usage cache's account ID | In memory only, as a hash, and never stored |
| The remote collectors' last report | SQLite, with the host label you chose |

Only the credentials are encrypted. Ordinary settings and readings are
readable in the database file, which is your own file on your own machine.

## What leaves your machine

**Nothing is sent to the project, its author or any third party.** There is
no telemetry, no analytics, no crash reporting, no counter and no license
check. The daemon never contacts a server that is not one of the services you
configured.

The only traffic is the traffic you set up: requests from the daemon to the
providers you added, which carry the key you gave for that provider (OpenCode
Go, Ollama Cloud and LiteLLM are the ones that use a key), and requests from
the Presto, the admin page and any remote collector to your own daemon. The
documentation site you are reading loads nothing from a third party: no web
fonts, no scripts, no analytics. It is static files on GitHub Pages.

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

A remote token is write-only for one account: it cannot read the status
payload, list your sources or touch another account. Revoke it and that
computer stops reporting immediately.

The route is off by default and speaks plain HTTP, so keep it on a private
network.

## A note on the shared token

The shared token is a bearer token with no per-user identity and no expiry.
It is meant for a trusted LAN and nothing else. Do not expose the daemon's
port to the internet.

## See also

- [Remote reports](../concepts/remote-reports.md)
- [Ports and endpoints](../reference/ports-and-endpoints.md)
- [Claude](../guides/claude.md)
