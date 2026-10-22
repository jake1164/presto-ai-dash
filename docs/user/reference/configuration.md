# Configuration keys and environment variables

The daemon reads a TOML file, usually `dash.toml`. Every key is optional;
the values below are the defaults.

```toml
[daemon]
host = "0.0.0.0"
port = 8787
shared_token = ""
poll_interval = 15
database = "dash.db"
retention_hours = 72
cleanup_interval = 300
ingest_enabled = false

[logging]
level = "INFO"

[admin]
enabled = false
port = 8788
token = ""
key_file = ""

[display]
overflow_rows = 1

[alerts]
enabled = true
warning_percent = 80
critical_percent = 95
sound = false
quiet_hours = false
quiet_start = "22:00"
quiet_end = "07:00"
pop_to_top = true
mute_hours = 4

[sources.fake]
enabled = true
scenario_interval = 20
```

## `[daemon]`

| Key | Default | What it does |
| --- | --- | --- |
| `host` | `0.0.0.0` | The address the status listener binds to. All interfaces, so the Presto can reach it on the LAN |
| `port` | `8787` | The status port. The Presto polls this one |
| `shared_token` | `""` | The bearer token the Presto presents. Empty means one is generated at startup and printed once. One you choose needs at least 16 characters, at least 6 different ones, and must not be a documentation placeholder, or the daemon will not start |
| `poll_interval` | `15` | Seconds between collections |
| `database` | `dash.db` | The SQLite file |
| `retention_hours` | `72` | How long snapshots and alert observations are kept |
| `cleanup_interval` | `300` | Seconds between retention sweeps |
| `ingest_enabled` | `false` | Whether the remote reports route accepts anything |

## `[logging]`

| Key | Default | What it does |
| --- | --- | --- |
| `level` | `INFO` | One of `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL` |

## `[admin]`

| Key | Default | What it does |
| --- | --- | --- |
| `enabled` | `false` | Whether the configuration page is served. `--admin` also turns it on |
| `host` | `127.0.0.1` | The address the admin page listens on. A non-loopback address needs an explicit `token` and `allowed_hosts`, or the daemon will not start the page |
| `port` | `8788` | The admin port |
| `allowed_hosts` | `[]` | The names the page is reached by (for example `admin.example.lan:8788`), checked against the `Host` and `Origin` headers. The loopback names on the admin port always work |
| `token` | `""` | The admin token. Empty means a fresh one at every start, printed once. The same strength rule as `shared_token` applies to one you choose |
| `key_file` | `""` | Where the encryption master key lives. Empty means `<database>.key` |

## `[display]`

| Key | Default | What it does |
| --- | --- | --- |
| `overflow_rows` | `1` | The maximum number of lines the Home overflow strip may use, 0 to 3. 0 turns it off |

## `[alerts]`

These apply to every service. The admin page's **Alerts** section overrides
them.

| Key | Default | What it does |
| --- | --- | --- |
| `enabled` | `true` | The master switch for alerts |
| `warning_percent` | `80` | A window at this percentage makes the service *near limit*. 1 to 100 |
| `critical_percent` | `95` | A window at this percentage makes the service *critical*. 1 to 100, and above the warning percent |
| `sound` | `false` | Whether a service getting worse makes a noise. Off until you turn it on |
| `quiet_hours` | `false` | Whether there is a quiet window |
| `quiet_start` | `22:00` | When the quiet window starts, `HH:MM`, in the daemon's local time |
| `quiet_end` | `07:00` | When the quiet window ends |
| `pop_to_top` | `true` | Whether Home may scroll to a service that just got worse |
| `mute_hours` | `4` | How long a long-press mute on the Presto lasts, in hours. 0 to 48; 0 means it lasts until you press again |

There is no environment variable for these: they are your own choices.

The sound is off until you turn it on: a desk device must not beep
unannounced. Tick *Sound on a new warning* in the admin page's **Alerts**
section, or set `sound = true` here; the admin page's stored value wins
over the file. A value you already stored is respected: only the unset
default is off.

## Environment variables

Every variable below overrides the matching configuration key.

| Variable | Overrides | Notes |
| --- | --- | --- |
| `DASH_SHARED_TOKEN` | `daemon.shared_token` | The better place for a secret |
| `DASH_HOST` | `daemon.host` | |
| `DASH_PORT` | `daemon.port` | |
| `DASH_DATABASE` | `daemon.database` | |
| `DASH_LOG_LEVEL` | `logging.level` | |
| `DASH_ADMIN_TOKEN` | `admin.token` | Not printed when it is set |
| `DASH_ADMIN_HOST` | `admin.host` | The admin listen address; see the Docker guide |
| `DASH_ADMIN_ALLOWED_HOSTS` | `admin.allowed_hosts` | Comma-separated names |
| `DASH_KEY_FILE` | `admin.key_file` | |
| `DASH_ENCRYPTION_KEY` | The encryption master key | A Fernet key. Use it when the key file cannot be protected, such as in a container |
| `DASH_INGEST_ENABLED` | `daemon.ingest_enabled` and the stored switch | Accepts `1/true/yes/on` and `0/false/no/off`, case-insensitively. An invalid value fails closed: the route stays off |
| `DASH_OVERFLOW_ROWS` | `display.overflow_rows` | 0 to 3. A value outside that range stops the daemon at startup |
| `LITELLM_API_KEY` | The LiteLLM source's key | Only when `sources.litellm.api_key_env` names it |

An environment variable always wins over the file, and the admin page shows
the origin of every effective value.

## Where each value comes from

| Setting | Precedence |
| --- | --- |
| The display setting | environment, then the database, then the file, then the default |
| The alert settings | the database, then the file, then the default |
| The remote reports switch | the environment, then the database, then the file, then off |
| Everything else | the environment, then the file, then the default |

## Per-account settings

These live in the database and are edited in the admin page, not in the
file:

| Setting | Range | Default |
| --- | --- | --- |
| Mark idle readings stale after (seconds) | any | 900 |
| Mark usage cache readings stale after (seconds) | 300 to 604800 | 7200 |
| Treat Claude as in use for (seconds) after a request | any | 300 |
| Request timeout (seconds) | 1 to 60 | 10 |
| Refresh interval (seconds) | 30 to 3600 | 60 |

## Command line

| Flag | What it does |
| --- | --- |
| `--config PATH` | Which configuration file to read |
| `--admin` | Enable the configuration page |
| `--check` | Validate the configuration and exit, without starting the daemon |
| `--generate-key` | Print a new encryption master key and exit |

## See also

- [Ports and endpoints](ports-and-endpoints.md)
- [The admin page](../guides/admin-page.md)
