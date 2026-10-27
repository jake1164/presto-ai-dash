"""An in-memory ring buffer of the daemon's recent log lines, for the admin page.

It keeps the last ``MAX_LINES`` records the daemon logs at its own log level, so
the admin page can show them without a log file or ``docker logs``. It lives in
memory only: it is empty again after a restart, and nothing is written to disk.

Each line is cleaned like the problems list is: short, ASCII, with paths and
credential-shaped text replaced, and an exception reduced to its type (a
traceback can quote file contents). A line that repeats one of the last few is
counted instead of added, so a source that fails every poll does not push
everything else out of the buffer.
"""

import logging
import threading
import time

from .problems import clean_logger, clean_message

MAX_LINES = 2000
# How far back a repeat is looked for. A few sources failing in turn still collapse.
REPEAT_WINDOW = 30
MAX_PAGE = MAX_LINES
DEFAULT_PAGE = 100
MAX_QUERY_CHARS = 100

LEVELS = {"DEBUG": logging.DEBUG, "INFO": logging.INFO, "WARNING": logging.WARNING,
          "ERROR": logging.ERROR}


class LogBuffer(logging.Handler):
    """Keeps the most recent log lines, oldest first, each with a number."""

    def __init__(self, max_lines=MAX_LINES, clock=None):
        super().__init__(level=logging.NOTSET)
        self.max_lines = max(1, int(max_lines))
        self._clock = clock or time.time
        self._lock = threading.Lock()
        self._lines = []
        self._next = 1

    def emit(self, record):
        """Never raises: a logging handler must not break the caller."""
        try:
            message = clean_message(record.getMessage())
            if record.exc_info and record.exc_info[0] is not None:
                message = "%s (%s)" % (message, record.exc_info[0].__name__)
            level, name, now = record.levelname, clean_logger(record.name), self._clock()
        except Exception:  # pragma: no cover - defensive
            return
        with self._lock:
            count = 1
            for line in reversed(self._lines[-REPEAT_WINDOW:]):
                if line["level"] == level and line["logger"] == name and line["message"] == message:
                    # Counted once and listed where it last happened, so a warning
                    # that keeps coming stays at the top of the newest-first list.
                    self._lines.remove(line)
                    count = line["count"] + 1
                    break
            self._lines.append({"n": self._next, "time": now, "level": level, "logger": name,
                                "message": message, "count": count})
            self._next += 1
            del self._lines[:-self.max_lines]

    def view(self, limit=DEFAULT_PAGE, level="DEBUG", text=""):
        """The newest lines first: ``{"lines", "matched", "held", "capacity"}``.

        ``level`` keeps that level and above; ``text`` keeps the lines that hold it,
        in the message or the logger, ignoring case. ``matched`` counts every line
        that passes the filter, so the page can say how many more there are.
        """
        floor = LEVELS.get(str(level).upper(), logging.DEBUG)
        needle = " ".join(str(text).split())[:MAX_QUERY_CHARS].lower()
        try:
            limit = max(1, min(int(limit), MAX_PAGE))
        except (TypeError, ValueError):
            limit = DEFAULT_PAGE
        with self._lock:
            held = len(self._lines)
            kept = [line for line in reversed(self._lines)
                    if logging.getLevelName(line["level"]) >= floor
                    and (not needle or needle in line["message"].lower()
                         or needle in line["logger"].lower())]
            return {"lines": [dict(line) for line in kept[:limit]], "matched": len(kept),
                    "held": held, "capacity": self.max_lines}

    def clear(self):
        with self._lock:
            self._lines = []
