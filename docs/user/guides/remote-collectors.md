# Remote collectors

A collector is a small script that runs on a computer *other* than the
daemon and reports Claude Code's quota to it.

You need one when the daemon runs somewhere else - a server, a container, a
different room - and the Claude Code you actually use is on your laptop.

## What a collector is

`claude_statusline.py` is a single-file Python 3.8+ script. It is the same
code that drives the local Claude export, so the two behave identically.

It reads the status-line JSON from standard input, normalises it into the
dashboard's report format, and posts it to the daemon. Then it chains your
previous status-line command, passing it the same input and printing its
output, so nothing is lost.

It never raises. It always exits 0. If the daemon is unreachable, the
collector gives up quietly and your status line still works.

## Setting it up

1. On the daemon's admin page, turn on **Settings -> Remote reports ->
   Accept remote reports**.
2. Download `claude_statusline.py` from the same page, so the remote machine
   does not need the repository.
3. In the remote account's row, press **Create remote token**. The token is
   shown once. Put it in a file on the remote machine, for example
   `~/.config/presto-ai-dash/claude.token`. The daemon stores only its
   hash.
4. On the remote machine, set the `statusLine` command in that profile's
   Claude settings file to the snippet the page shows you, then edit the URL
   placeholder to the daemon's address and check the token file path.

From then on Claude Code runs the collector after normal responses, exactly
as it runs the local export.

## Running it by hand

```bash
python3 claude_statusline.py --url http://192.168.1.50:8787 \
    --token-file ~/.config/presto-ai-dash/claude.token \
    --host laptop -- <previous status line command, optional>
```

The token comes from a file, never from the command line, so a settings file
can be shared or synced without leaking it.

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

The account's row has **Rotate** and **Revoke**.

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
- [Ports and endpoints](../reference/ports-and-endpoints.md)
