# Install the daemon

The daemon is a normal Python program. It runs on Windows, macOS and Linux,
needs no service manager, and keeps all of its state in two files next to
where you start it.

## Requirements

| Component | Requirement |
| --- | --- |
| Daemon | Python 3.11 or newer |
| Network | The daemon and the Presto on the same LAN |
| Disk | A few megabytes for the SQLite history |

The daemon needs the `cryptography` package to encrypt the provider keys it
stores. Installing the project with `pip` brings it in.

## Install

From a checkout of the repository:

```bash
cd daemon
python -m venv .venv

# Windows
.venv\Scripts\activate
# macOS or Linux
source .venv/bin/activate

pip install -e .
```

If you prefer a virtual environment at the top of the repository instead,
create it there and install from the `daemon` directory:

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -e daemon
```

## Create a configuration file

The daemon reads a TOML file. Copy the example and edit it:

```bash
cp ../examples/config.example.toml dash.toml
```

At a minimum, set `daemon.shared_token`. That token is the key the Presto
presents to the daemon. If you leave it empty the daemon generates a strong
one at startup and prints it to the log once. To choose your own, generate it
rather than inventing it:

```bash
python -c "import secrets; print(secrets.token_urlsafe(24))"
```

```toml
[daemon]
host = "0.0.0.0"
port = 8787
shared_token = "<paste the generated token>"
poll_interval = 15
```

A token you choose must be at least 16 characters long, must not be a
placeholder from this documentation, and must not be one repeated character
or two. The daemon refuses to start with a weaker one and says which setting
to fix. The same rule applies to `admin.token`. Anyone on your network can
reach the status port, so a guessable token is the only thing between them and
your usage data.

`dash.toml` holds tokens, so keep it out of version control. The repository
already ignores it.

## Run it

```bash
python -m presto_ai_dash.app --config dash.toml
```

Or, if the console script was installed:

```bash
presto-ai-dash --config dash.toml
```

Add `--admin` to enable the local configuration page:

```bash
python -m presto_ai_dash.app --config dash.toml --admin
```

The daemon then listens on two ports:

| Port | Default | Who uses it |
| --- | --- | --- |
| Status | 8787 | The Presto, and anything else that wants the payload |
| Admin | 8788 | Your browser, on `127.0.0.1` only |

## Check that it works

```bash
curl -H "Authorization: Bearer your-token-here" \
     http://127.0.0.1:8787/api/v1/status
```

You should get JSON. Without the token, or with the wrong one, the endpoint
returns `401`.

Validate the configuration without starting the daemon:

```bash
presto-ai-dash --check --config dash.toml
```

Stop the daemon with `Ctrl+C`.

## VS Code

The repository ships a launch profile that starts the daemon with the admin
page. Open the repository root in VS Code, pick **Daemon + admin page** in
**Run and Debug**, and press `F5` to debug or `Ctrl+F5` to run. The profile
uses a virtual environment at the repository root and a private `dash.toml`
there.

Run one daemon at a time. The status listener and the admin listener belong
to the same process, and the listeners bind exclusively, so a second start
fails instead of two daemons answering on the same ports.

## What the daemon keeps on disk

| File | What it is |
| --- | --- |
| `dash.toml` | Bootstrap configuration. Keep it private. |
| `dash.db` | SQLite: accounts, encrypted keys, readings, history. |
| `dash.db.key` | The encryption master key, outside the database. |

The daemon creates `dash.db` and `dash.db.key` so that only your account (and
on Windows the system and the administrators) can read or change them, and it
tightens an existing database the same way when it starts. That protects the
files, not the folder: anyone who can write to the folder can delete the
database and put their own there, and a database they control decides which
address your saved keys are sent to. So the daemon warns, in its log and in the
admin page's Problems list, when the folder is writable by Everyone,
Authenticated Users or Users (Windows) or by group or other users (Linux and
macOS). The fix is to keep the database somewhere only you can write, for
example `%LOCALAPPDATA%\presto-ai-dash\dash.db`, set with `daemon.database`.

On Windows the key file is protected with DPAPI for your Windows account. On
Linux and macOS it is an owner-only file. If you need to move the install to
another machine, generate an external key instead:

```bash
python -m presto_ai_dash.app --generate-key
```

Store the result as `DASH_ENCRYPTION_KEY`. Keep the same key across restarts
and back the database and the key up separately.

## Environment variables

Anything in the configuration file can be set in the environment instead,
which is the better place for secrets.

| Variable | Overrides |
| --- | --- |
| `DASH_SHARED_TOKEN` | `daemon.shared_token` |
| `DASH_HOST` | `daemon.host` |
| `DASH_PORT` | `daemon.port` |
| `DASH_DATABASE` | `daemon.database` |
| `DASH_LOG_LEVEL` | `logging.level` |
| `DASH_ADMIN_TOKEN` | `admin.token` |
| `DASH_KEY_FILE` | `admin.key_file` |
| `DASH_ENCRYPTION_KEY` | The encryption master key |
| `DASH_INGEST_ENABLED` | `daemon.ingest_enabled`, the remote reports switch |
| `DASH_DEMO` | `demo.enabled`, the demo-mode switch |
| `DASH_OVERFLOW_ROWS` | `display.overflow_rows` |

The full list, with every key and its default, is in
[Configuration keys and environment variables](../reference/configuration.md).

## Next

- [Set up the Presto](set-up-the-presto.md)
- [Add the first source](add-the-first-source.md)
- [Securing a deployment](../guides/securing-a-deployment.md)
