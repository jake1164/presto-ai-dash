# Remote collectors

A collector is a small script that runs on a computer *other* than the
daemon and reports to it. It can report Claude Code's quota from the terminal,
what Claude Code is doing in the VS Code panel, and Codex's limits.

## Do you need one?

| Where Claude Code and Codex run | Use |
| --- | --- |
| On the same computer as the daemon | **Not a collector.** The daemon reads them itself: press **Set up Claude export** on the Claude account, tick the local readers, and Codex works from its own login. No network, no token, and **Accept remote reports** stays off. |
| On a different computer: the daemon is on a server, in a container, or in another room | **A collector**, on the computer where Claude Code and Codex run. |

The two are alternatives. Do not set up both for the same account: the admin
page warns you about the Claude export and a collector together. If you do
both anyway, nothing breaks: the newest reading per window wins.

## What a collector is

`presto_collector.py` is a single-file Python 3.8+ script with no
dependencies. One file serves every service that can report remotely, and each
way of reporting is a *mode* named on the command line:

| Mode | For | Runs |
| --- | --- | --- |
| `claude-statusline` | Claude Code in a terminal | Claude Code starts it after every response |
| `watch` | The Claude VS Code panel, and Codex | You start it once and it keeps running |

The daemon's own readers and the collector share their code: the collector
file holds a word-for-word copy of the daemon's Claude and Codex readers, so a
reading means the same thing whichever way it arrives.

### `claude-statusline`

It reads the status-line JSON from standard input, normalises it into the
dashboard's report format, and posts it to the daemon. Then it chains your
previous status-line command, passing it the same input and printing its
output, so nothing is lost.

It never raises, and it always exits 0. If the daemon is unreachable, it gives
up quietly and your status line still works. **It is not a service:** Claude
Code starts it itself, because the `statusLine` setting names it.

### `watch`

The VS Code panel never runs the status line, and Codex has no hook at all, so
neither can call a collector when something happens. `watch` looks every
minute instead. For each account in its configuration it reads what the daemon
would read on its own computer, and pushes it with that account's own token:

- **Claude:** the activity summary from the session logs (last request, model,
  surface, request and token counts) and the usage cache (the two quota windows,
  the included credit, and when Claude Code fetched them).
- **Codex:** the rate-limit windows, the banked reset credits and the credit
  balance, from the same read-only call the daemon makes.

It is a long-running command, so it must be started: at login, as a service,
or in a container. Everything it needs is in one folder.

## The collector folder

`watch` reads its settings from `~/.presto-ai-dash/collector.json` (on Windows,
`C:\Users\you\.presto-ai-dash\collector.json`), or from the file named by
`--config`. Token files sit in the same folder, so one folder holds all of it:

```text
~/.presto-ai-dash/
    collector.json
    claude-<account>.token
    codex-<account>.token
```

```json
{
  "url": "http://192.168.1.50:8787/api/v1/ingest",
  "host": "laptop",
  "interval_seconds": 60,
  "accounts": [
    {"service": "claude", "name": "Claude", "token_file": "claude-abc123.token"},
    {"service": "codex", "name": "Codex", "token_file": "codex-def456.token"}
  ]
}
```

The admin page writes this for you: when you create a remote token it shows the
file for that account, with the right token file name. Only the URL needs
editing.

| Setting | Meaning |
| --- | --- |
| `url` | The daemon's full ingest address, path included. Required. |
| `host` | The label shown next to a report. Default: this computer's name. At most 32 ASCII characters. |
| `interval_seconds` | How often to read and push, 30 to 3600. Default 60. |
| `accounts` | One to eight accounts. Each uses its **own** token. |

A Claude account takes:

| Setting | Meaning |
| --- | --- |
| `token_file` | The file holding this account's remote token. A relative name is taken from the collector folder. Required. |
| `activity` | Read and push the session-log activity. Default `true`. |
| `usage_cache` | Read and push the usage cache. Default `true`. |
| `settings_path` | The Claude Code settings file of the profile. Default `~/.claude/settings.json`. |
| `usage_cache_path` | The usage cache file, when it is not where Claude Code puts it. |

A Codex account takes `token_file`, and optionally `executable` (a program
named `codex`), `profile_home` (an absolute path) and `timeout`.

An unknown setting is an error, not ignored: a misspelt `usage_cache` must not
silently turn a reader on or off. Use forward slashes in paths, also on
Windows, so the JSON stays valid.

### Trying it

```bash
python3 presto_collector.py watch --once
```

makes one round, says what it read and what happened to it for each account,
and exits `0` when every account was sent. Then start it for real:

```bash
python3 presto_collector.py watch
```

Run it the way you run any command that should stay up: a login task, a
systemd user service, a launch agent, or a container beside Claude. On Windows,
run Python directly from the task (a wrapper script can leave the process
running when the task is stopped). Nothing it needs is administrator-only.

## What reports remotely, and what does not

| Reports | By |
| --- | --- |
| The 5-hour and weekly quota, after a terminal response | `claude-statusline` |
| Last use, surface, request and token counts, from Claude's session logs | `watch` |
| The usage cache: the two quota windows and the included credit, with Claude Code's own fetch time | `watch` |
| Codex limits, reset credits and the credit balance | `watch` |

It never sends prompts, tool inputs, file paths, account ids, e-mail,
organisation, project names or credentials: the fields are the same short
whitelist the daemon's own readers keep, and the daemon refuses an envelope
with any other field.

**What has been tested.** The collector, run alone in an empty folder against a
throwaway daemon, with Linux and Windows test runs. A real second computer, a
real remote Claude session and a real Codex login on another host have not been
exercised. If you run it on one, tell the project what you find.

## Setting it up

Everything remote is on one page: **Settings -> Remote reports** in the admin
page. The account's card says whether remote reports are on, and links there.

1. Tick **Accept remote reports**.
2. On the same panel, **Download collector**. It shows the file's SHA-256: after
   you copy the file to the other computer, compare it. `Get-FileHash
   .\presto_collector.py` on Windows or `sha256sum presto_collector.py`
   elsewhere prints it.
3. Under **Accounts that report remotely**, press **Create remote token** on
   the account. The token is shown once; the daemon stores only its hash. The
   page also shows the file to save as `collector.json`, and, for Claude, the
   status-line snippets.
4. On the remote computer:
    - **For the status line (Claude in a terminal):** set the `statusLine`
      command in that profile's Claude settings file to the snippet, with its
      URL edited to the daemon's real address.
    - **For `watch` (the VS Code panel, Codex):** create `~/.presto-ai-dash/`,
      save the token in it under the file name the page gives, save the
      `collector.json` there with its URL edited, then run `watch --once` to
      check and `watch` to keep it going.

## Running it by hand

```bash
python3 presto_collector.py claude-statusline \
    --url http://192.168.1.50:8787/api/v1/ingest \
    --token-file ~/.config/presto-ai-dash/claude.token \
    --host laptop -- <previous status line command, optional>
```

`--url` is the daemon's full ingest address, path included. The collector posts
to exactly what it is given. The token comes from a file, never from the
command line, so a settings file can be shared or synced without leaking it.
On Linux and macOS, `chmod 600` the token file: the collector says so once if
others can read it.

## If the daemon does not accept the reports

Setting a collector up before the daemon is ready is safe. Nothing is stored,
nothing breaks, and `watch` says what is wrong, once, in plain words, retries
with a wait that grows to five minutes, and says **reporting again** when it
works. A status line stays silent instead, because it must never get in the way
of Claude Code, so use `watch --once` to see the reason.

| What `watch` says | What is wrong | What to do |
| --- | --- | --- |
| the daemon answered 404: remote reports are switched off | **Accept remote reports** is off on the daemon (or the URL is not its ingest address). While it is off the route answers like any unknown path, with or without a token | Tick it under **Settings -> Remote reports**, or set `DASH_INGEST_ENABLED=1`. The collector starts working at its next retry, within five minutes |
| the daemon refused the token (401) | The token was revoked, rotated, or belongs to another account | Create a new remote token for this account and save it in the token file |
| the daemon cannot be reached | Wrong address, a firewall, or the daemon is not running | Check the URL is the daemon's address *as this computer sees it* (not `localhost`) and that port 8787 is open; see [Securing a deployment](securing-a-deployment.md) |
| the daemon redirected the request | A proxy or a wrong URL. Redirects are never followed, because they would send the token elsewhere | Use the final address |
| the daemon rejected the report (400) | The collector and daemon disagree about the format | Update the older of the two |
| the token file is missing or empty | The file named in `collector.json` is not there | Save the token under that name in the collector folder |
| Codex could not be read | Codex is not installed or signed in on this computer | Sign in with `codex login` there; the collector never signs in for you |

One account failing never stops the others.

## If no report arrives

1. **Accept remote reports** is ticked. While it is off, the daemon answers as
   if the route did not exist, with or without a token.
2. The URL ends in `/api/v1/ingest` and uses the daemon's address as the
   remote computer sees it, not `localhost`.
3. The token file holds the token and nothing else, and it is for *this*
   account. A new token replaces the old one.
4. The remote computer can reach the status port, 8787.
5. `python3` (or `python` on Windows) starts Python 3.8 or newer there.
6. For `watch`, run `watch --once` and read what it says.

The Status tab's **Remote collectors** table shows the last report, the host
label and the collector's version for each account. A wrong or revoked token
is logged as a failed sign-in, with the address it came from and never the
token, and it shows in the Problems list. The **Readers** table shows a
reading that came from a collector, with the host that sent it.

If the collector stops, the daemon notices: a pushed activity summary is
treated as unavailable ten minutes after the last one, a pushed Codex reading
ages into a stale one, and a usage-cache reading keeps the age Claude Code
gave it.

## Keeping it up to date

The collector has a whole-number version, sent with every report. The daemon
compares it with the version of the file it serves. When a copy is older, the
daemon says so, as a chore and never as a fault:

- the account's row under **Accounts that report remotely** names the computer
  and the two versions;
- the Status tab's **Remote collectors** table shows the version, and the state
  becomes **update available**;
- the Problems list gets one entry per account and old version, and the daemon's
  log gets the same line once each time it starts;
- the Claude card's detail view on the Presto gets a line, `Collector v1 old, v2
  ready`.

It changes no status and no colour, and readings keep arriving meanwhile.

To update, download the file again and **replace it at the same path**. The
status-line command does not change, and Claude Code starts the script afresh
each time. **A running `watch` keeps the old code until you restart it.** The
notice clears with the next report.

The daemon never updates the copy for you, and the collector never downloads
anything. Either would mean running code fetched over plain HTTP.

A copy whose version is *newer* than the daemon's is reported as such, which
usually means the daemon was downgraded.

## How it behaves when the network is bad

A status line must never stall your terminal, so `claude-statusline` is careful:

- It skips a heartbeat if one was sent less than 60 seconds ago.
- It skips an identical report sent less than 60 seconds ago.
- After a network failure it stops posting for five minutes.
- The whole request is bounded by 1.5 seconds.
- Redirects are refused, so the token is never forwarded to another host.

`watch` has no terminal to protect, so each request may take up to 10 seconds.
After a failure an account waits one interval, then two, four, eight and
sixteen, never more than five minutes; the others carry on. Redirects are
refused here too. A reader that breaks is reported by name and never ends the
watch.

## What the daemon does with a report

- It validates the body strictly. An unknown key, a bad percentage or a bad
  timestamp is refused, and nothing is stored. A source takes only the extra
  readings that belong to it: Claude takes `activity` and `usage_cache`, Codex
  takes `reset_credits` and `credits`.
- It stamps the reading with its own receive time. The remote machine's
  clock is informational only. **The one exception** is the usage cache, which
  keeps the time Claude Code fetched it: that reading may be hours old, and
  stamping it "now" would show old numbers as new. That time is capped at the
  receive time, and a reading older than seven days is refused.
- It keeps the previous timestamp when the evidence hash is unchanged, so a
  redraw is not a new reading. A Codex reading is a fresh look at the provider
  every time, so each one restarts the clock.
- Claude: a daemon that can read the activity logs or the usage cache on its
  own machine uses its own reading, and the pushed one fills in when it has
  none. Codex: a pushed reading under five minutes old is used instead of
  starting a local Codex; when the collector goes quiet and this computer has no
  Codex, the last pushed reading is shown, aging into a stale one.
- It records when the collector was last seen and from which host label, and
  the card shows `Reports: <host> <age> ago`.

One account may report from several computers. The newest reading wins, and
the collector trail names the computer that reported last.

## Rotating and revoking

Under **Settings -> Remote reports**, the account has **Rotate** and
**Revoke**.

- Rotating issues a new token and stops the old one working. Save the new one
  in the token file; a running `watch` reads the file again every round.
- Revoking stops that computer's reports immediately. The account keeps its
  last reading, which then ages into a stale reading rather than a fresh
  zero.

## The security trade-off

The route speaks plain HTTP and sits beside the status endpoint. Put the
daemon on a private network such as Tailscale, or behind a reverse proxy
with TLS, before you turn this on.

A remote token is write-only for one account: it cannot read the status
payload, list your sources or touch another account. But the port also
serves the status endpoint, so treat its exposure like any other
unencrypted service on your LAN.

The collector only reads. It never signs in, signs out, refreshes a login,
sends a prompt or spends anything: the Codex call it makes is the same
read-only rate-limit call the daemon makes, and Claude's files are opened
read-only.

## See also

- [Remote reports](../concepts/remote-reports.md)
- [Claude](claude.md)
- [Codex](codex.md)
- [Running the daemon in the background](run-in-the-background.md)
- [Securing a deployment](securing-a-deployment.md)
- [Ports and endpoints](../reference/ports-and-endpoints.md)
