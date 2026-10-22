"""Claude Code status-line collector: one file, stdlib only, Python 3.8+.

Runs on any machine that uses Claude Code and posts quota observations to a
Presto AI Dash daemon's ingest route. It is deliberately self-contained: no
package imports, so a remote computer needs only this file. The daemon's local
status-line helper reuses its normalization so both transports share one copy
of the Claude parsing and evidence rules.

Usage::

    python3 claude_statusline.py --url http://server:8787 \
        --token-file ~/.config/presto-ai-dash/claude.token \
        --host laptop -- <previous status line command, optional>

It reads status-line JSON from stdin (at most 64 KiB), builds the observation
envelope, POSTs it with a short timeout, chains the previous command given
after ``--`` by handing it the same stdin, and always exits 0: a status line
must never stall or break Claude Code. A client-side throttle keeps cached
redraws and unreachable servers from producing traffic.
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
import urllib.request

WINDOWS = (("five_hour", "5h", 300), ("seven_day", "Weekly", 10080))
MAX_INPUT = 65536
MAX_HOST_CHARS = 32
COLLECTOR_NAME = "claude-statusline"
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
    args = [python, script, "--url", url, "--token-file", token_file, "--host", host]
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
    args = [python, script, "--url", url, "--token-file", token_file, "--host", host]
    if previous:
        args += ["--"] + list(previous)
    # Encoded PowerShell avoids shell interpolation by either Git Bash or PS.
    quoted = " ".join("'" + powershell_quote(value.replace("\\", "/")) + "'" for value in args)
    script_text = ("$ProgressPreference = 'SilentlyContinue'; "
                   "$OutputEncoding = [Console]::InputEncoding = [Text.UTF8Encoding]::new($false); "
                   "$prestoInput = [Console]::In.ReadToEnd(); $prestoInput | & " + quoted)
    return "powershell -NoProfile -NonInteractive -EncodedCommand " + \
        base64.b64encode(script_text.encode("utf-16-le")).decode()


def main(argv=None, state_path=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--token-file", required=True)
    parser.add_argument("--host", default=socket.gethostname())
    parser.add_argument("previous", nargs=argparse.REMAINDER,
                        help="the previous status line command, after --")
    args = parser.parse_args(argv)
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


if __name__ == "__main__":
    raise SystemExit(main())
