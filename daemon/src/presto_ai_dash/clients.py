"""In-memory registry of the clients that fetch the status endpoint.

The daemon has one shared token, so authentication cannot tell clients apart.
Each client declares a name in a request header (``X-Presto-Client``) and may
declare its app revision (``X-Presto-Revision``). The daemon remembers what it
saw, in memory only: nothing is stored in the database and nothing is sent
anywhere else.

Header values are untrusted. A value is kept only when it is printable ASCII
of at most 32 characters; anything else becomes ``unknown``. The address is
best effort: behind NAT, a proxy or Docker Desktop several clients can share
one address, so the name is what identifies a client.
"""

import threading
import time

MAX_CLIENTS = 16
MAX_HEADER_CHARS = 32
UNKNOWN = "unknown"


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


class ClientRegistry(object):
    """One record per (name, address), newest first, at most ``max_clients``.

    The records live in memory only and are lost when the daemon restarts.
    """

    def __init__(self, max_clients=MAX_CLIENTS, clock=None):
        self.max_clients = max(1, int(max_clients))
        self._clock = clock or time.time
        self._lock = threading.Lock()
        self._records = {}

    def record(self, name, address, revision=None, now=None):
        """Remember one fetch. Returns the stored record."""
        key = (sanitize_header(name), sanitize_header(address))
        moment = self._clock() if now is None else float(now)
        with self._lock:
            entry = self._records.get(key)
            if entry is None:
                entry = {"name": key[0], "address": key[1], "first_seen": moment,
                         "last_seen": moment, "fetches": 0, "revision": UNKNOWN}
                self._records[key] = entry
            entry["last_seen"] = moment
            entry["fetches"] += 1
            entry["revision"] = sanitize_header(revision)
            self._trim()
            return dict(entry)

    def _trim(self):
        """Drop the least recently seen client when there are too many."""
        while len(self._records) > self.max_clients:
            oldest = min(self._records.values(), key=lambda record: record["last_seen"])
            del self._records[(oldest["name"], oldest["address"])]

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
        return records

    def clear(self):
        with self._lock:
            self._records.clear()
