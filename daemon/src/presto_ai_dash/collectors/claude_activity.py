"""Claude Code local session-log activity reader: stdlib only, Python 3.8+.

Shows that Claude is in use on this machine even when its quota is stale, by
reading the JSONL session logs that the CLI and the VS Code panel write under
``<profile>/projects/<project>/<session>.jsonl``. The file format is not an
official interface (assumption E): anything unexpected is skipped and any
failure degrades to ``available: false`` with a short reason instead of raising.

Only these fields are extracted from each entry: ``timestamp``, ``entrypoint``,
``requestId``, ``message.id``, ``message.model`` and ``message.usage`` token
counts. The files also hold prompts, tool inputs, session identities and paths
(assumption C): this module never returns, stores, logs or forwards them.

Dedupe is essential. Measured on a real install (2026-10-19, field names and
counts only): 86 session files hold 22,009 usage entries, and inside one file
588 usage lines covered only 216 distinct requests - one API response is
written on several lines with identical usage, so summing lines would inflate
token totals about 2.7x. Each ``requestId`` therefore counts once (falling
back to ``message.id``, then to a signature of the parsed fields), and if two
lines of one request ever differ, the larger token total wins.

Tokens are ``input + output + cache_creation``; ``cache_read`` is excluded
because cached reads are cheap against subscription limits. Token totals are
shown as load, never converted to a percentage or a quota (assumption G).
The activity is a lower bound (assumption F): claude.ai, phone or other
machines never appear in these files.

Cold start. A week of real session logs is about 70 MB, more than one poll
should read at once, so the reader works in three bounded steps:

* it reads at most ``BYTES_PER_REFRESH`` bytes per refresh, in chunks of at
  most ``CHUNK_BYTES``, always continuing from the saved offset, so one
  refresh never holds more than a chunk (plus one line) in memory and never
  reads a whole huge file at once;
* it probes the tail of the newest files first, so the last request, the
  model and the surface - and with them the "in use" decision - are right on
  the very first refresh even when the newest file is still being read;
* it reports ``complete``: false while any file inside the eight-day window
  is still unread, true once every one of them has been read to its end.
  While it is false the token totals are lower bounds, and the caller shows
  that instead of numbers.
"""

import json
import os
from datetime import datetime, timezone

# Session-log lines above this size are skipped (real entries are far smaller).
MAX_LINE_BYTES = 1024 * 1024
# One file read never returns more than this, so memory stays bounded.
CHUNK_BYTES = 1024 * 1024
# Bytes one refresh may read. A week of logs is about 70 MB, so two refreshes
# finish a cold start; the work per poll stays bounded all the same.
BYTES_PER_REFRESH = 64 * 1024 * 1024
# The tail probe: the newest files are read from their end first, so the last
# use is known immediately. Never larger than one chunk.
PROBE_BYTES = 256 * 1024
PROBE_FILES = 20
# A cold scan only opens files Claude Code touched in the last 8 days.
FIRST_SCAN_MAX_AGE_SECONDS = 8 * 24 * 3600
# Only the newest 7 days of records are kept in memory and totalled.
KEEP_SECONDS = 7 * 24 * 3600
WINDOW_5H_SECONDS = 5 * 3600
# A record dated further ahead than this is not kept: more than a skewed clock explains.
FUTURE_TOLERANCE_SECONDS = 24 * 3600
# A model name is shown, so only a short printable ASCII name is kept.
MAX_MODEL_CHARS = 40

SURFACES = {"claude-vscode": "VS Code", "cli": "CLI"}
TOKEN_FIELDS = ("input_tokens", "output_tokens", "cache_creation_input_tokens")


def _iso(epoch_seconds):
    return datetime.fromtimestamp(epoch_seconds, timezone.utc).isoformat()


def _epoch(value):
    """Seconds for an ISO-8601 timestamp (``Z`` allowed); None if unusable."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.timestamp()


def _surface(value):
    """A short ASCII surface name: mapped, sanitised, or ``?``."""
    if not isinstance(value, str):
        return "?"
    if value in SURFACES:
        return SURFACES[value]
    cleaned = "".join(char for char in value
                      if char.isascii() and (char.isalnum() or char in "-_"))
    return cleaned[:8] or "?"


def _model(value):
    """A model name only when it is short printable ASCII; else None."""
    if not isinstance(value, str) or not value or len(value) > MAX_MODEL_CHARS:
        return None
    if not all(char.isascii() and char.isprintable() for char in value):
        return None
    return value


def _tokens(usage):
    """input + output + cache_creation; absent or malformed parts count zero."""
    total = 0
    for field in TOKEN_FIELDS:
        value = usage.get(field)
        if type(value) is int and value > 0:
            total += value
    return total


class ActivityReader:
    """Incremental, privacy-bounded reader of local Claude Code session logs.

    One instance lives as long as its source configuration: each ``refresh``
    re-reads only bytes appended since the last call, so repeated polls stay
    cheap. A settings edit rebuilds the source, and the new instance performs
    a fresh bounded first scan.
    """

    def __init__(self, projects_dir, clock=None, byte_cap=BYTES_PER_REFRESH,
                 chunk_bytes=CHUNK_BYTES, probe_bytes=PROBE_BYTES,
                 probe_files=PROBE_FILES):
        self.projects_dir = str(projects_dir)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._byte_cap = byte_cap
        self._chunk_bytes = chunk_bytes
        self._probe_bytes = min(probe_bytes, chunk_bytes)
        self._probe_files = probe_files
        # path -> {size, mtime, offset, skipping, ended}
        self._files = {}
        # dedupe id -> (timestamp, tokens, model, surface)
        self._records = {}

    def refresh(self, now=None):
        """Scan appended bytes and return the activity summary. Never raises."""
        now = now if now is not None else self._clock()
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        if not os.path.isdir(self.projects_dir):
            return self._failed("no session directory")
        try:
            complete = self._scan(now)
        except OSError:
            return self._failed("session directory unreadable")
        except Exception:
            # The unofficial format may change; a parse bug must never take
            # Claude's quota reporting down with it, and never logs internals.
            return self._failed("activity scan failed")
        try:
            return self._summary(now, complete)
        except Exception:
            # Whatever a hostile record holds, the answer is "unavailable",
            # not an exception that takes the whole Claude source offline.
            return self._failed("activity summary failed")

    # -- scanning ----------------------------------------------------------

    def _scan(self, now):
        """Read what the budget allows; say whether the window is complete."""
        root = os.path.realpath(self.projects_dir)
        found = []
        self._walk(self.projects_dir, root, found)
        found.sort(key=lambda item: item[2], reverse=True)  # newest first
        present = {item[0] for item in found}
        for path in [path for path in self._files if path not in present]:
            del self._files[path]  # the file is gone; its records stay in memory
        skip_before = now.timestamp() - FIRST_SCAN_MAX_AGE_SECONDS
        budget = [self._byte_cap]
        self._probe_tail(found, skip_before, budget)
        for path, size, mtime in found:
            state = self._files.get(path)
            if state is None:
                if mtime < skip_before:
                    self._files[path] = {"size": size, "mtime": mtime, "offset": 0,
                                         "skipping": False, "ended": True}
                    continue
                state = {"size": size, "mtime": mtime, "offset": 0, "skipping": False,
                         "ended": False}
                self._files[path] = state
            elif size < state["size"]:
                # shrank or was replaced: read it again from byte 0
                state.update({"size": size, "mtime": mtime, "offset": 0,
                              "skipping": False, "ended": False})
            elif mtime != state["mtime"]:
                state["mtime"] = mtime
            if state["ended"] and state["size"] == size:
                continue
            self._read_forward(path, size, mtime, state, budget)
        return all(state["ended"] for state in self._files.values()
                   if state["mtime"] >= skip_before)

    def _probe_tail(self, found, skip_before, budget):
        """Read the end of the newest cold files, so the last use is known now.

        The probed records go through the same dedupe as the full read, so
        reading a file again later never counts a request twice.
        """
        for path, size, mtime in found[:self._probe_files]:
            if budget[0] <= 0:
                break
            if path in self._files or mtime < skip_before:
                continue
            covered = self._probe(path, size, budget)
            if covered is not None:
                self._files[path] = {"size": size, "mtime": mtime, "offset": covered,
                                     "skipping": False, "ended": False}

    def _probe(self, path, size, budget):
        """Read the tail of one file; return the offset when it was all read."""
        start = max(0, size - self._probe_bytes)
        data = self._read_bytes(path, start, size - start)
        if not data:
            return None
        budget[0] -= len(data)
        if start:
            index = data.find(b"\n")
            if index < 0:
                return None  # no complete line in the probed window
            data = data[index + 1:]  # align forward to a line boundary
        cut = data.rfind(b"\n")
        if cut < 0:
            return None
        for raw in data[:cut].split(b"\n"):
            self._parse_line(raw)
        if start:
            return None  # the rest of this file still has to be read
        return start + cut + 1

    def _walk(self, directory, root, found):
        """Collect candidate session files; symlinks and escapes are refused."""
        with os.scandir(directory) as entries:
            for entry in entries:
                try:
                    if entry.is_symlink():
                        continue
                    stat = entry.stat(follow_symlinks=False)
                    if entry.is_dir(follow_symlinks=False):
                        self._walk(entry.path, root, found)
                    elif entry.is_file(follow_symlinks=False) and entry.name.endswith(".jsonl"):
                        if os.path.realpath(entry.path).startswith(root + os.sep):
                            found.append((entry.path, stat.st_size, stat.st_mtime))
                except OSError:
                    continue

    def _read_bytes(self, path, offset, count):
        """One bounded read; never more than one chunk in memory."""
        with open(path, "rb") as handle:
            handle.seek(offset)
            return handle.read(count)

    def _read_forward(self, path, size, mtime, state, budget):
        """Consume bytes from the saved offset, a chunk at a time.

        The offset only ever moves to the last complete newline, so a partial
        last line is read again once it is finished. A line longer than the
        limit is dropped without buffering it whole.

        The budget is soft by at most one line: a line already begun is
        finished even when the budget has run out, so every poll completes at
        least one line. Without that, a budget smaller than one line would
        re-read the same partial line forever. Real budgets are far larger
        than the line limit, so this only matters for tiny ones.
        """
        offset = state["offset"]
        skipping = state["skipping"]
        pending = b""
        pending_start = offset
        parsed_any = False
        while offset < size and (budget[0] > 0 or (pending and not parsed_any)):
            want = min(self._chunk_bytes, size - offset)
            data = self._read_bytes(path, offset, want)
            if not data:
                break
            budget[0] -= len(data)
            start = offset
            offset += len(data)
            if skipping:
                index = data.find(b"\n")
                if index < 0:
                    continue  # still inside the oversized line
                skipping = False
                offset = start + index + 1
                pending_start = offset
                continue
            pending += data
            while True:
                index = pending.find(b"\n")
                if index < 0:
                    break
                raw = pending[:index]
                pending = pending[index + 1:]
                pending_start += len(raw) + 1
                self._parse_line(raw)
                parsed_any = True
            if len(pending) > MAX_LINE_BYTES:
                # The line is longer than the limit: drop it, skip its tail.
                offset = pending_start
                pending = b""
                skipping = True
        state["offset"] = pending_start if pending else offset
        state["skipping"] = skipping
        state["size"] = size
        state["mtime"] = mtime
        state["ended"] = offset >= size

    def _parse_line(self, raw):
        """Parse one complete line; skip anything that is not a usage entry."""
        if len(raw) > MAX_LINE_BYTES or b'"usage"' not in raw:
            return  # cheap pre-filter: usage entries are the only ones that matter
        try:
            entry = json.loads(raw.decode("utf-8", "replace"))
        except ValueError:
            return
        self._record(entry)

    def _record(self, entry):
        """Keep one record per request: time, tokens, model, surface - nothing else."""
        if not isinstance(entry, dict):
            return
        message = entry.get("message")
        if not isinstance(message, dict):
            return
        usage = message.get("usage")
        if not isinstance(usage, dict):
            return
        stamp = _epoch(entry.get("timestamp"))
        if stamp is None:
            return
        record = (stamp, _tokens(usage), _model(message.get("model")),
                  _surface(entry.get("entrypoint")))
        key = entry.get("requestId")
        if not (isinstance(key, str) and key):
            key = message.get("id")
        if not (isinstance(key, str) and key):
            key = repr(record)  # no identity at all: only exact repeats collapse
        old = self._records.get(key)
        if old is None or record[1] > old[1]:
            self._records[key] = record

    # -- reporting ---------------------------------------------------------

    def _summary(self, now, complete):
        cutoff = now.timestamp() - KEEP_SECONDS
        ceiling = now.timestamp() + FUTURE_TOLERANCE_SECONDS
        # A record from the future is a skewed clock or a hostile file. It
        # would count as "within the last five hours" for ever, so it goes.
        for key in [k for k, value in self._records.items()
                    if value[0] < cutoff or value[0] > ceiling]:
            del self._records[key]
        five = [value for value in self._records.values()
                if now.timestamp() - value[0] < WINDOW_5H_SECONDS]
        latest = max(self._records.values(), key=lambda value: value[0]) \
            if self._records else None
        return {"available": True, "complete": complete,
                "last_request_at": _iso(latest[0]) if latest else None,
                "last_model": latest[2] if latest else None,
                "last_entrypoint": latest[3] if latest else "?",
                "requests_5h": len(five),
                "tokens_5h": sum(value[1] for value in five),
                "tokens_7d": sum(value[1] for value in self._records.values())}

    def _failed(self, reason):
        """The one honest answer when nothing was read: unavailable, no data."""
        return {"available": False, "reason": reason, "complete": False,
                "last_request_at": None, "last_model": None, "last_entrypoint": "?",
                "requests_5h": 0, "tokens_5h": 0, "tokens_7d": 0}
