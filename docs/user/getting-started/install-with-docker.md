# Running the daemon in a container

This page is about running the daemon and its admin page in Docker, on a
machine that is not your desk (a NAS, a server, a small box under the TV).
The daemon reads the providers' usage data and serves it to the Presto
display and to the admin page.

Nothing here changes the daemon's rules: the app only observes provider
accounts, it never spends or resets anything, and it never reads a
credential it was not given.

## What is in the box

| File | What it is |
| --- | --- |
| `Dockerfile` | The image: the daemon, its admin page and one dependency. |
| `compose.yaml` | The daemon, its ports, its data volume and its settings. |
| `.dockerignore` | The list of files that may not go into the image. |
| `.env.example` | A template for the tokens. Copy it to `.env`. |

The image is built on `python:3.13-alpine3.24`, pinned to one digest, and it
runs as an unprivileged user (`dash`, uid 10001). It is about 95 MB.

## Quick start

```bash
# 1. Two tokens: one for the admin page, one for the Presto device. Generate
#    each, keep them, and keep them different.
python -c "import secrets; print(secrets.token_urlsafe(24))"
#    or: openssl rand -base64 24

# 2. Put them in .env next to compose.yaml.
cp .env.example .env
#    then edit .env and paste the tokens after DASH_ADMIN_TOKEN= and
#    DASH_SHARED_TOKEN=

# 3. Build and start.
docker compose up -d

# 4. Open the admin page on this machine and unlock it.
#    http://127.0.0.1:8788
```

The admin token you generated in step 1 is the token for the first login.
It is the permanent one: it does not change when the container restarts.
The daemon never prints it.

If you leave `DASH_ADMIN_TOKEN` empty, the daemon generates a temporary
token at every start and prints it in its log:

```text
temporary admin token (new at every start; set admin.token in dash.toml to keep one): ...
```

That is the path of a local install, where the admin page is only reachable
from the machine itself. **In a container the admin page has to listen on
all interfaces**, because that is how the published port reaches it, and a
non-loopback address needs a token you chose. So the container refuses to
start the admin page without one, and says why:

```text
daemon startup failed: the admin page would listen on 0.0.0.0, which is not a
loopback address; set an explicit admin token (admin.token in dash.toml or
DASH_ADMIN_TOKEN) before listening on a non-loopback address
```

A second message, of the same kind, appears when the list of names the page
answers to (`DASH_ADMIN_ALLOWED_HOSTS`) is empty.

The device token is separate. The Presto display holds it, and the compose
file refuses to start without `DASH_SHARED_TOKEN` in `.env`. That is on purpose:
without one the daemon would print a temporary token in its log, and Docker
keeps that log after the container is gone. Put the same value in the Presto's
settings (`presto/config.py`).

## The ports

| Port | Published as | Who uses it |
| --- | --- | --- |
| 8787 | `0.0.0.0:8787` (every address), or the one `DASH_PUBLISH_ADDRESS` names | The Presto display polls this. |
| 8788 | `127.0.0.1:8788` (this machine only) | The admin page. |

The admin page is published on the loopback address of the host on purpose.
To reach it from another machine, use an SSH port forward:

```bash
ssh -L 8788:127.0.0.1:8788 user@server
```

and open <http://127.0.0.1:8788> on your own machine. The browser then uses
`127.0.0.1:8788`, which the admin page always accepts.

To reach the admin page from another machine on the LAN, publish it as
`8788:8788` and add the name the browser will use to
`DASH_ADMIN_ALLOWED_HOSTS`, for example `192.168.1.50:8788`. Read the
security notes below before you do that.

## The health check

The image's health check asks one question:

```bash
curl -s http://127.0.0.1:8787/healthz
{"ok": true}
```

That is the whole answer. The route needs no token and returns nothing else:
no version, no source names, no counts, no timings. Every other route still
needs its token.

## Data and backups

Everything that has to survive an upgrade lives in one named volume,
`dash-data`, mounted at `/data`:

| File | What it is |
| --- | --- |
| `/data/dash.db` | The SQLite database: the accounts, the history, the encrypted provider keys, the browser sessions. |
| `/data/dash.db.key` | The encryption master key, in an owner-only file. |

Both files belong to the container's `dash` user and are readable by that user
only. To read or back them up from the host, use `docker cp` or a container
running as the same user; a bind mount owned by another host user will not
let the daemon create them.

**Losing the key file loses the stored provider keys.** The database still
opens and every other setting is still there, but the encrypted keys cannot
be read any more, and they are never restored by guessing: the daemon
refuses to start with a wrong or missing key instead of overwriting
anything. Back up the database *and* the key file, together:

```bash
docker run --rm -v presto-ai-dash_dash-data:/data -v "$PWD":/backup \
  presto-ai-dash:local tar czf /backup/dash-backup.tar.gz -C /data .
```

To move to another machine, copy both files and set `DASH_ENCRYPTION_KEY`
instead (see below).

The encryption master key can also live in the environment, which is the
portable choice: the key file of a Windows install is protected with DPAPI
and cannot be used in a Linux container.

```bash
python -m presto_ai_dash.app --generate-key   # prints a Fernet key
```

Put it in `.env` as `DASH_ENCRYPTION_KEY=...`. Keep it with the database
backup: the same rule applies, the key and the data belong together.

## Upgrades

```bash
docker compose pull        # only if you build from a registry
docker compose up -d --build
```

The data volume is not touched by an upgrade: the database and the key file
stay where they are. The daemon migrates its own schema when it starts.

Take a backup first if the version jumps a long way. The daemon refuses to
open a database that is newer than it is, and it never downgrades one.

## The time zone

A container starts in UTC. The quiet hours and the weekly reset estimate
use the process's local time, so set `TZ` in your `.env` to your own zone,
for example `America/New_York` or `Europe/London`. Left alone it is `UTC`,
and quiet hours such as 22:00 to 07:00 will then be in UTC:

```yaml
environment:
  TZ: "${TZ:-UTC}"
```

The admin page's **Settings** tab shows the zone in the effective
configuration, next to the other read-only values, so you can see what the
process is using.

## A reverse proxy in front

The daemon speaks plain HTTP and does not terminate TLS. A reverse proxy
does that:

- point the proxy at the container's port 8787 (the device port) or 8788
  (the admin page),
- terminate TLS at the proxy,
- and add the name the browser will use to the admin page's allowed hosts,
  because the admin page only answers to the names it knows:

```yaml
environment:
  DASH_ADMIN_HOST: "0.0.0.0"
  DASH_ADMIN_ALLOWED_HOSTS: "dash.example.com"
```

With that name in the list, the admin page answers to
`dash.example.com` over HTTP and over HTTPS, and to nothing else. A
request with another `Host` header is refused, exactly as before.

The device port needs no such list: the Presto sends the shared token and
the daemon does not look at the `Host` header there.

## Security notes

[Securing a deployment](../guides/securing-a-deployment.md) covers these in
full, including publishing the status port on one LAN address only.

- **The admin page is on the loopback address of the host by default.**
  `127.0.0.1:8788` is reachable from the machine and from nowhere else.
- **The shared token is plain HTTP on the LAN.** The Presto device and the
  device port send it unencrypted. Anyone who can watch the LAN can read
  it, and with it the status payload. Keep the LAN trusted, or put the
  device port behind a proxy with TLS, or use a private network such as
  Tailscale.
- **The admin token is separate from the device token** and the daemon
  refuses to use the same value for both.
- **Limits are per address.** The daemon limits how many connections, and how
  many alert writes, one client address may use. Where every client appears to
  come from one address (Docker Desktop on Windows or macOS, or a proxy in
  front), they share one allowance.
- **The container never sees your Claude credentials.** Do not mount
  `~/.claude` or `~/.claude.json` into it. Those files hold credentials, and
  the daemon has no reason to read them in a container. The image's build
  context excludes them, and the test suite fails if it ever stops doing so.
- **The container runs as an unprivileged user** and owns only `/data`. The
  compose file also drops every Linux capability, forbids gaining new
  privileges, and mounts the root filesystem read-only (with a scratch `/tmp`).
- **Tokens can come from files instead of the environment.** Anything in
  `environment:` shows in `docker inspect`. For the two tokens and the master
  key, set `DASH_ADMIN_TOKEN_FILE`, `DASH_SHARED_TOKEN_FILE` or
  `DASH_ENCRYPTION_KEY_FILE` to a path, and mount the file there (Compose
  `secrets:` appear under `/run/secrets/`). Setting both a variable and its
  `_FILE` form is an error, so in `compose.yaml` replace the
  `DASH_ADMIN_TOKEN` and `DASH_SHARED_TOKEN` lines rather than adding to them:

  ```yaml
  environment:
    DASH_ADMIN_TOKEN_FILE: /run/secrets/admin_token
    DASH_SHARED_TOKEN_FILE: /run/secrets/shared_token
  secrets:
    - admin_token
    - shared_token
  ```

  with a top-level `secrets:` block that points each name at a file outside the
  repository. The daemon reads the whole file (up to 4 KB) and drops the
  trailing newline.
- **The image carries no secrets.** The configuration, the tokens and the
  database all come from the environment and the volume.

## What works in a container, and what does not

| Source | In a container? | Why |
| --- | --- | --- |
| OpenCode Go | **Yes** | It is an HTTP API on the internet. |
| Ollama Cloud | **Yes** | It is an HTTP API on the internet. |
| LiteLLM | **Yes** | Point it at the gateway: `http://host.docker.internal:4000` or the gateway's LAN address. |
| Ollama (a server on your machine) | **Yes** | Same: `http://host.docker.internal:11434` or the LAN address. |
| Claude Code quota (the terminal export) | **No, not directly** | The export runs on the machine where Claude Code runs and reads files there. Use the remote collector below. |
| Claude Code activity (the local session logs) | **No** | The reader opens the session files on the daemon's own machine. A container has none. |
| Claude Code usage cache (`~/.claude.json`) | **No** | Same: the file is on the Claude machine, and it must not be mounted into the container. |
| Codex | **No** | The source starts a local `codex app-server` and uses the local login. A container has neither. |

A source whose files are missing does not break anything: it says plainly
that it is unavailable, and why, in its status. The container does not
crash over it.

`host.docker.internal` is the name Docker Desktop gives the machine that
runs Docker. On Linux, add it yourself in the compose file:

```yaml
extra_hosts:
  - "host.docker.internal:host-gateway"
```

The LAN address of the machine works everywhere and needs nothing.

### Feeding Claude data to a containerised daemon

The way in is the **remote collector**, the same one the local install
uses:

1. Turn the feature on: **Settings -> Remote reports -> Accept remote
   reports** in the admin page, or `DASH_INGEST_ENABLED=1` in the
   environment (the usual choice for a container).
2. On the same panel, download the collector `presto_collector.py`. It is a
   single file, Python 3.8 or newer, and it runs on the Claude machine. The
   panel shows its SHA-256 to compare after you copy it.
3. Under **Accounts that report remotely**, press **Create remote token** for
   the Claude account. The token is shown once. The daemon stores only its hash.
4. On the Claude machine, set that profile's `statusLine` command to the
   snippet the page gives you, and point the URL at the container's ingest
   address (`http://server:8787/api/v1/ingest`) and the token file at the token
   you copied.

Claude Code then runs the collector after each response, and the reading
arrives in the container. The body carries quota numbers, hashes and a short
host label. **No credentials, no prompts, no transcripts.**

Two warnings:

- **The ingest route is plain HTTP.** The token travels in the clear, like
  the device token. Never expose the device port to an untrusted network;
  use a reverse proxy with TLS or a private network.
- **A remote token can only write readings for one account.** It cannot
  read the status payload, list your accounts, or touch anything else.

The activity logs and the usage cache are not part of the pushed report
yet: the collector sends the terminal export only. A container therefore
shows Claude's quota, but not the VS Code panel's activity. That path is
not built yet.

**Codex needs a local daemon.** The Codex source runs the Codex app-server
on the machine it is on and uses that machine's login. A container has
neither Codex nor a login, so the source says it is unavailable. Run the
daemon locally for Codex, or wait for the push support that is planned.

## The environment variables

The compose file passes the documented variables through, so `.env` can
hold any of them:

| Variable | What it does |
| --- | --- |
| `DASH_SHARED_TOKEN` | The token the Presto device holds. Required by the compose file. |
| `DASH_PUBLISH_ADDRESS` | The host address the device port is published on. Every address by default; set your LAN address on a host with a public interface. |
| `DASH_ADMIN_TOKEN_FILE`, `DASH_SHARED_TOKEN_FILE`, `DASH_ENCRYPTION_KEY_FILE` | A path to a file holding the value, instead of the variable. Not both. |
| `DASH_HOST` / `DASH_PORT` | The device port. `0.0.0.0:8787` inside the container. |
| `DASH_ADMIN_HOST` | The admin page's bind address. `0.0.0.0` inside the container. |
| `DASH_ADMIN_ALLOWED_HOSTS` | The names the admin page answers to, comma separated. |
| `DASH_ADMIN_TOKEN` | The admin page's token. Required for a non-loopback bind address. |
| `DASH_ADMIN_SECURE_COOKIE` | `true` when a TLS proxy fronts the admin page, so the session cookie is marked `Secure`. |
| `DASH_DATABASE` | The database. `/data/dash.db`. |
| `DASH_KEY_FILE` | The key file. `/data/dash.db.key`. |
| `DASH_ENCRYPTION_KEY` | The encryption master key, instead of the key file. |
| `DASH_INGEST_ENABLED` | The remote reports switch. `0` by default. |
| `DASH_LOG_LEVEL` | `INFO` by default. |
| `DASH_OVERFLOW_ROWS` | The Home overflow strip. `1` by default. |
| `TZ` | The time zone of the process. |

## Troubleshooting

**The container will not start.** Read its log:

```bash
docker compose logs dash
```

The two messages you are most likely to see are the two refusals above: no
admin token, or no allowed hosts.

**The admin page answers 403.** The name in the browser's address bar is not
one the page knows. Add it to `DASH_ADMIN_ALLOWED_HOSTS`, including the
port the browser used (`127.0.0.1:8788`), and restart the container.

**The Presto says "Daemon offline".** The device port must be reachable
from the device: it is published on the LAN, so the Presto needs the
address of the machine that runs the container, and the firewall must let
8787 through. `curl http://<the machine>:8787/healthz` from another
machine on the LAN is the quickest check.

**A source says it is unavailable.** In a container that is expected for
the Claude readers and for Codex. The reason is in the account's status in
the admin page, and in the daemon's log.
