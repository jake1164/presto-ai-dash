"""Claude Code usage cache reader: stdlib only, Python 3.8+.

Claude Code caches the numbers behind its ``/usage`` dialog in a JSON file on
disk: ``~/.claude.json`` in the default layout (a sibling of the ``~/.claude``
directory, not inside it) and ``<config dir>/.claude.json`` for a custom
``CLAUDE_CONFIG_DIR``. That file is the only quota source that works for
people who use Claude in the VS Code panel, which never runs the status line.

**The file is sensitive.** It also holds the account id, the email, the
organisation, the project history and the MCP server list. This module opens
it read-only, parses it in memory and extracts only:

* ``cachedUsageUtilization.fetchedAtMs`` - when the numbers were fetched;
* ``cachedUsageUtilization.utilization.five_hour`` and ``.seven_day`` - the
  percentage and the reset time of the two quota windows;
* the included credit, found by SHAPE (a dict with numeric ``limit_dollars``
  and ``used_dollars``), never by its obfuscated key, which will change;
* a SHA-256 of ``accountUuid``, kept in memory only, to notice that the
  account behind the cache changed.

Nothing else is read, returned, logged, hashed for storage or forwarded. The
file is never written, renamed, locked or touched: the reader only ever opens
it in read-only binary mode.

The format is unofficial (assumption E): it can change at any time. Every
failure degrades to "unavailable" with a precise reason. Nothing here ever
raises, and quota reporting from the other sources is never affected.
"""

import argparse
import copy
import hashlib
import json
import logging
import math
import os
import re
import stat as stat_module
import sys
from datetime import datetime, timedelta, timezone

# The daemon's logger name for this module, whatever the file was imported as.
# It is the daemon's package name plus ".claude_usage_cache" inside the daemon,
# and plain "claude_usage_cache" when the file travels to another machine.
_PACKAGE = (__package__ or "").split(".")[0]
LOGGER = logging.getLogger((_PACKAGE + ".claude_usage_cache") if _PACKAGE
                           else "claude_usage_cache")

# The file is read only up to this size. A real one is about 95 KB; the
# "projects" key can make it far larger, and a huge file is not worth parsing.
MAX_FILE_BYTES = 32 * 1024 * 1024
# A reset time further away than this is not a provider reading.
MAX_RESET_DAYS = 400
# A percentage above this is not a usage percentage.
MAX_PERCENT = 1000
# 2020-01-01T00:00:00Z: a fetch time before this is not a real reading.
MIN_FETCH_SECONDS = 1577836800
# Below this the timestamp is seconds, not milliseconds: a format change, and
# never something to guess at.
MILLISECONDS_FLOOR = 10 ** 11
# A reading older than this is still returned, but the message says so.
OLD_FETCH_SECONDS = 7 * 24 * 3600
# The same problem is warned about at most this often.
WARN_INTERVAL_SECONDS = 30 * 60
# Two consecutive transient failures are tolerated; the third is persistent.
TRANSIENT_LIMIT = 2

WINDOWS = (("five_hour", "5h", 300), ("seven_day", "Weekly", 10080))
# Keys inside "utilization" that are never a credit entry.
CREDIT_SKIP_KEYS = ("five_hour", "seven_day", "extra_usage", "spend")

REASON_MISSING = "no usage cache found"
REASON_TOO_LARGE = "usage cache too large"
REASON_DENIED = "access denied"
REASON_FORMAT = "format changed"
REASON_NOT_FILE = "not a regular file"
REASON_ACCOUNT = "account changed"
REASON_TRANSIENT = "read problem"
REASON_NO_DATA = "no quota reported"

_UNAFFECTED = ("Quota from the usage cache is unavailable; terminal-export "
               "readings are unaffected.")
_SILENCE = ("Untick 'Read Claude Code usage cache' in the admin page to "
            "silence this.")


def _type_name(value):
    """A JSON type name for messages; never the value itself."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, list):
        return "array"
    return type(value).__name__


def _iso(epoch_seconds):
    """ISO-8601 UTC for a moment."""
    return datetime.fromtimestamp(epoch_seconds, timezone.utc).isoformat()


_ISO_PATTERN = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})(?::(\d{2})(?:[.,](\d{1,6}))?)?"
    r"(Z|z|[+-]\d{2}:?\d{2})?$")


def _parse_iso(text):
    """Seconds for an ISO-8601 timestamp with an offset; None if unusable.

    Parsed by hand so the accepted spellings are known: ``Z``, microseconds
    and ``+00:00`` all occur in the file, and a timestamp without an offset
    is not a provider reading.
    """
    if not isinstance(text, str):
        return None
    match = _ISO_PATTERN.match(text.strip())
    if not match:
        return None
    year, month, day, hour, minute, second, fraction, offset = match.groups()
    if offset is None:
        return None
    if offset in ("Z", "z"):
        shift = timedelta(0)
    else:
        sign = 1 if offset[0] == "+" else -1
        digits = offset[1:].replace(":", "")
        if len(digits) != 4:
            return None
        shift = sign * timedelta(hours=int(digits[:2]), minutes=int(digits[2:]))
    microsecond = int((fraction or "0").ljust(6, "0"))
    try:
        moment = datetime(int(year), int(month), int(day), int(hour), int(minute),
                          int(second or 0), microsecond, timezone(shift))
    except ValueError:
        return None
    return moment.timestamp()


def candidate_paths(settings_path, override=None):
    """The usage cache paths to try, most specific first.

    The explicit override wins. Then the settings file's own directory, which
    is where a custom ``CLAUDE_CONFIG_DIR`` keeps its state file. Then, only
    when that directory is the default ``.claude`` folder, its parent: the
    default layout keeps the state file beside the folder, not inside it.
    """
    candidates = []
    if override:
        candidates.append(str(override))
    if settings_path:
        directory = os.path.dirname(os.path.abspath(str(settings_path)))
        candidates.append(os.path.join(directory, ".claude.json"))
        if os.path.basename(directory) == ".claude":
            candidates.append(os.path.join(os.path.dirname(directory), ".claude.json"))
    return candidates


class FieldProblem(Exception):
    """A field the cache must have but does not have in the expected shape.

    The message names the field's path and the type that was found; the value
    itself is never mentioned.
    """

    def __init__(self, field, expected, found, reason=REASON_FORMAT):
        Exception.__init__(self, field)
        self.field = field
        self.expected = expected
        self.found = found
        self.reason = reason


class AccountChanged(Exception):
    """The account behind the cache is not the one seen when the daemon started."""


class UsageCacheReader:
    """Reads Claude Code's local usage cache, defensively and incrementally.

    One instance lives as long as its source configuration. Each ``refresh``
    stats the file first and re-reads it only when its size or its
    modification time changed, so repeated polls stay cheap. Nothing is ever
    written, and no failure ever leaves this method: the result always says
    whether a reading is available and why not when it is not.
    """

    def __init__(self, path_candidates, clock=None, max_bytes=MAX_FILE_BYTES):
        self.path_candidates = [str(path) for path in path_candidates]
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._max_bytes = max_bytes
        # (size, mtime_ns) of the last successful parse and its result.
        self._stat = None
        self._result = None
        # A SHA-256 of the account id, in memory only. Never returned, logged
        # or stored: it only says "this is a different account".
        self._account_fingerprint = None
        self._failures = 0
        self._warned = {}
        self._announced = set()
        self._recovering = False
        # Set when the account behind the cache changed: the file is then read
        # again on every poll, so the change keeps being reported.
        self._blocked = False

    # -- reading -----------------------------------------------------------

    def refresh(self, now=None):
        """Read the cache and return what it holds. Never raises."""
        now = now if now is not None else self._clock()
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        try:
            return self._refresh(now)
        except Exception as error:  # noqa: BLE001 - an unofficial format may change
            LOGGER.debug("Claude usage cache: unexpected problem (%s)",
                         type(error).__name__)
            return self._transient(now, None, type(error).__name__)

    def _refresh(self, now):
        path = self._locate()
        if path is None:
            return self._persistent(now, REASON_MISSING, self._missing_message(), None)
        try:
            info = os.stat(path)
        except FileNotFoundError:
            return self._persistent(now, REASON_MISSING, self._missing_message(), None)
        except PermissionError:
            return self._transient(now, path, "PermissionError")
        except OSError as error:
            return self._transient(now, path, type(error).__name__)
        if not stat_module.S_ISREG(info.st_mode):
            return self._persistent(now, REASON_NOT_FILE, self._not_a_file_message(path),
                                    path)
        if info.st_size > self._max_bytes:
            return self._persistent(now, REASON_TOO_LARGE,
                                    self._too_large_message(path, info.st_size), path)
        self._announce(path)
        if (self._result is not None and not self._blocked and
                self._stat == (info.st_size, info.st_mtime_ns)):
            # Nothing changed since the last good read: do not open the file.
            return copy.deepcopy(self._result)
        return self._read(now, path, info)

    def _locate(self):
        """The first candidate that exists, or None."""
        for candidate in self.path_candidates:
            try:
                if os.path.exists(candidate):
                    return candidate
            except OSError:
                continue
        return None

    def _read(self, now, path, info):
        """Open the file read-only, parse it, and report what it holds."""
        try:
            with open(path, "rb") as handle:
                data = handle.read(self._max_bytes)
        except PermissionError:
            return self._transient(now, path, "PermissionError")
        except FileNotFoundError:
            return self._persistent(now, REASON_MISSING, self._missing_message(), None)
        except OSError as error:
            return self._transient(now, path, type(error).__name__)
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            return self._transient(now, path, "UnicodeDecodeError")
        try:
            document = json.loads(text, parse_constant=_reject_constant)
        except (ValueError, RecursionError):
            # A file that is being rewritten is read half-written now and then.
            return self._transient(now, path, "JSONDecodeError")
        try:
            result, digest = self._build(now, path, document)
        except FieldProblem as problem:
            return self._persistent(now, self._format_reason(problem),
                                    self._format_message(path, problem), path)
        except AccountChanged:
            self._blocked = True
            return self._persistent(now, REASON_ACCOUNT, self._account_message(path),
                                    path, account_changed=True)
        if digest is not None and self._account_fingerprint is None:
            self._account_fingerprint = digest
        self._stat = (info.st_size, info.st_mtime_ns)
        self._result = copy.deepcopy(result)
        self._failures = 0
        if self._recovering:
            LOGGER.info("Claude usage cache: RECOVERED - readable again (fetched %s)",
                        _local_time(result["fetched_at"]))
            self._recovering = False
        return result

    def _build(self, now, path, document):
        """Validate the document and build the result. Never trusts a value."""
        if not isinstance(document, dict):
            raise FieldProblem("the file root", "an object", _type_name(document))
        cached = document.get("cachedUsageUtilization")
        if not isinstance(cached, dict):
            raise FieldProblem("cachedUsageUtilization", "an object", _type_name(cached))
        utilization = cached.get("utilization")
        if not isinstance(utilization, dict):
            raise FieldProblem("cachedUsageUtilization.utilization", "an object",
                               _type_name(utilization))
        fetched = self._fetched_at(cached, now)
        digest = self._account_digest(cached)
        if digest is not None and self._account_fingerprint is not None \
                and digest != self._account_fingerprint:
            raise AccountChanged()
        windows = []
        problems = []
        notes = []
        usable = 0
        for window_id, label, minutes in WINDOWS:
            try:
                window = self._window(utilization.get(window_id), window_id, label,
                                      minutes, now)
            except FieldProblem as problem:
                problems.append(problem)
                message = self._format_message(path, problem)
                self._warn(now, self._format_reason(problem), message)
                notes.append(message)
                window = {"id": window_id, "label": label, "period_minutes": minutes,
                          "used_percent": None, "resets_at": None, "state": "unavailable"}
            else:
                if window["used_percent"] is not None:
                    usable += 1
            windows.append(window)
        if not usable:
            if problems:
                raise problems[0]
            raise FieldProblem("cachedUsageUtilization.utilization", "a quota reading",
                               "no window data", reason=REASON_NO_DATA)
        stale = self._fresh_message(now, fetched)
        if stale:
            notes.append(stale)
        result = self._result_dict(True, None, " ".join(notes) or None, path,
                                   _iso(fetched), windows,
                                   self._credit(utilization, now))
        return result, digest

    def _fetched_at(self, cached, now):
        """The fetch time in seconds; a bad value is a format change."""
        value = cached.get("fetchedAtMs")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise FieldProblem("cachedUsageUtilization.fetchedAtMs", "a number",
                               _type_name(value))
        if not math.isfinite(value):
            raise FieldProblem("cachedUsageUtilization.fetchedAtMs", "a number",
                               "not a finite number")
        if value < MILLISECONDS_FLOOR:
            # Seconds instead of milliseconds: the format changed, and the
            # reader never guesses which unit a number means.
            raise FieldProblem("cachedUsageUtilization.fetchedAtMs",
                               "milliseconds since the epoch", "seconds")
        moment = value / 1000.0
        if moment < MIN_FETCH_SECONDS or moment > now.timestamp() + 86400:
            raise FieldProblem("cachedUsageUtilization.fetchedAtMs",
                               "a time between 2020 and now", "a time out of range")
        return min(moment, now.timestamp())

    def _account_digest(self, cached):
        """A SHA-256 of the account id, or None when there is none."""
        value = cached.get("accountUuid")
        if not isinstance(value, str) or not value:
            return None
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    def _window(self, entry, window_id, label, minutes, now):
        """One quota window; a bad field drops the window with its reason."""
        if entry is None:
            return {"id": window_id, "label": label, "period_minutes": minutes,
                    "used_percent": None, "resets_at": None, "state": "unavailable"}
        field = "cachedUsageUtilization.utilization." + window_id
        if not isinstance(entry, dict):
            raise FieldProblem(field, "an object", _type_name(entry))
        percent = entry.get("utilization")
        if (isinstance(percent, bool) or not isinstance(percent, (int, float)) or
                not math.isfinite(percent) or percent < 0 or percent > MAX_PERCENT):
            raise FieldProblem(field + ".utilization", "a number", _type_name(percent))
        resets_at = None
        reset = entry.get("resets_at")
        if reset is not None:
            moment = _parse_iso(reset)
            if moment is None:
                raise FieldProblem(field + ".resets_at",
                                   "an ISO-8601 timestamp with an offset",
                                   _type_name(reset))
            if abs(moment - now.timestamp()) > MAX_RESET_DAYS * 86400:
                raise FieldProblem(field + ".resets_at", "a time within 400 days",
                                   "a time out of range")
            resets_at = _iso(moment)
        return {"id": window_id, "label": label, "period_minutes": minutes,
                "used_percent": percent, "resets_at": resets_at, "state": "available"}

    def _credit(self, utilization, now):
        """The included credit, found by shape and never by its key."""
        best = None
        for key, value in utilization.items():
            if key in CREDIT_SKIP_KEYS or not isinstance(value, dict):
                continue
            limit = value.get("limit_dollars")
            used = value.get("used_dollars")
            if (isinstance(limit, bool) or not isinstance(limit, (int, float)) or
                    isinstance(used, bool) or not isinstance(used, (int, float)) or
                    not math.isfinite(limit) or not math.isfinite(used) or limit <= 0):
                continue
            reset = value.get("resets_at")
            if reset is None:
                continue
            moment = _parse_iso(reset)
            if moment is None or abs(moment - now.timestamp()) > MAX_RESET_DAYS * 86400:
                continue
            if best is None or moment < best[0]:
                best = (moment, limit, used)
        if best is None:
            return None
        return {"limit": best[1], "used": best[2], "currency": "USD",
                "resets_at": _iso(best[0])}

    # -- failures ----------------------------------------------------------

    def _result_dict(self, available, reason, message, path, fetched_at, windows,
                     credit, account_changed=False):
        return {"available": available, "reason": reason, "message": message,
                "path": path, "fetched_at": fetched_at, "windows": windows,
                "credit": credit, "account_changed": account_changed}

    def _persistent(self, now, reason, message, path, account_changed=False,
                    keep_counter=False):
        """A problem that stays until the file or the daemon changes."""
        if not keep_counter:
            self._failures = 0
        self._warn(now, reason, message)
        self._recovering = True
        return self._result_dict(False, reason, message, path, None, [], None,
                                 account_changed)

    def _transient(self, now, path, kind):
        """A problem that usually goes away on the next poll.

        The first failures are logged at DEBUG and the last known reading is
        kept; a problem that survives becomes a real one.
        """
        self._failures += 1
        self._recovering = True
        if self._failures > TRANSIENT_LIMIT:
            reason = REASON_DENIED if kind == "PermissionError" else REASON_FORMAT
            message = self._persistent_message(reason, path, kind)
            return self._persistent(now, reason, message, path, keep_counter=True)
        LOGGER.debug("Claude usage cache: transient read problem (%s)%s. The last "
                     "known reading is kept and the file is read again on the next poll.",
                     kind, (" at " + path) if path else "")
        message = ("the usage cache could not be read this time (%s); the last known "
                   "reading is kept" % kind)
        if self._result is not None:
            result = copy.deepcopy(self._result)
            result["reason"] = REASON_TRANSIENT
            result["message"] = message
            return result
        self._recovering = True
        return self._result_dict(False, REASON_TRANSIENT, message, path, None, [], None)

    def _warn(self, now, reason, message):
        """Warn about a problem, then at most once every 30 minutes."""
        last = self._warned.get(reason)
        if last is not None and (now - last).total_seconds() < WARN_INTERVAL_SECONDS:
            return
        self._warned[reason] = now
        LOGGER.warning(message)

    def _announce(self, path):
        """Say once which file is being read."""
        if path in self._announced:
            return
        self._announced.add(path)
        LOGGER.info("Claude usage cache: reading %s", path)

    # -- messages ----------------------------------------------------------

    def _format_reason(self, problem):
        if problem.reason == REASON_NO_DATA:
            return problem.reason
        return "%s: %s (%s)" % (problem.reason, problem.field, problem.found)

    def _format_message(self, path, problem):
        if problem.reason == REASON_NO_DATA:
            return ("Claude usage cache: NO QUOTA READING - %s holds no quota for "
                    "either window right now. %s Open /usage in Claude Code to "
                    "fetch usage; %s" % (path, _UNAFFECTED, _SILENCE))
        return ("Claude usage cache: FORMAT CHANGED - expected %s at %s in %s but "
                "found %s. %s If Claude Code was updated this reader needs updating: "
                "%s" % (problem.expected, problem.field, path, problem.found,
                        _UNAFFECTED, _SILENCE))

    def _missing_message(self):
        tried = ", ".join(self.path_candidates) if self.path_candidates else "none"
        return ("Claude usage cache: FILE NOT FOUND - no usage cache was found "
                "(tried: %s). %s The file appears when Claude Code first fetches "
                "usage: open /usage in Claude Code once. %s"
                % (tried, _UNAFFECTED, _SILENCE))

    def _too_large_message(self, path, size):
        megabytes = size // (1024 * 1024)
        return ("Claude usage cache: FILE TOO LARGE - the file is %d MB, larger than "
                "the %d MB safety limit in %s, so it was not read. %s Reduce the "
                "file (its project history grows with use) or %s"
                % (megabytes, self._max_bytes // (1024 * 1024), path, _UNAFFECTED,
                   _SILENCE))

    def _not_a_file_message(self, path):
        return ("Claude usage cache: NOT A FILE - %s is not a regular file, so it "
                "was not read. %s Point the reader at Claude Code's state file in "
                "the admin page, or %s" % (path, _UNAFFECTED, _SILENCE))

    def _account_message(self, path):
        return ("Claude usage cache: ACCOUNT CHANGED - the Claude account in %s "
                "changed since the daemon started, so its readings are hidden. %s "
                "Restart the daemon or re-save this source to accept the new "
                "account." % (path, _UNAFFECTED))

    def _persistent_message(self, reason, path, kind):
        if reason == REASON_DENIED:
            return ("Claude usage cache: ACCESS DENIED reading %s (%s). The daemon "
                    "user cannot read the file. %s Run the daemon as the account "
                    "that owns the file, or %s" % (path, kind, _UNAFFECTED, _SILENCE))
        return ("Claude usage cache: FORMAT CHANGED - %s could not be parsed (%s) "
                "after %d attempts. %s If Claude Code was updated this reader "
                "needs updating: %s" % (path, kind, TRANSIENT_LIMIT + 1,
                                        _UNAFFECTED, _SILENCE))

    def _fresh_message(self, now, fetched):
        """A note when the reading itself is old; the numbers are still used."""
        if now.timestamp() - fetched > OLD_FETCH_SECONDS:
            days = int((now.timestamp() - fetched) // 86400)
            return ("the usage cache reading is %d days old; Claude Code refreshes "
                    "it when /usage is opened" % days)
        return None


def _reject_constant(name):
    """Refuse NaN and Infinity: they are not JSON and not a quota reading."""
    raise ValueError("non-finite number: " + name)


def _local_time(value):
    """The local time of an ISO timestamp, for a log line."""
    if not value:
        return "unknown"
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return "unknown"
    return moment.astimezone().strftime("%H:%M")


def main(argv=None):
    """Print what the usage cache holds. Exit code 0 when a reading is there."""
    parser = argparse.ArgumentParser(
        prog="python -m %s" % (getattr(__spec__, "name", None) or "claude_usage_cache"),
        description="Read Claude Code's local usage cache and print what it holds.")
    parser.add_argument("--path", help="the usage cache file to read")
    arguments = parser.parse_args(argv)
    if arguments.path:
        candidates = [arguments.path]
    else:
        candidates = candidate_paths(os.path.join(os.path.expanduser("~"),
                                                  ".claude", "settings.json"))
    reader = UsageCacheReader(candidates)
    result = reader.refresh()
    if not result["available"]:
        print(result["message"] or "Claude usage cache: UNAVAILABLE")
        return 1
    print("Claude usage cache: %s" % result["path"])
    print("Fetched: %s" % result["fetched_at"])
    for window in result["windows"]:
        percent = window["used_percent"]
        value = "not reported" if percent is None else "%g%%" % percent
        print("%s: %s (resets %s)" % (window["label"], value,
                                      window["resets_at"] or "unknown"))
    credit = result["credit"]
    if credit:
        print("Credit: $%g of $%g (expires %s)"
              % (credit["limit"] - credit["used"], credit["limit"],
                 credit["resets_at"]))
    if result["message"]:
        print(result["message"])
    return 0


if __name__ == "__main__":  # pragma: no cover - the script entry point
    sys.exit(main())
