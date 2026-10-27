"""An in-memory ring buffer of the daemon's recent warnings and errors.

The buffer exists so the admin page can show problems without reading a log
file. It holds the last 50 WARNING and ERROR records, de-duplicated, and it
lives in memory only: it is empty again after a restart.

Nothing that could carry a secret or a file's contents is kept. Only the log
message is shown, with paths and credential-shaped text replaced, and an
exception is reduced to its type: a traceback can quote file contents.
"""

import logging
import re
import threading
import time

MAX_PROBLEMS = 50
MAX_MESSAGE_CHARS = 200
MAX_LOGGER_CHARS = 64

# An absolute path, either kind. Quoted and unquoted.
# A URL's slashes are not a path: "http://host:8788/" stays readable, while a
# file:// URL, which names a file, is replaced.
_PATH = re.compile(r"file:/{1,3}[^\s'\"]+|[A-Za-z]:\\[^\s'\"]+"
                   r"|(?<![:/\w])/(?:[^\s'\"]+/)+[^\s'\"]*")
# Credential-shaped text: a bearer token, or a token/key parameter.
_SECRET = re.compile(
    r"(?i)\bbearer\s+\S+"
    # A credential-named key with its value, quoted or not: token=abc,
    # 'x-api-key': 'abc', password: abc, cookie: session=abc.
    r"|\b(?:x-api-key|api[_-]?key|access[_-]?token|token|pass(?:word|wd|phrase)?|pwd|secret|"
    r"cookie)['\"]?\s*[=:]\s*['\"]?[^\s'\",;&]+"
    r"|\bauthorization['\"]?\s*[=:]\s*['\"]?(?:[a-z][a-z0-9-]*\s+)?[^\s'\",;&]+"
    # A provider-style key on its own, and any long opaque run of characters.
    r"|\bsk-[A-Za-z0-9_-]{8,}|\b[A-Za-z0-9_-]{32,}\b")


def clean_message(message):
    """The message as it may be shown: short, ASCII, without paths or secrets."""
    text = " ".join(str(message).split())
    text = _PATH.sub("<path>", text)
    text = _SECRET.sub("<redacted>", text)
    if len(text) > MAX_MESSAGE_CHARS:
        text = text[:MAX_MESSAGE_CHARS - 3] + "..."
    return text


def clean_logger(name):
    """A short printable label for the logger that wrote the record."""
    text = "".join(char for char in str(name) if 32 <= ord(char) < 127)
    return text[:MAX_LOGGER_CHARS] or "unknown"


class ProblemBuffer(logging.Handler):
    """Keeps the last warnings and errors, newest first, de-duplicated."""

    def __init__(self, max_entries=MAX_PROBLEMS, clock=None):
        super().__init__(level=logging.WARNING)
        self.max_entries = max(1, int(max_entries))
        self._clock = clock or time.time
        self._lock = threading.Lock()
        self._records = []

    def emit(self, record):
        """Never raises: a logging handler must not break the caller."""
        try:
            entry = self._entry(record)
        except Exception:  # pragma: no cover - defensive
            return
        with self._lock:
            for existing in self._records:
                if (existing["level"] == entry["level"] and
                        existing["logger"] == entry["logger"] and
                        existing["message"] == entry["message"]):
                    existing["count"] += 1
                    existing["last_seen"] = entry["time"]
                    self._records.remove(existing)
                    self._records.insert(0, existing)
                    return
            self._records.insert(0, entry)
            del self._records[self.max_entries:]

    def _entry(self, record):
        message = clean_message(record.getMessage())
        if record.exc_info and record.exc_info[0] is not None:
            # The type only: a traceback can quote file contents.
            message = "%s (%s)" % (message, record.exc_info[0].__name__)
        return {"time": self._clock(), "level": record.levelname,
                "logger": clean_logger(record.name), "message": message, "count": 1}

    def problems(self):
        """The buffered problems, newest first."""
        with self._lock:
            return [dict(record) for record in self._records]

    def clear(self):
        with self._lock:
            self._records = []
