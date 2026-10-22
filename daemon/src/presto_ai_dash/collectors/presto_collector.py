"""Presto AI Dash collector: one file, stdlib only, Python 3.8+.

Runs on a machine other than the daemon's and posts observations to the
daemon's ingest route. It is deliberately self-contained: no package imports,
so a remote computer needs only this file. One file serves every service that
can report remotely; each service is a mode, named on the command line. The
daemon's local Claude export reuses the Claude parsing below, so both
transports share one copy of the evidence rules.

Modes:

    claude-statusline   Claude Code's status line runs it after each response
    watch               reads on a timer what has no status line: the VS Code
                        panel's activity and usage cache, and Codex's limits

Usage::

    python3 presto_collector.py claude-statusline \
        --url http://server:8787/api/v1/ingest \
        --token-file ~/.config/presto-ai-dash/claude.token \
        --host laptop -- <previous status line command, optional>

    python3 presto_collector.py watch            # reads ~/.presto-ai-dash/collector.json
    python3 presto_collector.py watch --once     # one round, saying what it found

--url is the daemon's full ingest address, path included: the collector posts
to exactly what it is given.

In claude-statusline mode it reads status-line JSON from stdin (at most
64 KiB), builds the observation envelope, POSTs it with a short timeout,
chains the previous command given after ``--`` by handing it the same stdin,
and always exits 0: a status line must never stall or break Claude Code. A
client-side throttle keeps cached redraws and unreachable servers from
producing traffic.

The watch mode reads its settings from a folder of its own,
``~/.presto-ai-dash/collector.json``, which names the daemon's ingest address
and each account: its service, its token file and which readers to run. The
readers are the daemon's own modules, embedded below word for word between two
marker lines (tools/build_collector.py refreshes them), so this one file is all
a remote computer needs and the daemon and the collector read Claude's and
Codex's files the same way.

COLLECTOR_VERSION is the version of this file as a whole. Change the file and
it must change; tests/test_collector_version.py fails until it does.
"""

import argparse
import base64
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import shlex
import socket
import subprocess
import sys
import time
import types
import urllib.error
import urllib.request

WINDOWS = (("five_hour", "5h", 300), ("seven_day", "Weekly", 10080))
MAX_INPUT = 65536
MAX_HOST_CHARS = 32
COLLECTOR_NAME = "claude-statusline"
# Every mode this file can run, by the name used on the command line and as the
# envelope's collector name. A new service is a new entry here.
MODES = (COLLECTOR_NAME, "watch")
COLLECTOR_VERSION = "1"
SCHEMA_NAME = "presto-ai-dash.observation/1"
WINDOW_FIELDS = ("id", "label", "period_minutes", "used_percent", "resets_at", "state")
POST_TIMEOUT_SECONDS = 1.5
CHAIN_TIMEOUT_SECONDS = 2
HEARTBEAT_SECONDS = 60
DUPLICATE_SECONDS = 60
BACKOFF_SECONDS = 300


def _iso(moment):
    if moment is None:
        moment = datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).isoformat()


def _host(label):
    """A display label: truncated and made ASCII so the daemon never rejects it."""
    cleaned = label[:MAX_HOST_CHARS].encode("ascii", "ignore").decode("ascii")
    return cleaned or "collector"


def _progress_hash(data):
    """Hash API-response progress markers; timers and prompt ids never count as work."""
    markers = []
    for section, keys in (("cost", ("total_api_duration_ms",)),
                          ("context_window", ("total_input_tokens", "total_output_tokens"))):
        value = data.get(section)
        if isinstance(value, dict):
            for key in keys:
                marker = value.get(key)
                if type(marker) in (int, float) and math.isfinite(marker) and marker >= 0:
                    markers.append([section + "." + key, marker])
    if not markers:
        return None
    return hashlib.sha256(json.dumps(markers, sort_keys=True).encode("utf-8")).hexdigest()


def normalize(data):
    """Extract quota windows and evidence fingerprints from one status-line payload.

    Returns ``(windows, evidence, progress, provider_version)``, or ``None``
    before Claude's first normal response (no ``rate_limits`` yet). Windows are
    envelope-shaped: no ``observed_at``, because the daemon owns time. Evidence
    hashes keep today's "a cached redraw is not new data" rule: they cover only
    the readings, never wall-clock timers, prompt ids or session identity.
    Raises ValueError on a payload that claims quota but cannot be trusted.
    """
    if not isinstance(data, dict):
        raise ValueError("invalid status-line input")
    limits = data.get("rate_limits")
    if limits is None:
        return None  # Before a normal response: nothing measured yet.
    if not isinstance(limits, dict):
        raise ValueError("invalid quota fields")
    progress = _progress_hash(data)
    windows, evidence = [], {}
    for key, label, minutes in WINDOWS:
        value = limits.get(key)
        if value is None:
            window = {"id": key, "label": label, "period_minutes": minutes,
                      "used_percent": None, "resets_at": None, "state": "unavailable"}
        else:
            if not isinstance(value, dict):
                raise ValueError("invalid quota window")
            percent, reset = value.get("used_percentage"), value.get("resets_at")
            if percent is None or type(reset) not in (int, float) or not math.isfinite(reset):
                raise ValueError("incomplete quota window")
            if type(percent) not in (int, float) or not math.isfinite(percent) or percent < 0:
                raise ValueError("invalid quota percentage")
            try:
                resets_at = datetime.fromtimestamp(reset, timezone.utc).isoformat()
            except (OverflowError, OSError, ValueError) as exc:
                # A finite number can still be no date: 1e20, or year 3000+ on Windows.
                raise ValueError("invalid quota reset time") from exc
            window = {"id": key, "label": label, "period_minutes": minutes,
                      "used_percent": percent,
                      "resets_at": resets_at,
                      "state": "available"}
        windows.append(window)
        signal = [window["state"], window["used_percent"], window["resets_at"],
                  progress if window["state"] == "available" else None]
        evidence[key] = hashlib.sha256(json.dumps(signal, sort_keys=True).encode("utf-8")).hexdigest()
    version = data.get("version")
    if not (isinstance(version, str) and len(version) <= 64 and
            all(char.isdigit() or char in ".-abcdefghijklmnopqrstuvwxyz" for char in version)):
        version = None
    return windows, evidence, progress, version


def build_envelope(data, host, client_time=None):
    """Wrap one status-line payload in the ingest envelope, or None before the first response."""
    normalized = normalize(data)
    if normalized is None:
        return None
    windows, evidence, progress, version = normalized
    envelope = {
        "schema": SCHEMA_NAME,
        "collector": {"name": COLLECTOR_NAME, "version": COLLECTOR_VERSION, "host": _host(host)},
        "client_time": _iso(client_time),
        "windows": windows,
        "evidence": evidence,
        "progress": progress,
    }
    if version is not None:
        envelope["provider_version"] = version
    return envelope


def heartbeat_envelope(host, client_time=None):
    """The envelope sent when the collector ran but had no quota to report."""
    return {"schema": SCHEMA_NAME,
            "collector": {"name": COLLECTOR_NAME, "version": COLLECTOR_VERSION,
                          "host": _host(host)},
            "client_time": _iso(client_time)}


def _digest(envelope):
    """Fingerprint an envelope ignoring client_time, which changes every run."""
    signal = {key: value for key, value in envelope.items() if key != "client_time"}
    return hashlib.sha256(json.dumps(signal, sort_keys=True).encode("utf-8")).hexdigest()


def should_post(envelope, state, now):
    """Apply the client throttle: heartbeats, duplicates and post-failure backoff."""
    if now < state.get("backoff_until", 0):
        return False
    if "windows" not in envelope:
        return now - state.get("heartbeat_at", 0) >= HEARTBEAT_SECONDS
    return not (_digest(envelope) == state.get("digest") and
                now - state.get("sent_at", 0) < DUPLICATE_SECONDS)


def state_after(envelope, state, now):
    """Record a successful post in the throttle state."""
    result = dict(state)
    if "windows" not in envelope:
        result["heartbeat_at"] = now
    else:
        result["digest"] = _digest(envelope)
        result["sent_at"] = now
    return result


def _state_directory():
    """The per-user state directory, or None when it cannot be created.

    Throttle state lives under the user's own profile, never in a shared
    temp directory another account could pre-create.
    """
    if os.name == "nt":
        root = os.environ.get("LOCALAPPDATA")
    else:
        root = os.environ.get("XDG_STATE_HOME")
        if not root:
            home = os.path.expanduser("~")
            root = os.path.join(home, ".local", "state") if home not in ("", "~") else None
    if not root:
        return None
    directory = os.path.join(root, "presto-ai-dash")
    try:
        os.makedirs(directory, mode=0o700, exist_ok=True)
        if os.name != "nt":
            os.chmod(directory, 0o700)
    except OSError:
        return None
    return directory


def _state_path(url, host):
    """The throttle file for one daemon, or None when there is no safe place for it."""
    directory = _state_directory()
    if directory is None:
        return None
    name = hashlib.sha256((url + "\0" + host).encode("utf-8")).hexdigest()[:16]
    return os.path.join(directory, "collector-%s.json" % name)


def _open_state(path, flags):
    """Open a state file, refusing a symlink somebody else planted."""
    if os.path.islink(path):
        raise OSError("state path is a symlink")
    return os.open(path, flags | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0), 0o600)


def _load_state(path):
    if not path:
        return {}
    try:
        with os.fdopen(_open_state(path, os.O_RDONLY), "r", encoding="utf-8") as handle:
            state = json.load(handle)
        return state if isinstance(state, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_state(path, state):
    if not path:
        return
    try:
        with os.fdopen(_open_state(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC),
                        "w", encoding="utf-8") as handle:
            json.dump(state, handle)
    except OSError:
        pass  # Throttling is best-effort; a hostile or full disk must not break Claude.


def _read_token(path):
    try:
        return Path(path).expanduser().read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return ""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect())


def _post(url, token, envelope):
    """POST one envelope. Raises on any failure; the caller backs off.

    A redirect is a failure: following one would copy the bearer token to
    whatever host answered, so every 30x is refused outright.
    """
    request = urllib.request.Request(
        url, data=json.dumps(envelope).encode("utf-8"),
        headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
    with _OPENER.open(request, timeout=POST_TIMEOUT_SECONDS) as response:
        response.read()


def _chain(command, raw):
    """Run the user's previous status-line command on the same stdin."""
    try:
        result = subprocess.run(
            list(command), input=raw, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=CHAIN_TIMEOUT_SECONDS,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        return result.stdout.decode("utf-8", errors="replace")
    except (OSError, subprocess.TimeoutExpired):
        return ""


def _status_text(envelope):
    windows = envelope.get("windows")
    if not windows:
        return "Claude quota export waiting for a normal response\n"
    return "Claude " + " / ".join(
        window["label"] + " " + ("%g%%" % window["used_percent"]
                                 if window["used_percent"] is not None else "unavailable")
        for window in windows) + "\n"


def posix_snippet(script, url, token_file, host, previous=(), python="python3"):
    """The settings.json status line for a POSIX machine, safely quoted."""
    args = [python, script, COLLECTOR_NAME, "--url", url, "--token-file", token_file,
            "--host", host]
    if previous:
        args += ["--"] + list(previous)
    return " ".join(shlex.quote(value) for value in args)


# PowerShell ends a single-quoted string at any of these, not only at the
# ASCII apostrophe: the typographic single quotes count as quotes too.
_POWERSHELL_QUOTES = "'\u2018\u2019\u201a\u201b"


def powershell_quote(value):
    """Text as the inside of a PowerShell single-quoted string, every quote doubled."""
    for quote in _POWERSHELL_QUOTES:
        value = value.replace(quote, quote * 2)
    return value


def powershell_snippet(script, url, token_file, host, previous=(), python="python"):
    """The settings.json status line for Windows: encoded PowerShell, forward slashes."""
    args = [python, script, COLLECTOR_NAME, "--url", url, "--token-file", token_file,
            "--host", host]
    if previous:
        args += ["--"] + list(previous)
    # Encoded PowerShell avoids shell interpolation by either Git Bash or PS.
    quoted = " ".join("'" + powershell_quote(value.replace("\\", "/")) + "'" for value in args)
    script_text = ("$ProgressPreference = 'SilentlyContinue'; "
                   "$OutputEncoding = [Console]::InputEncoding = [Text.UTF8Encoding]::new($false); "
                   "$prestoInput = [Console]::In.ReadToEnd(); $prestoInput | & " + quoted)
    return "powershell -NoProfile -NonInteractive -EncodedCommand " + \
        base64.b64encode(script_text.encode("utf-16-le")).decode()


# ---------------------------------------------------------------------------
# The watch mode: readers that run on a timer, for what has no status line.
#
# Claude Code's VS Code panel never runs the status line, and Codex has no hook
# at all, so neither can call the collector when something happens. `watch`
# looks every so often instead: it reads what the daemon would read on its own
# machine (Claude's activity and usage cache, Codex's rate limits) and pushes
# it to the daemon's ingest route, one account at a time with that account's
# own token. What it reads, and where, is named in a small JSON file in a
# folder of its own (~/.presto-ai-dash/collector.json), so nothing is in a
# command line, a service definition or an environment variable.
# ---------------------------------------------------------------------------

WATCH_MODE = "watch"
CONFIG_FOLDER = ".presto-ai-dash"
CONFIG_NAME = "collector.json"
SERVICES = ("claude", "codex")
# The name each service's envelopes carry. The daemon only checks it is printable.
SERVICE_NAMES = {"claude": "claude-watch", "codex": "codex-watch"}
DEFAULT_INTERVAL_SECONDS = 60
MIN_INTERVAL_SECONDS = 30
MAX_INTERVAL_SECONDS = 3600
WATCH_POST_TIMEOUT_SECONDS = 10
MAX_BACKOFF_SECONDS = 300
MAX_PUSH_WINDOWS = 24
MAX_CONFIG_BYTES = 65536
MAX_ACCOUNTS = 8
MAX_REASON_CHARS = 120
MAX_CACHE_AGE_SECONDS = 7 * 24 * 3600
ACTIVITY_FIELDS = ("available", "complete", "last_request_at", "last_model", "last_entrypoint",
                   "requests_5h", "tokens_5h", "tokens_7d")
CONFIG_KEYS = frozenset(("version", "url", "host", "interval_seconds", "accounts"))
ACCOUNT_KEYS = {
    "claude": frozenset(("service", "name", "token_file", "settings_path", "activity",
                         "usage_cache", "usage_cache_path")),
    "codex": frozenset(("service", "name", "token_file", "executable", "profile_home", "timeout")),
}


class ConfigError(Exception):
    """The configuration file cannot be used; the message says why and where."""


def default_config_path():
    return os.path.join(os.path.expanduser("~"), CONFIG_FOLDER, CONFIG_NAME)


def _option(entry, key, kind, default, where):
    value = entry.get(key, default)
    if kind is bool:
        ok = type(value) is bool
    elif kind is int:
        ok = type(value) is int
    else:
        ok = isinstance(value, str) and len(value) <= 1024
    if not ok:
        raise ConfigError("%s: %s must be %s" % (where, key, {bool: "true or false",
                                                              int: "a whole number",
                                                              str: "text"}[kind]))
    return value


def _path(value, folder):
    """A path from the file: ~ expanded, and a relative one taken from the config folder."""
    if not value:
        return ""
    value = os.path.expanduser(value)
    return value if os.path.isabs(value) else os.path.join(folder, value)


def load_config(path):
    """The validated configuration, or ConfigError. Unknown keys are errors, not ignored:
    a misspelt ``usage_cache`` must not silently turn a reader off or on."""
    try:
        with open(path, "rb") as handle:
            raw = handle.read(MAX_CONFIG_BYTES + 1)
    except FileNotFoundError:
        raise ConfigError("there is no configuration file at %s" % path) from None
    except OSError as error:
        raise ConfigError("cannot read %s (%s)" % (path, type(error).__name__)) from None
    if len(raw) > MAX_CONFIG_BYTES:
        raise ConfigError("%s is larger than %d bytes" % (path, MAX_CONFIG_BYTES))
    try:
        data = json.loads(raw.decode("utf-8-sig"))
    except (ValueError, UnicodeError, RecursionError):
        raise ConfigError("%s is not valid JSON" % path) from None
    if not isinstance(data, dict):
        raise ConfigError("%s must hold one JSON object" % path)
    unknown = set(data) - CONFIG_KEYS
    if unknown:
        raise ConfigError("unknown setting in %s: %s" % (path, ", ".join(sorted(map(str, unknown)))))
    if data.get("version", 1) != 1:
        raise ConfigError("%s: version must be 1" % path)
    url = data.get("url")
    if (not isinstance(url, str) or not url.startswith(("http://", "https://"))
            or len(url) > 300 or not url.isascii()):
        raise ConfigError("%s: url must be the daemon's ingest address, like "
                          "http://server:8787/api/v1/ingest" % path)
    folder = os.path.dirname(os.path.abspath(path))
    host = _host(_option(data, "host", str, "", path) or socket.gethostname())
    interval = _option(data, "interval_seconds", int, DEFAULT_INTERVAL_SECONDS, path)
    if not MIN_INTERVAL_SECONDS <= interval <= MAX_INTERVAL_SECONDS:
        raise ConfigError("%s: interval_seconds must be %d to %d"
                          % (path, MIN_INTERVAL_SECONDS, MAX_INTERVAL_SECONDS))
    entries = data.get("accounts")
    if not isinstance(entries, list) or not 1 <= len(entries) <= MAX_ACCOUNTS:
        raise ConfigError("%s: accounts must list 1 to %d accounts" % (path, MAX_ACCOUNTS))
    accounts, seen = [], set()
    for index, entry in enumerate(entries, 1):
        where = "%s: account %d" % (path, index)
        if not isinstance(entry, dict) or entry.get("service") not in SERVICES:
            raise ConfigError("%s: service must be one of %s" % (where, ", ".join(SERVICES)))
        service = entry["service"]
        unknown = set(entry) - ACCOUNT_KEYS[service]
        if unknown:
            raise ConfigError("%s: unknown setting %s for %s"
                              % (where, ", ".join(sorted(map(str, unknown))), service))
        token_file = _option(entry, "token_file", str, "", where)
        if not token_file:
            raise ConfigError("%s: token_file is required" % where)
        account = {"service": service, "token_file": _path(token_file, folder),
                   "name": _host(_option(entry, "name", str, "", where) or service)}
        if service == "claude":
            settings = _option(entry, "settings_path", str, "", where)
            account.update(
                settings_path=_path(settings or os.path.join("~", ".claude", "settings.json"),
                                    folder),
                activity=_option(entry, "activity", bool, True, where),
                usage_cache=_option(entry, "usage_cache", bool, True, where),
                usage_cache_path=_path(_option(entry, "usage_cache_path", str, "", where), folder))
            if not (account["activity"] or account["usage_cache"]):
                raise ConfigError("%s: a Claude account needs activity or usage_cache; the "
                                  "terminal status line has its own setup" % where)
        else:
            timeout = _option(entry, "timeout", int, 15, where)
            if not 1 <= timeout <= 60:
                raise ConfigError("%s: timeout must be 1 to 60 seconds" % where)
            account.update(executable=_option(entry, "executable", str, "", where),
                           profile_home=_path(_option(entry, "profile_home", str, "", where),
                                              folder), timeout=timeout)
        key = (service, account["token_file"])
        if key in seen:
            raise ConfigError("%s: two accounts use the token file %s" % (where, token_file))
        seen.add(key)
        accounts.append(account)
    return {"url": url, "host": host, "interval": interval, "accounts": accounts}


_EMBEDDED_LOADED = {}
# An embedded module that imports another loads that one first.
_EMBEDDED_NEEDS = {"codex_rpc": ("child_env",)}


def _embedded(name):
    """One of the daemon's reader modules, loaded from the copy held in this file."""
    module = _EMBEDDED_LOADED.get(name)
    if module is None:
        for needed in _EMBEDDED_NEEDS.get(name, ()):
            _embedded(needed)
        package = sys.modules.get("presto_collector_embedded")
        if package is None:
            package = types.ModuleType("presto_collector_embedded")
            package.__path__ = []
            sys.modules["presto_collector_embedded"] = package
        qualified = "presto_collector_embedded." + name
        module = types.ModuleType(qualified)
        module.__file__ = "<presto_collector embedded %s>" % name
        module.__package__ = "presto_collector_embedded"
        sys.modules[qualified] = module
        setattr(package, name, module)
        exec(compile(_EMBEDDED[name], module.__file__, "exec"), module.__dict__)
        _EMBEDDED_LOADED[name] = module
    return module


def _ascii(text, limit=MAX_REASON_CHARS):
    """Printable ASCII, one line, bounded."""
    cleaned = "".join(char if " " <= char <= "~" else "?" for char in str(text))
    return cleaned[:limit]


def activity_reading(summary):
    """The activity summary as pushed: the reader's own fields and nothing else."""
    result = {key: summary.get(key) for key in ACTIVITY_FIELDS}
    if not summary.get("available") and summary.get("reason"):
        result["reason"] = _ascii(summary["reason"])
    return result


def usage_cache_reading(result, now):
    """The usage-cache reading as pushed: numbers and times, never a path or a message.

    A reading older than the daemon will accept is sent as unavailable, saying
    why, rather than as a quota somebody might believe.
    """
    if result.get("available"):
        fetched = result.get("fetched_at")
        try:
            moment = datetime.fromisoformat(str(fetched).replace("Z", "+00:00"))
            age = now - moment.timestamp()
        except (ValueError, OverflowError, OSError):
            age = None
        if age is None or age > MAX_CACHE_AGE_SECONDS:
            return {"available": False, "reason": "the usage cache reading is over 7 days old",
                    "fetched_at": None, "windows": [], "credit": None, "account_changed": False,
                    "refreshes": [], "since": None}
    windows = [{key: window.get(key) for key in WINDOW_FIELDS}
               for window in result.get("windows") or []] if result.get("available") else []
    credit = result.get("credit")
    if isinstance(credit, dict):
        credit = {key: credit.get(key) for key in ("limit", "used", "currency", "resets_at")}
    else:
        credit = None
    reading = {"available": bool(result.get("available")),
               "reason": None if result.get("available") else _ascii(result.get("reason") or
                                                                     "unavailable"),
               "fetched_at": result.get("fetched_at") if result.get("available") else None,
               "windows": windows, "credit": credit if result.get("available") else None,
               "account_changed": bool(result.get("account_changed")),
               "refreshes": [str(item) for item in (result.get("refreshes") or [])][-50:],
               "since": result.get("since")}
    return reading


def _evidence(window, client_time):
    """A fresh Codex reading is new evidence every time, so each push refreshes its age."""
    return hashlib.sha256(json.dumps([window, client_time], sort_keys=True)
                          .encode("utf-8")).hexdigest()


class Problem(Exception):
    """Something to tell the user, in one plain sentence, without a stack trace."""


class Account(object):
    """One configured account: its readers (kept between rounds) and its send state."""

    def __init__(self, settings):
        self.settings = settings
        self.service = settings["service"]
        self.label = "%s (%s)" % (self.service, settings["name"])
        self.activity_reader = None
        self.cache_reader = None
        self.failures = 0
        self.skip_until = 0
        self.last_message = None

    def _claude_pieces(self, now):
        pieces, notes = {}, []
        if self.settings["activity"]:
            if self.activity_reader is None:
                folder = os.path.join(os.path.dirname(self.settings["settings_path"]), "projects")
                self.activity_reader = _embedded("claude_activity").ActivityReader(folder)
            summary = self.activity_reader.refresh()
            pieces["activity"] = activity_reading(summary)
            notes.append("activity " + ("ok, %d requests in 5 h" % summary["requests_5h"]
                                        if summary["available"] else
                                        "unavailable (%s)" % _ascii(summary.get("reason"))))
        if self.settings["usage_cache"]:
            module = _embedded("claude_usage_cache")
            if self.cache_reader is None:
                override = self.settings["usage_cache_path"] or None
                self.cache_reader = module.UsageCacheReader(
                    module.candidate_paths(self.settings["settings_path"], override))
            result = self.cache_reader.refresh()
            pieces["usage_cache"] = usage_cache_reading(result, now)
            reading = pieces["usage_cache"]
            notes.append("usage cache " + ("ok, fetched %s" % reading["fetched_at"]
                                           if reading["available"] else
                                           "unavailable (%s)" % reading["reason"]))
        return pieces, notes

    def _codex_pieces(self, host, now):
        rpc = _embedded("codex_rpc")
        report = _embedded("codex_report")
        try:
            executable = rpc.find_executable(self.settings["executable"])
            data = rpc.read_subscription(executable, self.settings["profile_home"],
                                         self.settings["timeout"])
            windows, _, reset_credits, credits = report.rate_limit_windows(data)
            if len(windows) > MAX_PUSH_WINDOWS:
                raise Problem("Codex reported more limits than a report can carry (%d)"
                              % len(windows))
        except rpc.CodexReportingError as error:
            raise Problem("Codex could not be read: %s" % _ascii(error)) from None
        except (ValueError, TypeError, OverflowError, OSError):
            raise Problem("Codex answered in a form this collector does not understand; "
                          "update Codex or the collector") from None
        client_time = _iso(None)
        pieces = {"windows": windows,
                  "evidence": {window["id"]: _evidence(window, client_time) for window in windows},
                  "reset_credits": reset_credits, "credits": credits}
        shown = [window for window in windows if window["used_percent"] is not None]
        return pieces, ["rate limits ok, " + (", ".join("%s %g%%" % (window["label"],
                                                                      window["used_percent"])
                                                        for window in shown) or "no window reported")]

    def envelope(self, host, now):
        """What to send this round, plain notes on what was read, and a warning or None.

        A reading that could not be made still produces an envelope - a
        heartbeat - so the daemon sees the collector is alive and the account's
        old quota ages instead of looking current.
        """
        notes, warning = [], None
        envelope = {"schema": SCHEMA_NAME,
                    "collector": {"name": SERVICE_NAMES[self.service],
                                  "version": COLLECTOR_VERSION, "host": host},
                    "client_time": _iso(None)}
        try:
            if self.service == "claude":
                pieces, notes = self._claude_pieces(now)
            else:
                pieces, notes = self._codex_pieces(host, now)
            envelope.update(pieces)
        except Problem as problem:
            warning = str(problem)
        return envelope, notes, warning


def _read_token_checked(path):
    """The token in ``path``, or a Problem that says what to do about it."""
    token = _read_token(path)
    if not token:
        raise Problem("the token file %s is missing or empty; create a remote token for this "
                      "account in the daemon's admin page and save it there" % path)
    if os.name != "nt":
        try:
            if os.stat(path).st_mode & 0o077:
                sys.stderr.write("note: %s can be read by other users; run chmod 600 on it\n"
                                 % path)
        except OSError:
            pass
    return token


def send(url, token, envelope, opener=None):
    """POST one envelope. Raises Problem with the reason in the user's words."""
    request = urllib.request.Request(
        url, data=json.dumps(envelope).encode("utf-8"),
        headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
    try:
        with (opener or _OPENER).open(request, timeout=WATCH_POST_TIMEOUT_SECONDS) as response:
            response.read(1024)
    except urllib.error.HTTPError as error:
        code = error.code
        if code == 401:
            raise Problem("the daemon refused the token (401); it was revoked or replaced: "
                          "create a new remote token for this account and save it") from None
        if code == 404:
            raise Problem("the daemon answered 404: remote reports are switched off on the "
                          "daemon (Settings, Remote reports), or the URL is not its ingest "
                          "address") from None
        if code == 400:
            try:
                detail = _ascii(json.loads(error.read(2048).decode("utf-8")).get("error", ""))
            except (ValueError, UnicodeError, AttributeError):
                detail = ""
            raise Problem("the daemon rejected the report (400)%s"
                          % (": " + detail if detail else "")) from None
        if 300 <= code < 400:
            raise Problem("the daemon redirected the request (%d); redirects are never "
                          "followed, because they would carry the token elsewhere" % code) from None
        raise Problem("the daemon answered %d" % code) from None
    except (OSError, ValueError) as error:
        raise Problem("the daemon cannot be reached (%s)" % type(error).__name__) from None


def run_round(config, accounts, now=None, out=None, opener=None, verbose=False):
    """One pass over every account. Returns how many were sent."""
    out = out or sys.stderr
    now = time.time() if now is None else now
    sent = 0

    def say(account, text):
        out.write("%s %s: %s\n" % (time.strftime("%H:%M:%S", time.localtime(now)),
                                   account.label, text))
        out.flush()

    for account in accounts:
        if now < account.skip_until:
            continue
        try:
            envelope, notes, warning = account.envelope(config["host"], now)
            token = _read_token_checked(account.settings["token_file"])
            send(config["url"], token, envelope, opener)
        except Problem as problem:
            account.failures += 1
            account.skip_until = now + min(MAX_BACKOFF_SECONDS,
                                           config["interval"] * 2 ** min(account.failures - 1, 4))
            if account.last_message != str(problem):
                account.last_message = str(problem)
                say(account, str(problem))
            continue
        except Exception as error:  # a reader that breaks must not end the watch
            account.failures += 1
            account.skip_until = now + MAX_BACKOFF_SECONDS
            message = "unexpected problem (%s); will retry" % type(error).__name__
            if account.last_message != message:
                account.last_message = message
                say(account, message)
            continue
        sent += 1
        recovered = account.failures or account.last_message
        account.failures, account.skip_until = 0, 0
        if warning:
            if account.last_message != warning:
                account.last_message = warning
                say(account, warning)
        else:
            if recovered:
                say(account, "reporting again")
            account.last_message = None
            if verbose:
                say(account, "sent: " + "; ".join(notes))
    return sent


def watch(config_path, once=False, out=None, sleep=None, opener=None, rounds=None):
    """Read and push on a timer. ``once`` makes one pass and says what it found."""
    out = out or sys.stderr
    sleep = sleep or time.sleep
    try:
        config = load_config(config_path)
    except ConfigError as error:
        out.write("presto_collector: %s\n" % error)
        return 2
    accounts = [Account(settings) for settings in config["accounts"]]
    out.write("presto_collector %s: %d account(s) to %s, every %d s\n"
              % (WATCH_MODE, len(accounts), config["url"], config["interval"]))
    out.flush()
    taken = 0
    try:
        while True:
            sent = run_round(config, accounts, out=out, opener=opener, verbose=once)
            taken += 1
            if once:
                return 0 if sent == len(accounts) else 1
            if rounds is not None and taken >= rounds:
                return 0
            sleep(config["interval"])
    except KeyboardInterrupt:
        return 0


def main(argv=None, state_path=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    # One subcommand per service. Each has its own options, so a service that
    # is polled rather than hooked can have different ones.
    modes = parser.add_subparsers(dest="mode", metavar="MODE", required=True)
    claude = modes.add_parser(COLLECTOR_NAME, help="Claude Code's status line")
    claude.add_argument("--url", required=True)
    claude.add_argument("--token-file", required=True)
    claude.add_argument("--host", default=socket.gethostname())
    claude.add_argument("previous", nargs=argparse.REMAINDER,
                        help="the previous status line command, after --")
    watcher = modes.add_parser(WATCH_MODE, help="read on a timer and push (VS Code panel, Codex)")
    watcher.add_argument("--config", default=None,
                         help="the configuration file (default: ~/.presto-ai-dash/collector.json)")
    watcher.add_argument("--once", action="store_true",
                         help="make one round, say what was read and sent, and exit")
    args = parser.parse_args(argv)
    if args.mode == WATCH_MODE:
        return watch(args.config or default_config_path(), once=args.once)
    previous = list(args.previous)
    if previous and previous[0] == "--":  # argparse keeps the separator in REMAINDER.
        previous.pop(0)
    raw = sys.stdin.buffer.read(MAX_INPUT + 1)
    envelope = None
    if len(raw) <= MAX_INPUT:
        try:
            envelope = build_envelope(json.loads(raw.decode("utf-8-sig")), args.host)
        except (ValueError, TypeError, UnicodeError, RecursionError):
            envelope = None
    if envelope is None:
        envelope = heartbeat_envelope(args.host)
    state_path = state_path or _state_path(args.url, args.host)
    state = _load_state(state_path)
    now = time.time()
    if should_post(envelope, state, now):
        token = _read_token(args.token_file)
        if token:
            try:
                _post(args.url, token, envelope)
                state = state_after(envelope, state, now)
            except Exception:  # The status line never fails on transport errors.
                state = dict(state, backoff_until=now + BACKOFF_SECONDS)
        _save_state(state_path, state)
    output = _chain(previous, raw) if previous else ""
    if not output:
        output = _status_text(envelope)
    sys.stdout.write(output)
    return 0


# ==== BEGIN embedded readers (generated by tools/build_collector.py; do not edit) ====
_EMBEDDED = {
    'child_env': r'''"""The environment a helper program, such as Codex, is started with.

The daemon takes its secrets from environment variables: its own tokens and
the master key (``DASH_*``) and a provider key named by ``api_key_env``. A
helper it starts has no use for any of them, and anything the helper starts
in turn would inherit them. So they are left out. Everything else is passed on
unchanged, because a helper may need ``PATH``, ``HOME``, proxy settings and the
rest to run at all.
"""

import os

PREFIX = "DASH_"

# Provider key variables, by upper-cased name. LiteLLM's default is always
# hidden; load_config adds the one a configuration names.
_hidden = {"LITELLM_API_KEY"}


def hide(name):
    """Keep one more variable out of every helper's environment."""
    if isinstance(name, str) and name:
        _hidden.add(name.upper())


def is_hidden(name):
    upper = str(name).upper()
    return upper.startswith(PREFIX) or upper in _hidden


def child_environment():
    """A copy of the environment without the daemon's own secrets."""
    return {name: value for name, value in os.environ.items() if not is_hidden(name)}
''',
    'claude_activity': r'''"""Claude Code local session-log activity reader: stdlib only, Python 3.8+.

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
''',
    'claude_usage_cache': r'''"""Claude Code usage cache reader: stdlib only, Python 3.8+.

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
# The distinct fetch times kept in memory. Nothing is written to a file or a
# database: the record starts empty on every daemon start.
MAX_REFRESH_RECORDS = 50

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

# The start of the real checkbox label in the admin page. It is quoted in the
# log messages, so a user can find the box the message names.
SILENCE_PHRASE = "Read Claude Code's local usage cache"

_UNAFFECTED = ("Quota from the usage cache is unavailable; terminal-export "
               "readings are unaffected.")
_SILENCE = ("Untick '%s' in the admin page to silence this." % SILENCE_PHRASE)


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


class FileSwapped(OSError):
    """The file opened is not the one that was checked a moment before."""


def _open_the_file_that_was_checked(path, checked):
    """Open a regular file for reading, and only the one ``checked`` described.

    Looking at a path and then opening it leaves a gap in which the file can be
    replaced by a link to somewhere else. Where the platform can, the open itself
    refuses a link (O_NOFOLLOW); everywhere, what was opened must be a regular
    file with the identity of the one that was looked at.
    """
    def opener(name, flags):
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(name, flags)
        try:
            opened = os.fstat(descriptor)
            if not stat_module.S_ISREG(opened.st_mode) or not os.path.samestat(checked, opened):
                raise FileSwapped("the file changed between the check and the open")
        except BaseException:
            os.close(descriptor)
            raise
        return descriptor

    return open(path, "rb", opener=opener)


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
        # The distinct fetch times seen, newest last, at most MAX_REFRESH_RECORDS.
        # Memory only: nothing is written to a file or a database, and a new
        # reader (a daemon restart) starts with an empty record.
        self._refreshes = []
        # When the daemon first looked at the file: the start of the watching.
        self._since = None

    # -- reading -----------------------------------------------------------

    def refresh(self, now=None):
        """Read the cache and return what it holds. Never raises."""
        now = now if now is not None else self._clock()
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        if self._since is None:
            self._since = _iso(now.timestamp())
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
            # lstat, not stat: a link is looked at, never followed.
            info = os.lstat(path)
        except FileNotFoundError:
            return self._persistent(now, REASON_MISSING, self._missing_message(), None)
        except PermissionError:
            return self._transient(now, path, "PermissionError")
        except OSError as error:
            return self._transient(now, path, type(error).__name__)
        if stat_module.S_ISLNK(info.st_mode):
            return self._persistent(now, REASON_NOT_FILE, self._symlink_message(path), path)
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
            with _open_the_file_that_was_checked(path, info) as handle:
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
        self._record_refresh(result["fetched_at"])
        result.update(self._refresh_state())
        self._stat = (info.st_size, info.st_mtime_ns)
        self._result = copy.deepcopy(result)
        self._failures = 0
        if self._recovering:
            LOGGER.info("Claude usage cache: RECOVERED - the usage cache file '%s' "
                        "is readable again (fetched %s).",
                        path, _local_time(result["fetched_at"]))
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

    def _record_refresh(self, fetched_at):
        """Remember a fetch time. A duplicate or an older time changes nothing.

        Only the fetch time is kept: no value from the file ever enters the
        record.
        """
        if not fetched_at:
            return
        if self._refreshes and fetched_at <= self._refreshes[-1]:
            return
        self._refreshes.append(fetched_at)
        if len(self._refreshes) > MAX_REFRESH_RECORDS:
            del self._refreshes[:-MAX_REFRESH_RECORDS]

    def _refresh_state(self):
        """The refresh record: the fetch times seen, and since when we watch."""
        return {"refreshes": list(self._refreshes), "since": self._since}

    def _result_dict(self, available, reason, message, path, fetched_at, windows,
                     credit, account_changed=False):
        result = {"available": available, "reason": reason, "message": message,
                  "path": path, "fetched_at": fetched_at, "windows": windows,
                  "credit": credit, "account_changed": account_changed}
        result.update(self._refresh_state())
        return result

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
                     kind, (" at '%s'" % path) if path else "")
        message = ("the usage cache could not be read this time (%s); the last known "
                   "reading is kept" % kind)
        if self._result is not None:
            result = copy.deepcopy(self._result)
            result["reason"] = REASON_TRANSIENT
            result["message"] = message
            result.update(self._refresh_state())
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
        LOGGER.info("Claude usage cache: reading the usage cache file '%s'.", path)

    # -- messages ----------------------------------------------------------

    def _format_reason(self, problem):
        if problem.reason == REASON_NO_DATA:
            return problem.reason
        return "%s: %s (%s)" % (problem.reason, problem.field, problem.found)

    def _format_message(self, path, problem):
        if problem.reason == REASON_NO_DATA:
            return ("Claude usage cache: NO QUOTA READING - %s holds no quota for "
                    "either window right now. %s Open /usage in Claude Code to "
                    "fetch usage. %s" % (path, _UNAFFECTED, _SILENCE))
        return ("Claude usage cache: FORMAT CHANGED - expected %s at %s in %s but "
                "found %s. %s If Claude Code was updated this reader needs "
                "updating. %s" % (problem.expected, problem.field, path,
                                  problem.found, _UNAFFECTED, _SILENCE))

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

    def _symlink_message(self, path):
        return ("Claude usage cache: SYMBOLIC LINK - %s is a symbolic link, which is "
                "not followed, so it was not read. %s Point the reader at the real "
                "file in the admin page, or %s" % (path, _UNAFFECTED, _SILENCE))

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
                "needs updating. %s" % (path, kind, TRANSIENT_LIMIT + 1,
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
    print("Refreshes seen: %d (since %s)"
          % (len(result.get("refreshes") or []), result.get("since") or "unknown"))
    if result["message"]:
        print(result["message"])
    return 0


if __name__ == "__main__":  # pragma: no cover - the script entry point
    sys.exit(main())
''',
    'codex_report': r'''"""What Codex's rate-limit answer says, as plain quota windows: stdlib only, Python 3.8+.

The daemon's Codex source and the one-file collector both read Codex's
``account/rateLimits/read`` answer. This module is the one copy of the rules
for turning it into quota windows, banked reset credits and a credit balance,
so a reading pushed from another machine means exactly what a local one does.

Only the quota numbers, the reset times, the count and expiry of reset credits
and the credit balance are taken. Nothing about the account is read.
"""

from datetime import datetime, timezone
import decimal
import math
import re

# Banked reset credits (Phase 9). The provider's count is authoritative; the
# rows only supply expiries. Missing or malformed data means "not reported".
MAX_RESET_COUNT = 99
MAX_RESET_EXPIRIES = 8
# A Unix timestamp outside this range is not a real expiry.
EARLIEST_EXPIRY = 1000000000  # 2001-09-09
LATEST_EXPIRY = 4102444800    # 2100-01-01
# The credit balance is a short decimal string on the detail card.
MAX_BALANCE_CHARS = 16
MAX_BUCKETS = 32
MAX_BUCKET_ID_CHARS = 128
# A window id is bucket and slot, "codex:primary"; the envelope allows 32.
MAX_WINDOW_ID_CHARS = 32


def period_label(minutes):
    if minutes == 10080:
        return "Weekly"
    if minutes == 1440:
        return "Daily"
    if minutes is None:
        return "Reported window"
    if minutes % 1440 == 0:
        return "%dd" % (minutes // 1440)
    if minutes % 60 == 0:
        return "%dh" % (minutes // 60)
    return "%dm" % minutes


def balance(value):
    """A credit balance as a plain decimal string, or None when unusable.

    The provider reports it in exponent notation ("0E-10"), which must never
    reach the screen: Decimal turns it into "0". At most 16 characters.
    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    try:
        amount = decimal.Decimal(text)
    except (decimal.InvalidOperation, ValueError, ArithmeticError):
        return None
    if not amount.is_finite():
        return None
    if amount == 0:
        return "0"
    try:
        amount = amount.normalize()
        if amount.as_tuple().exponent > 0:
            amount = amount.quantize(decimal.Decimal(1))
    except (decimal.InvalidOperation, ArithmeticError):
        return None
    result = format(amount, "f")
    if len(result) > MAX_BALANCE_CHARS:
        return None
    return result


def reset_credits(summary):
    """The banked reset credits: a count and the expiries of the available ones.

    ``available`` is the provider's own count when it is a real integer in
    range, and None otherwise: an unknown count is never shown as zero. Only
    rows whose status is exactly "available" supply an expiry.
    """
    available, expiries = None, []
    if isinstance(summary, dict):
        count = summary.get("availableCount")
        if type(count) is int and 0 <= count <= MAX_RESET_COUNT:
            available = count
        rows = summary.get("credits")
        if isinstance(rows, list):
            seen = set()
            for row in rows:
                if not isinstance(row, dict) or row.get("status") != "available":
                    continue
                expires = row.get("expiresAt")
                if type(expires) is not int:
                    continue
                if expires < EARLIEST_EXPIRY or expires > LATEST_EXPIRY:
                    continue
                moment = datetime.fromtimestamp(expires, timezone.utc).isoformat()
                if moment in seen:
                    continue
                seen.add(moment)
                expiries.append(moment)
            expiries.sort()
            expiries = expiries[:MAX_RESET_EXPIRIES]
    return {"available": available, "expires_at": expiries}


def credits(bucket):
    """The bucket's credit balance, or None when the provider reports none."""
    amount, unlimited = None, None
    if isinstance(bucket, dict):
        reported = bucket.get("credits")
        if isinstance(reported, dict):
            amount = balance(reported.get("balance"))
            if type(reported.get("unlimited")) is bool:
                unlimited = reported["unlimited"]
    return {"balance": amount, "unlimited": unlimited}


def buckets_of(data):
    """The quota buckets of an answer, and the legacy single bucket when that is all there is.

    Returns ``(buckets, legacy_bucket)``. Raises ValueError for an answer that
    holds no usable bucket.
    """
    if not isinstance(data, dict):
        raise ValueError("invalid reporting response")
    legacy_bucket = None
    if "rateLimitsByLimitId" in data and data["rateLimitsByLimitId"] is not None:
        buckets = data["rateLimitsByLimitId"]
        if not isinstance(buckets, dict) or not buckets:
            raise ValueError("no quota buckets reported")
    else:
        legacy = data.get("rateLimits")
        if not isinstance(legacy, dict):
            raise ValueError("no quota buckets reported")
        legacy_bucket = legacy
        buckets = {legacy.get("limitId") or "codex": legacy}
    if len(buckets) > MAX_BUCKETS:
        raise ValueError("too many quota buckets")
    return buckets, legacy_bucket


def credit_bucket(buckets, legacy_bucket):
    """The bucket whose credit balance is shown: ``codex``, or the legacy one."""
    chosen = buckets.get("codex") if isinstance(buckets, dict) else None
    return legacy_bucket if chosen is None else chosen


_ID_CHARS = re.compile(r"[^a-z0-9_.]")


def safe_window_id(bucket_id, slot):
    """``bucket:slot`` in the characters and length a window id may have."""
    stem = _ID_CHARS.sub("_", bucket_id.lower())[:MAX_WINDOW_ID_CHARS - len(slot) - 1]
    return stem + ":" + slot


def rate_limit_windows(data):
    """The quota windows of one answer, in the shape the ingest envelope carries.

    Returns ``(windows, limited, reset_credits, credits)``. A window has
    ``id``, ``label``, ``period_minutes``, ``used_percent``, ``resets_at`` (an
    ISO time or None) and ``state``; there is no time of observation, because
    whoever receives the reading owns the clock. Raises ValueError for an
    answer whose shape is not understood.
    """
    buckets, legacy = buckets_of(data)
    windows, limited, seen = [], False, set()
    for bucket_id, bucket in buckets.items():
        if (not isinstance(bucket_id, str) or not bucket_id
                or len(bucket_id) > MAX_BUCKET_ID_CHARS or not isinstance(bucket, dict)):
            raise ValueError("invalid quota bucket")
        prefix = (bucket_id + " ") if len(buckets) > 1 else ""
        for slot in ("primary", "secondary"):
            window_id = safe_window_id(bucket_id, slot)
            if window_id in seen:
                raise ValueError("duplicate quota window identity")
            seen.add(window_id)
            value = bucket.get(slot)
            if value is None:
                windows.append({"id": window_id, "label": _label(prefix + slot.title()),
                                "period_minutes": None, "used_percent": None,
                                "resets_at": None, "state": "unavailable"})
                continue
            if not isinstance(value, dict):
                raise ValueError("invalid quota window")
            minutes = value.get("windowDurationMins")
            if minutes is not None and (type(minutes) is not int or minutes <= 0):
                raise ValueError("invalid quota period")
            reset = value.get("resetsAt")
            if reset is not None:
                if type(reset) not in (int, float):
                    raise ValueError("invalid reset")
                reset = datetime.fromtimestamp(reset, timezone.utc).isoformat()
            percent = value.get("usedPercent")
            if percent is not None and (type(percent) not in (int, float)
                                        or not math.isfinite(percent) or percent < 0):
                raise ValueError("invalid quota percentage")
            windows.append({"id": window_id, "label": _label(prefix + period_label(minutes)),
                            "period_minutes": minutes, "used_percent": percent,
                            "resets_at": reset,
                            "state": "available" if percent is not None else "unavailable"})
            limited = limited or (percent is not None and percent >= 100)
        limited = limited or bucket.get("rateLimitReachedType") is not None
    return (windows, limited, reset_credits(data.get("rateLimitResetCredits")),
            credits(credit_bucket(buckets, legacy)))


def _label(text):
    """A short printable-ASCII window label."""
    cleaned = "".join(char for char in text if " " <= char <= "~").strip()
    return (cleaned or "Reported window")[:32]
''',
    'codex_rpc': r'''"""Bounded read-only Codex app-server session; OAuth remains CLI-owned."""

import json
import os
from pathlib import Path
import queue
import re
import shutil
import subprocess
import threading
import time

try:
    from ..child_env import child_environment
except ImportError:  # the one-file collector has no package; it loads its own copy first
    from presto_collector_embedded.child_env import child_environment


class CodexReportingError(Exception):
    """Only fixed, credential-free messages cross the source boundary."""


_EXECUTABLE_SUFFIXES = ("", ".exe", ".cmd", ".bat")


def executable_setting_problem(configured):
    """Why a configured Codex executable is refused, or ``None`` when it is fine.

    The daemon starts this program on every poll, so the setting must name
    Codex and not any program on the computer. This is not a defence against
    someone who can put a file of that name where they like; it keeps the
    admin page from being a way to run an arbitrary program, or one on a
    network share.
    """
    if not configured:
        return None
    text = str(configured).strip()
    if text.startswith(("\\\\", "//")):
        return "Codex executable must be on this computer, not on a network share"
    # Both separators on every system: a Windows-style path typed on a Linux
    # host is still a path, and is refused as one.
    name = re.split(r"[/\\]", text.rstrip("/\\"))[-1]
    if name != text and not os.path.isabs(text):
        return "Codex executable must be a full path or just the name codex"
    base, suffix = os.path.splitext(name.lower())
    if not base.startswith("codex") or suffix not in _EXECUTABLE_SUFFIXES:
        return "Codex executable must be a program named codex"
    return None


def find_executable(configured=""):
    problem = executable_setting_problem(configured)
    if problem:
        raise CodexReportingError(problem)
    if configured:
        resolved = shutil.which(configured)
        if resolved:
            return resolved
        raise CodexReportingError("Codex executable not found; update its path in source configuration")
    resolved = shutil.which("codex")
    if resolved:
        return resolved
    # VS Code's bundled binary is versioned. Resolve it anew after each update.
    candidates = list((Path.home() / ".vscode" / "extensions").glob(
        "openai.chatgpt-*/bin/*/codex.exe" if os.name == "nt" else
        "openai.chatgpt-*/bin/*/codex"))
    candidates = [path for path in candidates if path.is_file()]
    if candidates:
        return str(max(candidates, key=lambda path: path.stat().st_mtime))
    raise CodexReportingError("Install Codex or set its executable path in source configuration")


class ReportingSession:
    def __init__(self, executable, home="", timeout=15):
        env = child_environment()
        if home:
            profile = Path(home).expanduser()
            if not profile.is_absolute():
                raise CodexReportingError("Codex profile directory must be an absolute path")
            env["CODEX_HOME"] = str(profile)
        self.deadline = time.monotonic() + timeout
        self.messages = queue.Queue(maxsize=32)
        self.closed = threading.Event()
        self.sequence = 0
        try:
            self.process = subprocess.Popen(
                [executable, "app-server", "--listen", "stdio://"], env=env,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        except OSError:
            raise CodexReportingError("Unable to start Codex app-server") from None
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _read(self):
        try:
            while not self.closed.is_set():
                line = self.process.stdout.readline(1024 * 1024 + 1)
                if not line or len(line) > 1024 * 1024:
                    item = None
                else:
                    try:
                        item = json.loads(line)
                    except (ValueError, UnicodeError):
                        item = None
                # Never block shutdown if the server floods unsolicited output.
                try:
                    self.messages.put(item, timeout=0.1)
                except queue.Full:
                    self.process.terminate()
                    return
                if item is None:
                    return
        except (OSError, ValueError):
            pass

    def _send(self, message):
        try:
            self.process.stdin.write((json.dumps(message) + "\n").encode("utf-8"))
            self.process.stdin.flush()
        except (OSError, ValueError):
            raise CodexReportingError("Codex app-server disconnected") from None

    def request(self, method, params=None):
        self.sequence += 1
        request_id = self.sequence
        self._send({"id": request_id, "method": method, "params": params or {}})
        for _ in range(128):
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                message = self.messages.get(timeout=remaining)
            except queue.Empty:
                break
            if not isinstance(message, dict):
                raise CodexReportingError("Codex app-server returned an invalid response or disconnected")
            if "method" in message:
                if "id" in message:
                    self._send({"id": message["id"], "error": {
                        "code": -32601, "message": "Reporting client does not support this request"}})
                continue
            if message.get("id") != request_id:
                continue
            if "error" in message:
                raise CodexReportingError("Codex reporting request failed; check subscription login and CLI version")
            if not isinstance(message.get("result"), dict):
                raise CodexReportingError("Codex reporting schema changed; update the CLI or adapter")
            return message["result"]
        raise CodexReportingError("Codex reporting timed out")

    def close(self):
        self.closed.set()
        if self.process.poll() is None:
            self.process.terminate()
        try:
            self.process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=2)
        self.reader.join(timeout=1)
        self.process.stdin.close()
        self.process.stdout.close()


def read_subscription(executable, home="", timeout=15):
    session = ReportingSession(executable, home, timeout)
    try:
        session.request("initialize", {"clientInfo": {
            "name": "presto_ai_dash", "title": "Presto AI Dash", "version": "0.1.0"}})
        session._send({"method": "initialized"})
        account = session.request("account/read", {"refreshToken": False}).get("account")
        if not isinstance(account, dict) or account.get("type") != "chatgpt":
            raise CodexReportingError("Sign into Codex with ChatGPT in this profile; API keys do not report subscription limits")
        # Never return account identity, tokens, prompts, or raw error payloads.
        return session.request("account/rateLimits/read")
    finally:
        session.close()
''',
}
# ==== END embedded readers ====


if __name__ == "__main__":
    raise SystemExit(main())
