"""In-memory registry of the clients that fetch the status endpoint.

The daemon has one shared token, so authentication cannot tell clients apart.
Each client declares a name in a request header (``X-Presto-Client``) and may
declare its app revision (``X-Presto-Revision``). The daemon remembers what it
saw, in memory only: nothing is stored in the database and nothing is sent
anywhere else.

A client may also send ``X-Presto-Diag``: a short summary of the device's own
state (the last fetch failure, the failure count, free memory, the signal and
the uptime). It is parsed against a strict whitelist, kept in memory only, and
shown in the admin page's Status tab. A device that cannot reach the daemon
cannot send it, so the header only ever carries what a *previous* failure left
behind, plus the numbers of a healthy poll.

Header values are untrusted. A value is kept only when it is printable ASCII
of at most 32 characters; anything else becomes ``unknown``. The address is
best effort: behind NAT, a proxy or Docker Desktop several clients can share
one address, so the name is what identifies a client.
"""

import logging
import re
import threading
import time

log = logging.getLogger(__name__)

MAX_CLIENTS = 16
MAX_HEADER_CHARS = 32
UNKNOWN = "unknown"

# The diagnostics header: at most this many characters, and every character
# printable ASCII. Anything else is refused whole.
MAX_DIAG_CHARS = 120
# The error label: the exception's type name and, for an OSError, its errno.
MAX_DIAG_ERR_CHARS = 24
# The stages a fetch can stop at.
DIAG_STAGES = ("conn", "send", "head", "body", "parse")
# The keys the header may carry, with the range each number is allowed.
DIAG_KEYS = ("err", "st", "n", "free", "up", "rssi", "drops")
DIAG_RANGES = {
    "n": (0, 999999),
    "free": (0, 65535),
    "up": (0, 99999999),
    "rssi": (-127, 0),
    "drops": (0, 9999),
}

_ERR_PATTERN = re.compile(r"^[A-Za-z0-9_]+( -?\d{1,4})?$")
_INT_PATTERN = re.compile(r"^-?\d{1,8}$")


def sanitize_header(value, limit=MAX_HEADER_CHARS):
    """A printable ASCII label of at most ``limit`` characters, or ``unknown``.

    Nothing is filtered out of a hostile value: a value that is not entirely
    printable ASCII, or that is too long, becomes ``unknown`` rather than a
    partly-trusted string.
    """
    if not isinstance(value, str) or not value:
        return UNKNOWN
    if len(value) > limit:
        return UNKNOWN
    if any(not 32 <= ord(char) < 127 for char in value):
        return UNKNOWN
    text = value.strip()
    return text or UNKNOWN


def parse_diag(value):
    """The parsed diagnostics header, or ``None`` when it cannot be used.

    The header is untrusted input. It is kept only when it is short printable
    ASCII, and only the pairs whose key is known and whose value matches the
    whitelist are kept. A pair that is malformed or out of range is dropped,
    never clamped, and a rejected value is never echoed into a log or a
    response.

    A part that is not a ``key=value`` pair at all is refused whole: the
    header is a fixed grammar, and a part that does not fit it is not a
    value to be salvaged.
    """
    if not isinstance(value, str) or not value:
        return None
    if len(value) > MAX_DIAG_CHARS:
        return None
    if any(not 32 <= ord(char) < 127 for char in value):
        return None
    parsed = {}
    seen = set()
    for part in value.split(";"):
        if not part:
            continue
        if "=" not in part:
            return None
        key, _, raw = part.partition("=")
        if key not in DIAG_KEYS or key in seen:
            continue
        seen.add(key)
        pair = _parse_diag_pair(key, raw)
        if pair is not None:
            parsed[key] = pair
    return parsed


def _parse_diag_pair(key, raw):
    """One validated pair, or ``None`` when the value is not acceptable."""
    if key == "err":
        if len(raw) > MAX_DIAG_ERR_CHARS or not _ERR_PATTERN.fullmatch(raw):
            return None
        return raw
    if key == "st":
        return raw if raw in DIAG_STAGES else None
    if not _INT_PATTERN.fullmatch(raw):
        return None
    value = int(raw)
    low, high = DIAG_RANGES[key]
    return value if low <= value <= high else None


def _merge_diag(previous, parsed):
    """The diagnostics of this poll, with the last error kept.

    A poll replaces what was stored, but the error and the stage survive a
    poll that carries none: a recovery must not erase the evidence of what
    went wrong. A restarted device is the exception: its failure count or its
    uptime goes backwards, and the error of its previous run is then dropped,
    so the page never shows "Failures 0" beside an old error.
    """
    merged = dict(parsed)
    for key in ("n", "up"):
        if key in parsed and key in previous and parsed[key] < previous[key]:
            return merged
    for key in ("err", "st"):
        if not merged.get(key) and previous.get(key):
            merged[key] = previous[key]
    return merged


class ClientRegistry(object):
    """One record per (name, address), newest first, at most ``max_clients``.

    The records live in memory only and are lost when the daemon restarts.
    """

    def __init__(self, max_clients=MAX_CLIENTS, clock=None):
        self.max_clients = max(1, int(max_clients))
        self._clock = clock or time.time
        self._lock = threading.Lock()
        self._records = {}
        # The diagnostics last reported for each client, so a change is said
        # once and a steady value is not said again. The same idea as the
        # usage cache's warned-and-recovering bookkeeping.
        self._reported = {}

    def record(self, name, address, revision=None, now=None, diag=None):
        """Remember one fetch. Returns the stored record.

        ``diag`` is the raw ``X-Presto-Diag`` header. It is parsed against
        the whitelist and kept in memory only. The last error and stage are
        kept when a poll carries none, so a recovery does not erase the
        evidence of what went wrong.
        """
        key = (sanitize_header(name), sanitize_header(address))
        moment = self._clock() if now is None else float(now)
        parsed = parse_diag(diag) or {}
        with self._lock:
            entry = self._records.get(key)
            if entry is None:
                entry = {"name": key[0], "address": key[1], "first_seen": moment,
                         "last_seen": moment, "fetches": 0, "revision": UNKNOWN,
                         "diag": {}}
                self._records[key] = entry
            entry["last_seen"] = moment
            entry["fetches"] += 1
            entry["revision"] = sanitize_header(revision)
            entry["diag"] = _merge_diag(entry.get("diag") or {}, parsed)
            self._trim()
            said = self._change(key, entry["diag"])
        if said is not None:
            log.info('client "%s" reported a fetch problem: err=%s st=%s n=%s', *said)
        return dict(entry)

    def _change(self, key, diag):
        """The log arguments for a changed problem, or None.

        A steady value is not said again: only a change of the error, the
        stage or the failure count is worth a line. Only the sanitised
        values are used.
        """
        err = diag.get("err")
        stage = diag.get("st")
        count = diag.get("n")
        signature = (err, stage, count)
        if signature == self._reported.get(key):
            return None
        self._reported[key] = signature
        if not err and not stage and not count:
            return None
        return (key[0], err or "-", stage or "-",
                count if count is not None else "-")

    def _trim(self):
        """Drop the least recently seen client when there are too many."""
        while len(self._records) > self.max_clients:
            oldest = min(self._records.values(), key=lambda record: record["last_seen"])
            del self._records[(oldest["name"], oldest["address"])]
        for key in list(self._reported):
            if key not in self._records:
                del self._reported[key]

    def clients(self, now=None, poll_interval=None):
        """The known clients, most recently seen first.

        ``connected`` is false when the client has not been seen for three poll
        intervals: it is then treated as not connected.
        """
        moment = self._clock() if now is None else float(now)
        limit = 3 * (poll_interval if poll_interval else 15)
        with self._lock:
            records = [dict(entry) for entry in self._records.values()]
        records.sort(key=lambda record: record["last_seen"], reverse=True)
        for record in records:
            record["connected"] = (moment - record["last_seen"]) <= limit
            record["diag"] = dict(record.get("diag") or {})
        return records

    def clear(self):
        with self._lock:
            self._records.clear()
            self._reported.clear()
