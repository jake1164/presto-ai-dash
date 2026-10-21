# The admin page will not open

The admin page is served by the daemon itself on `127.0.0.1`. Most of the
time, the reason it does not open is one of four things.

## 1. The daemon was not started with `--admin`

The page is off by default. Start the daemon with the flag:

```bash
python -m presto_ai_dash.app --config dash.toml --admin
```

You can also enable it in the configuration file:

```toml
[admin]
enabled = true
port = 8788
```

## 2. The daemon is not running at all

Check on the machine that should be serving it:

```bash
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8788/
```

No answer means the daemon is not running, or it is running without the
admin page. Look at its terminal for the startup message; it prints the
address and the admin token.

## 3. You are on the wrong machine, or the wrong address

The admin listener binds to `127.0.0.1` and refuses requests whose `Host`
header is anything else. It is not reachable from another computer, on
purpose: it is the page that edits your accounts.

On a headless server, forward the port over SSH:

```bash
ssh -L 8788:127.0.0.1:8788 you@your-server
```

Then open <http://127.0.0.1:8788> in the browser on your own machine.

Use the same address every time: `localhost` and `127.0.0.1` have separate
cookies, so a session you unlock at one address does not unlock the other.

## 4. The token is wrong, or the session expired

The page asks for the **admin** token, not the shared token the Presto
uses. It is printed in the startup log, and it changes on every start unless
you set `admin.token` or `DASH_ADMIN_TOKEN`.

A browser session lasts seven days. When it expires you need the current
startup token to log in again. **Lock configuration** revokes the session
immediately.

## Other symptoms

| Symptom | Cause |
| --- | --- |
| The page loads but every action says *unauthorised* | The session cookie was dropped; log in again |
| The page loads but the account list is empty | The first admin start has not happened yet, so nothing has been imported into the database |
| A save returns 409 | The account was edited in another browser. Reload the page |
| The page is blank | The browser blocked a mixed-content or cross-origin request. Open it on `127.0.0.1` rather than through a proxy |
| The page cannot be reached from a container | The admin listener binds to loopback inside the container. Use `docker exec` or an SSH forward |

## A note on containers

The admin page is not reachable from outside a container, because the
listener binds to `127.0.0.1` and checks the `Host` header. That is a known
limit, not a misconfiguration on your side. Until the daemon can be told to
bind elsewhere, use a port forward.

## See also

- [The admin page](../guides/admin-page.md)
- [Ports and endpoints](../reference/ports-and-endpoints.md)
