# Writing a collector

There are two kinds of collector in this project, and they solve different
problems.

| Kind | Where it runs | What it does |
| --- | --- | --- |
| A **local reader** | Inside the daemon, on the machine where the tool wrote its files | Reads a file and turns it into readings |
| A **push collector** | On another computer, as a standalone script | Normalises a reading and posts it to the daemon |

Both are in `daemon/src/presto_ai_dash/collectors/`. Both are standard
library only, so the push collector can be copied to a machine with nothing
but Python.

## A local reader

A local reader is a class with a `read()` method that returns a dict, or
`None` when it could not read anything. It never raises: a failure becomes
`{"available": False, "reason": "..."}`.

Look at `collectors/claude_usage_cache.py`: it opens a file read-only,
extracts four fields, and reports why it could not do that. The rules it
follows are the rules every reader follows.

### The rules

1. **Read only.** Open the file read-only. Never write, rename, lock or
   touch it.
2. **Extract a whitelist, not a blacklist.** Name the fields you take. The
   file will hold more than you need, including things you must not keep.
3. **Never raise.** A missing file, a permission error, a format change:
   each becomes an `available: False` with a short reason.
4. **Say the reason in words.** "format changed", "access denied", "no usage
   cache found". The reason is shown on the admin page and it never contains
   a value.
5. **Degrade, do not break.** A reader that cannot read its file must not
   stop the other sources, and must not damage the quota display.
6. **Bound the work.** A cap on the file size, on the bytes read per
   refresh, and on what you keep in memory. A week of session logs is tens
   of megabytes; the reader reads it in chunks and says so while it is
   still scanning.

The activity reader, `collectors/claude_activity.py`, adds two more:

- **Dedupe.** One API response is written on several lines, so counting
  lines inflates the token totals. Count each response once, by its request
  ID.
- **Be incremental.** Remember each file's size, modification time and
  offset, and read only the bytes that were appended.

### A reader's output

```python
{"available": True, "reason": None, "message": "ok",
 "path": "/home/you/.claude.json", "fetched_at": "2026-10-20T14:22:00+00:00",
 "credit": {"limit": 100, "used": 0, "currency": "USD"}}
```

The reason codes are constants at the top of the module, so the tests and
the admin page agree on the wording.

## A push collector

A push collector runs on the computer where the tool lives and posts to the
daemon. `collectors/claude_statusline.py` is the reference: it is the same
code that drives the local Claude export, and it is downloadable from the
admin page so a remote machine does not need the repository.

### The envelope

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

The validator in `ingest.py` is strict on purpose:

- the body is at most 16 KiB, UTF-8 JSON, an object;
- `schema` must be exactly `presto-ai-dash.observation/1`;
- **unknown top-level or window keys are rejected, not ignored**;
- `collector` has exactly `name`, `version` and `host`;
- a window has exactly `id`, `label`, `period_minutes`, `used_percent`,
  `resets_at` and `state`;
- at most 8 windows; the IDs match `[a-z0-9_]{1,32}`, are unique, and must
  be in the source's `push_window_ids` when it declares them;
- `used_percent` is null or a finite number from 0 to 1000;
- `period_minutes` is null or an integer from 1 to 527040;
- `resets_at` is null or within 400 days;
- strings are printable ASCII with length caps, and the hashes are 64
  lowercase hex characters.

A failure is a `400` and nothing is stored.

### The responses

| Code | Meaning |
| --- | --- |
| `204` | Accepted |
| `400` | Invalid. Nothing was stored |
| `401` | The token is wrong or revoked |
| `404` | Ingest is off. The route does not reveal itself |
| `413` | The body is too large |
| `429` | The same token posted again within two seconds |

The response body never echoes the input.

### What a collector must do

1. **Never raise.** A collector that fails still has to let the user's tool
   work. Exit 0 on every failure.
2. **Never stall.** The whole post is bounded - 1.5 seconds in the reference
   collector. A status line that hangs is worse than no status line.
3. **Throttle.** Skip a heartbeat sent less than 60 seconds ago, skip an
   identical report sent less than 60 seconds ago, and stop posting for
   five minutes after a network failure.
4. **Refuse redirects.** A redirect counts as a failed post, so a token is
   never forwarded to another host.
5. **Keep the token out of the command line.** Read it from a file, so a
   settings file can be shared without leaking it.
6. **Send no credentials.** The envelope carries readings and hashes, not
   tokens, session identities or prompt text.
7. **Let the daemon own the time.** `client_time` is informational. The
   daemon stamps the reading with its own receive time, so a remote machine
   with a wrong clock cannot make a stale number look fresh.

### The one exception

The usage cache reading may be hours old when it is pushed. The envelope
therefore carries the reading's own `fetched_at`, the daemon accepts it only
for that object, caps it at the receive time, rejects anything older than
seven days or in the future, and uses it as the window's `observed_at`.

## Writing a new collector

1. Put it in `daemon/src/presto_ai_dash/collectors/`, standard library only.
2. Give it a module docstring that says what it reads and what it never
   reads.
3. Use the shared reason codes, or define new ones at the top of the
   module.
4. Write the tests with synthetic fixtures: a good file, a missing file, a
   malformed file, an oversized file, an unreadable file.
5. For a push collector, add the envelope keys to the validator's
   allow-lists in `ingest.py`, and the tests for every rejection rule.
6. For a pull source, wire the reader into the source's `collect()` and
   turn it into a report with a provenance label.

## See also

- [How a source works](how-a-source-works.md)
- [The worked example](worked-example.md)
