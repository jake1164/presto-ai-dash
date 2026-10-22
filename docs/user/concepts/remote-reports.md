# Remote reports

By default the daemon reads everything itself, from the machine it runs on.
**Remote reports** let a Claude Code or Codex setup on *another* computer
report to this daemon instead.

This is for the case where the daemon runs somewhere else - a server, a
container, another room - and the Claude Code or Codex you use is on your
laptop.

## The rules

- The feature is **off by default**. Nothing new listens until you turn it
  on.
- It is **one account at a time**. Each remote token is write-only for one
  account and cannot read anything.
- The daemon **owns the time**. A pushed reading is stamped with the time the
  daemon received it, so a remote machine with a wrong clock cannot make a
  stale number look fresh.
- The body carries **no credentials**. Only quota windows, evidence hashes,
  a short host label and, from `watch`, the same short list of fields the
  daemon's own readers keep: activity counts, usage-cache numbers and Codex
  limits.

## Turning it on

In the admin page, **Settings -> Remote reports**, tick **Accept remote
reports**. Or set it in the configuration file:

```toml
[daemon]
ingest_enabled = true
```

Or in the environment, which is the usual choice for a container:

```bash
DASH_INGEST_ENABLED=1
```

The environment variable wins over the stored switch and over the
configuration file, and it locks the page's switch read-only. It accepts
`1/true/yes/on` and `0/false/no/off`, case-insensitively; an invalid value
fails closed, so the route stays off and the daemon logs the error.

While the feature is off, the ingest path answers `404` like any unknown
path. The route does not advertise itself.

## The flow

1. Turn the feature on.
2. Download the collector `presto_collector.py` from the same panel. It is a
   single-file Python 3.8+ script, and it is the same code that drives the
   local export. The panel shows its SHA-256, to compare after you copy it.
3. Under **Accounts that report remotely**, on the same panel, press **Create
   remote token** for the account. The token is shown once. Copy it into a file
   on the remote computer, for example `~/.config/presto-ai-dash/claude.token`.
   The daemon stores only its SHA-256 hash.
4. On the remote computer, for Claude Code in a terminal, set the `statusLine`
   command in that profile's Claude settings file to the snippet the page gives
   you, then edit the URL placeholder to the daemon's real address, with the
   path `/api/v1/ingest`, and check the token file path. For the VS Code panel
   and for Codex, which have no status line, save the token and the
   `collector.json` the page gives you in `~/.presto-ai-dash/` and run
   `python3 presto_collector.py watch`.

All of it is on **Settings -> Remote reports**. The account's card on the
Services tab only says whether remote reports are on, and links there.

Claude Code then runs the collector after normal responses, exactly as it
runs the local export; `watch` reads on a timer.

If you set a collector up while remote reports are still off, nothing is
stored and nothing breaks. `watch` tells you the daemon answered 404 and what
to switch on, and starts working at its next retry once you do.

The daemon can also rotate a token and revoke one. A revoked token stops that
computer's reports immediately.

## What a report contains

```json
{
  "schema": "presto-ai-dash.observation/1",
  "collector": {"name": "claude-statusline", "version": "1", "host": "laptop"},
  "client_time": "2026-10-18T14:02:11Z",
  "windows": [
    {"id": "five_hour", "label": "5h", "period_minutes": 300,
     "used_percent": 12, "resets_at": "2026-10-18T18:00:00Z", "state": "available"}
  ],
  "evidence": {"five_hour": "<sha256 hex>"},
  "progress": "<sha256 hex or null>"
}
```

- `host` is a label you choose, at most 32 ASCII characters. It is used to
  show which computer reported last.
- `evidence` is a hash of the values. A report whose hash matches the stored
  one keeps its previous timestamp, so a redraw is not a new reading.
- `progress` is a hash that changes only when the provider did real work. It
  is the "last reply seen" signal.
- `client_time` is informational. The daemon's own clock is what counts.

`watch` adds optional keys for what it reads, each checked field by field:
Claude's `activity` and `usage_cache`, and Codex's `reset_credits` and
`credits`. A source takes only its own, and anything else in an object is
refused. A usage-cache reading carries the time Claude Code fetched it, which
is the one time the daemon accepts from a collector: capped at the receive
time and refused when over seven days old.

## Network safety

The ingest route speaks **plain HTTP** and sits beside the status endpoint.
The dashboard does not terminate TLS, so:

- never expose the daemon's port to an untrusted network,
- put it behind a reverse proxy with TLS, or
- keep it on a private network such as Tailscale.

A remote token can only write readings for one account. It cannot read the
status payload, list your sources, or touch any other account. But the port
also serves the status endpoint, so treat its exposure like any other
unencrypted service on your LAN.

## See also

- [Remote collectors](../guides/remote-collectors.md)
- [Ports and endpoints](../reference/ports-and-endpoints.md)
- [Privacy](../privacy/index.md)
