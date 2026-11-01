# Securing a deployment

Presto AI Dash is built for one person's desk and one trusted network. It
reads numbers, it never acts on a provider account, and it keeps your
provider keys encrypted. It does not try to defend against someone who
already has control of the machine or the network it runs on, so most of the
work of securing it is choosing where it runs. This page says what to do, and
what you accept if you do not.

## The short version

- Run the daemon on a network you trust, and do not forward its ports to the
  internet.
- Let the daemon generate its tokens, or generate your own with
  `python -c "import secrets; print(secrets.token_urlsafe(24))"`.
- Keep `dash.db` and its key in a folder only your account can write to.
- Leave the admin page on `127.0.0.1`.
- If a device or collector must reach the daemon from outside your LAN, put a
  private network such as Tailscale or a TLS reverse proxy in front of it.

## Who can reach what

| Port | Default bind | What it gives | Who should reach it |
| --- | --- | --- | --- |
| Status, 8787 | `0.0.0.0` (the LAN) | Your usage numbers and account names, and the one write the Presto makes: one service's alerts on or off | The Presto, the remote collectors, nothing else |
| Admin, 8788 | `127.0.0.1` | Everything: accounts, provider keys, ingest tokens | You, in a browser on the same machine |

**Anyone who holds the shared token can read the status payload.** That
includes your account names and quota percentages. They can also switch a
service's alerts on or off. They cannot read a provider key or change
anything else.

**The shared token travels over plain HTTP.** Anyone who can watch your
network can read it. That is a deliberate trade for a device with no TLS
stack, which is why the first rule is a network you trust. If you cannot say
that of your Wi-Fi, use a private network (Tailscale, WireGuard) between the
Presto's network and the daemon.

Concretely, someone who can capture traffic on that network (another device
on the same Wi-Fi, a compromised router, a guest on a shared network) gets:

- the shared token, which lets them read your account names and quota
  percentages and switch one service's alerts on or off, from then on;
- every status payload as it is fetched.

They do not get a provider key, the admin page or any ability to act on a
provider account, because none of those cross this port.

**The Presto cannot use TLS, and that will not change soon.** The device
runs MicroPython with no certificate store, so there is no safe way to check
a server's certificate on it, and encrypting without checking only hides the
traffic from a passive listener. The mitigation is the network, not the
device: a Wi-Fi network you control, a firewall rule, or a VPN. A TLS reverse
proxy protects collectors and the admin page, but not the Presto's own
fetches.

**There is one shared token for every Presto.** The daemon cannot tell
devices apart or revoke one. If a device is lost or you stop trusting a
network, rotate the shared token and update every device (see
[If something leaks](#if-something-leaks)). Per-device tokens are a possible
future change, not a current feature.

Use the firewall to narrow the status port to the Presto and your collectors,
for example on Windows:

```powershell
New-NetFirewallRule -DisplayName "Presto AI Dash status" -Direction Inbound `
  -Protocol TCP -LocalPort 8787 -RemoteAddress 192.168.1.60 -Action Allow
```

## Tokens

There are three, and they are separate on purpose.

| Token | Held by | Can do |
| --- | --- | --- |
| Shared token | The Presto | Read the status payload; set one service's alerts on or off |
| Admin token | You | Everything on the admin page |
| Ingest token | One remote collector | Write readings for one account, and nothing else |

If you let the daemon generate a token, it is long and random, and you need do
nothing. If you choose your own, the daemon refuses to start with one that is
under 16 characters, built from fewer than six different characters, or an
example from this documentation. A token someone can guess is the one thing between the network
and your usage data.

A wrong token is answered after a short pause and logged with the address it
came from, never with the token. The warning appears in the log and in the
admin page's **Problems** list. A run of them is either someone on your
network guessing or a Presto or collector still holding an old token.

The temporary tokens the daemon generates when you set none are printed to the
log at startup. That log is as sensitive as the token: do not paste it
anywhere.

## The admin page

The admin page binds to `127.0.0.1` and checks the `Host` and `Origin` of
every request, which is what stops a web page in your browser from driving it.
Keep it that way. To reach it from another computer, use an SSH tunnel to the
loopback address (`ssh -L 8788:127.0.0.1:8788 host`) instead of binding it to
the LAN.

If you must serve it on another address, the daemon requires an explicit
admin token, answers only to the names in `allowed_hosts`, and you should put
a TLS reverse proxy in front of it. Behind a proxy that terminates TLS, set
`admin.secure_cookie = true` (or `DASH_ADMIN_SECURE_COOKIE=true`) so the session
cookie is marked `Secure` and a browser never sends it over plain HTTP. Do not
turn it on for a plain-HTTP address: the browser would drop the cookie and
sign-in would not stick.

A browser session lasts seven days, and at most 25 are kept (the oldest go
first). When you set the admin token yourself, changing it ends every session
that was opened under the old one. With the temporary token the daemon
generates, sessions deliberately survive a restart, so that **Lock
configuration**, which ends the session in that browser only, is not needed on
every start. To end all of them at once, open **Settings > Browser sessions**
and press **Sign out of every browser**.

**Treat admin access as the ability to run programs as the daemon's account.**
An administrator can set which Codex executable the daemon starts, and the
daemon starts it on every poll. The setting must be a program named `codex`
on this computer, not a network share, but a file with that name can be put
anywhere, so this limits mistakes, not a determined administrator. That is by
design: the admin page is the configuration. It is why it must not be
exposed. The Codex process is started without the daemon's own secrets
(`DASH_*` variables and the provider key variable), so setting them in the
environment does not hand them to Codex.

## The database and its key

`dash.db` holds your accounts, your provider keys (encrypted), your readings
and your browser sessions. `dash.db.key` holds the key that decrypts them.

The daemon creates both so only your account can use them, and it tightens an
older database when it starts. On Windows that means your account, the
system and the administrators; on Linux and macOS, mode 0600.

That protects the files, not the folder. **Anyone who can write to the folder
can delete `dash.db` and put their own there**, and a database they control
decides which program the daemon runs and which address your saved keys are
sent to. The daemon warns at startup, in the log and the Problems list, when
the folder is writable by Everyone, Authenticated Users or Users (Windows) or
by group or other users (Linux and macOS). Heed it:

- Keep the database somewhere only you can write, for example
  `%LOCALAPPDATA%\presto-ai-dash\dash.db` on Windows or
  `~/.local/share/presto-ai-dash/dash.db` elsewhere, with `daemon.database`.
  Create the folder first. The key file moves with the database by default,
  and an existing database and its key must be moved together.
- On Windows, a folder under a data drive such as `D:\` usually lets every
  signed-in user write to it. The profile folder does not.
- Other accounts include the sandbox accounts that some coding agents run
  under. If an agent can write to your working folder, it can write there.
- Do not keep the database in a folder that is synced or shared.

The warning looks at the broad groups. It does not list a named account or a
custom group that has write access, so read the folder's permissions
yourself if you share the machine.

**The database is not checked for tampering.** Someone who can change it can
change the daemon's configuration, because the daemon trusts what it finds
there and does not sign its own rows. The control today is the one above: keep
it in a folder only you can write to, and treat the Problems warning as a
fault to fix, not a notice to ignore.

**Back up the database and the key separately**, and keep both. The daemon
will not start without the key it encrypted with. If the key is missing or
wrong, or a stored credential no longer decrypts with it, startup stops with
an error and the daemon neither makes a replacement key nor changes any
stored record. Restore the original key (the key file, or `DASH_ENCRYPTION_KEY`)
and start again. There is no automatic key rotation; to change a provider key,
replace it on the admin page. To keep the key off the disk entirely, set it as
`DASH_ENCRYPTION_KEY` (`python -m presto_ai_dash.app --generate-key` prints
one), and store it in your password manager or your service manager's secret
store.

## Docker

The image runs as a non-root user and holds only the installed package. To
keep it that way:

- Publish the status port on the LAN address only, not on every interface:
  set `DASH_PUBLISH_ADDRESS=192.168.1.50` in `.env` (it becomes
  `"192.168.1.50:8787:8787"` in `compose.yaml`). On Linux, Docker's own
  firewall rules bypass `ufw` and `firewalld`, so a published port is open to
  the whole network the host is on, whatever the host firewall says.
- Leave the admin port published on `127.0.0.1` only, as the example does.
- The tokens in `.env` are passed as environment variables, which anyone who
  can run `docker inspect` or read `/proc` on the host can see. Keep that
  group small, or give the daemon the tokens as files instead
  (`DASH_ADMIN_TOKEN_FILE`, `DASH_SHARED_TOKEN_FILE`,
  `DASH_ENCRYPTION_KEY_FILE`, for example from Compose `secrets:`), which
  keeps them out of `docker inspect`. The compose file requires
  `DASH_SHARED_TOKEN`, so a temporary one is never written to the container's
  log.
- The data volume belongs to the container's `dash` user and its files are
  readable by that user only.

## Remote collectors

A collector on another machine reports with its own ingest token, which can
write one account's readings and nothing else. It travels over plain HTTP
unless you put TLS in front. Over a network you do not control, use a VPN or
a TLS reverse proxy. Revoke a token in the admin page when a machine is
retired or lost, and create a new one for the replacement.

## If something leaks

| What | Do |
| --- | --- |
| The shared token | Set a new `daemon.shared_token`, restart the daemon, and put the same value in the Presto's private `config.py` as described in [Set up the Presto](../getting-started/set-up-the-presto.md) |
| The admin token | Set a new `admin.token` and restart. Every session opened under the old token ends. If you use the generated token, press **Sign out of every browser** in **Settings** as well |
| An ingest token | Revoke it in the admin page and create a new one |
| `dash.db` and `dash.db.key` together | Assume every stored provider key is exposed. Replace each key at the provider, then remove and re-add it |
| The log, if it held a temporary token | Set fixed tokens so the next start prints none |

## What this does not cover

Exposing either port directly to the internet, hosting for several users, and
protecting against an attacker who already runs code as your account on the
daemon's machine. In each of those the tokens and the database are not the
thing standing in the way.

## See also

- [Privacy](../privacy/index.md): what is read, stored and sent
- [Ports and endpoints](../reference/ports-and-endpoints.md)
- [Configuration keys and environment variables](../reference/configuration.md)
- [Install with Docker](../getting-started/install-with-docker.md)
