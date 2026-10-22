# Remote collectors

A collector is a small script that runs on a computer *other* than the
daemon and reports to it. Today it reports Claude Code's quota.

## Do you need one?

| Where Claude Code runs | Use |
| --- | --- |
| On the same computer as the daemon | **Not a collector.** Press **Set up Claude export** on the account (Services tab). It is local: no network, no token, and **Accept remote reports** stays off. |
| On a different computer: the daemon is on a server, in a container, or in another room | **A collector**, on the computer where Claude Code runs. |

The two are alternatives. Do not set up both for the same account: the admin
page warns you if you do.

## What a collector is

`presto_collector.py` is a single-file Python 3.8+ script with no
dependencies. One file serves every service that can report remotely, and each
service is a *mode* named on the command line. Claude Code's status line is the
first, `claude-statusline`. The daemon's own Claude export uses the same
parsing, so the two behave identically.

In `claude-statusline` mode it reads the status-line JSON from standard input,
normalises it into the dashboard's report format, and posts it to the daemon.
Then it chains your previous status-line command, passing it the same input and
printing its output, so nothing is lost.

It never raises, and it always exits 0. If the daemon is unreachable, it gives
up quietly and your status line still works.

**It is not a service.** Nothing runs between Claude's responses. Claude Code
starts the script itself, after each response, because the `statusLine`
setting names it. You do not install it, start it, stop it or give it
administrator rights. You need Python 3.8 or newer on that computer, the file,
a token file and one line in Claude's settings.

## What reports remotely, and what does not

| Reports | Does not |
| --- | --- |
| The 5-hour and weekly quota, after a terminal response | Last use and the token counts: they come from Claude's local session logs, which only the daemon's own computer can read |
| | The usage cache, for the same reason |
| | Claude in the VS Code panel, which does not run the status line |

Codex needs Codex on the daemon's own computer, so a daemon in a container
cannot show it. See the [Docker guide](../getting-started/install-with-docker.md).

## Setting it up

Everything remote is on one page: **Settings -> Remote reports** in the admin
page. The Claude account's card says whether remote reports are on, and links
there.

1. Tick **Accept remote reports**.
2. On the same panel, **Download collector**. It shows the file's SHA-256: after
   you copy the file to the other computer, compare it. `Get-FileHash
   .\presto_collector.py` on Windows or `sha256sum presto_collector.py`
   elsewhere prints it.
3. Under **Accounts that report remotely**, press **Create remote token** on
   the account. The token is shown once. Put it in a file on the remote
   computer, for example `~/.config/presto-ai-dash/claude.token`. The daemon
   stores only its hash.
4. On the remote computer, set the `statusLine` command in that profile's
   Claude settings file to the snippet the page shows. Edit its URL placeholder
   to the daemon's real address, with the path: `http://192.168.1.50:8787/api/v1/ingest`.
   Check the token file path and that `presto_collector.py` is where the
   snippet says.

From then on Claude Code runs the collector after normal responses, exactly as
it runs the local export.

## Running it by hand

```bash
python3 presto_collector.py claude-statusline \
    --url http://192.168.1.50:8787/api/v1/ingest \
    --token-file ~/.config/presto-ai-dash/claude.token \
    --host laptop -- <previous status line command, optional>
```

`--url` is the daemon's full ingest address, path included. The collector posts
to exactly what it is given.

The token comes from a file, never from the command line, so a settings file
can be shared or synced without leaking it.

## If no report arrives

A status line never shows its own errors, so a collector that cannot reach the
daemon looks like one that works. Check, in this order:

1. **Accept remote reports** is ticked. While it is off, the daemon answers as
   if the route did not exist, with or without a token.
2. The URL ends in `/api/v1/ingest` and uses the daemon's address as the
   remote computer sees it, not `localhost`.
3. The token file holds the token and nothing else, and it is for *this*
   account. A new token replaces the old one.
4. The remote computer can reach the status port, 8787. A firewall on the
   daemon's computer is the usual cause; see
   [Securing a deployment](securing-a-deployment.md).
5. `python3` (or `python` on Windows) starts Python 3.8 or newer there.

The Status tab's **Remote collectors** table shows the last report, the host
label and the collector's version for each account. A wrong or revoked token
is logged as a failed sign-in, with the address it came from and never the
token, and it shows in the Problems list.

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
status-line command does not change, and nothing needs a restart: Claude Code
starts the script afresh each time. The notice clears with the next report.

The daemon never updates the copy for you, and the collector never downloads
anything. Either would mean running code fetched over plain HTTP.

A copy whose version is *newer* than the daemon's is reported as such, which
usually means the daemon was downgraded.

## How it behaves when the network is bad

A status line must never stall your terminal, so the collector is careful:

- It skips a heartbeat if one was sent less than 60 seconds ago.
- It skips an identical report sent less than 60 seconds ago.
- After a network failure it stops posting for five minutes.
- The whole request is bounded by 1.5 seconds.
- Redirects are refused, so the token is never forwarded to another host.

## What the daemon does with a report

- It validates the body strictly. An unknown key, a bad percentage or a bad
  timestamp is refused, and nothing is stored.
- It stamps the reading with its own receive time. The remote machine's
  clock is informational only.
- It keeps the previous timestamp when the evidence hash is unchanged, so a
  redraw is not a new reading.
- It records when the collector was last seen and from which host label, and
  the card shows `Reports: <host> <age> ago`.

One account may report from several computers. The newest reading wins, and
the collector trail names the computer that reported last.

## Rotating and revoking

Under **Settings -> Remote reports**, the account has **Rotate** and
**Revoke**.

- Rotating issues a new token and stops the old one working.
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

## See also

- [Remote reports](../concepts/remote-reports.md)
- [Claude](claude.md)
- [Running the daemon in the background](run-in-the-background.md)
- [Securing a deployment](securing-a-deployment.md)
- [Ports and endpoints](../reference/ports-and-endpoints.md)
