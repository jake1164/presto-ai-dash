"""EXPERIMENTAL Claude OAuth usage reader: stdlib only, Python 3.8+.

Off unless an account in collector.json sets ``oauth_usage``. It asks
``GET https://api.anthropic.com/api/oauth/usage`` for the 5-hour and 7-day
utilization, with the access token Claude Code keeps on this computer. The
endpoint is undocumented and whether this use is permitted is an open question
with Anthropic, so the rules here are deliberately narrow.

A request needs all of these, checked in ``evaluate`` before any credential is
opened:

* the account is not stopped (a 401 or 403 stops it until ``reenable``);
* Claude did something recently, and did it after the previous request. The
  moment comes from a session-log entry or a Claude Code hook, never from the
  time this collector looked, so a collector round can never count as activity;
* no backoff is running and the shared cooldown has passed;
* the daemon has answered, can store the reading, and has no fresh status-line
  reading that would make the request pointless.

The cooldown is stamped in a state file under a lock before the request goes
out. Sessions, watch processes and restarts therefore share one budget, and a
crash in mid-request spends the cooldown rather than repeating the request. A
state file that cannot be read or written means no request: a damaged file must
not forget a 401.

The credentials file is opened read-only. The token is never written, returned,
logged, refreshed or put in an exception: it lives in one local variable for one
request, which goes only to ENDPOINT, without a proxy, and never follows a
redirect. Nothing here sends a prompt or runs a model.
"""

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import hashlib
import json
import math
import os
import stat as stat_module
import tempfile
import time
import urllib.error
import urllib.request

if os.name == "nt":
    import msvcrt
else:
    import fcntl

from .claude_usage_cache import (FieldProblem, UsageCacheReader, WINDOWS,
                                 _open_the_file_that_was_checked, _parse_iso,
                                 _reject_constant, candidate_paths, MAX_FILE_BYTES)

ENDPOINT = "https://api.anthropic.com/api/oauth/usage"
BETA_HEADER = "oauth-2025-04-20"
REQUEST_TIMEOUT_SECONDS = 10
MAX_RESPONSE_BYTES = 256 * 1024
MAX_CREDENTIALS_BYTES = 64 * 1024
MAX_TOKEN_CHARS = 8192

DEFAULT_INTERVAL_SECONDS = 300
MIN_INTERVAL_SECONDS = 300
MAX_INTERVAL_SECONDS = 86400
# Activity older than this means Claude is idle, and requests stop.
ACTIVE_SECONDS = 300
# An activity time further ahead than this is a bad clock or a bad file, not use.
FUTURE_TOLERANCE_SECONDS = 60
# A token this close to expiry is left alone: Claude Code refreshes its own, and
# sending a dead token is the one thing that must never become a habit.
TOKEN_EXPIRY_MARGIN_SECONDS = 300

RATE_LIMIT_BACKOFF_SECONDS = 1800
MAX_RATE_LIMIT_BACKOFF_SECONDS = 6 * 3600
FAILURE_BACKOFF_SECONDS = 900
MAX_FAILURE_BACKOFF_SECONDS = 2 * 3600
MAX_RETRY_AFTER_SECONDS = 7 * 86400

STATE_VERSION = 1
MAX_HISTORY = 50
LOCK_WAIT_SECONDS = 2.0
LOCK_STALE_SECONDS = 30
REPLACE_ATTEMPTS = 5

HOOK_EVENTS = ("UserPromptSubmit", "Stop")
# Set for a Claude Code that does not use subscription sign-in, or that talks to
# something other than Anthropic: the stored token then says nothing useful.
OTHER_ROUTES = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL")

SOURCE_STATUS_LINE = "status line"
SOURCE_OAUTH = "oauth"
SOURCE_CACHE = "usage cache"
SOURCE_NONE = "none"

_STOPPED_TEXT = {"401": "the provider answered 401", "403": "the provider answered 403",
                 "manual": "stopped by hand"}


def _iso(moment):
    if moment is None:
        return None
    return datetime.fromtimestamp(moment, timezone.utc).isoformat()


def account_key(settings_path):
    """One name per Claude profile, shared by every process that reads that profile."""
    folder = os.path.dirname(os.path.abspath(str(settings_path)))
    name = os.path.normcase(os.path.realpath(folder))
    return hashlib.sha256(name.encode("utf-8", "replace")).hexdigest()[:16]


def credentials_path(settings_path):
    return os.path.join(os.path.dirname(os.path.abspath(str(settings_path))), ".credentials.json")


def profile_identity(settings_path):
    """Hash the cache's account ID, as corroboration for a manual attestation.

    Never use an override from another profile. The cache is not token-owner
    proof; it is only a switch detector after the user confirms the mapping.
    """
    for path in candidate_paths(settings_path):
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            continue
        except OSError:
            return None
        try:
            if not stat_module.S_ISREG(info.st_mode) or info.st_size > MAX_FILE_BYTES:
                return None
            with _open_the_file_that_was_checked(path, info) as handle:
                document = json.loads(handle.read(MAX_FILE_BYTES + 1).decode("utf-8-sig"),
                                      parse_constant=_reject_constant)
            value = document.get("cachedUsageUtilization", {}).get("accountUuid")
            current = document.get("oauthAccount", {}).get("accountUuid")
            if (not isinstance(value, str) or not 0 < len(value) <= 256
                    or (current is not None and current != value)):
                return None
            return hashlib.sha256(value.encode("utf-8")).hexdigest()
        except (OSError, ValueError, AttributeError, RecursionError):
            return None
    return None


def state_path(state_dir, settings_path):
    # Conservative per-user budget across *all* profiles. Without a verified
    # account identity, profile paths must never create independent budgets.
    return os.path.join(state_dir, "claude-oauth-budget.json")


def hook_path(state_dir, settings_path):
    return os.path.join(state_dir, "claude-hook-%s.json" % account_key(settings_path))


def new_state():
    return {"version": STATE_VERSION, "last_attempt_at": None, "last_success_at": None,
            "last_status": None, "last_outcome": None, "failures": 0, "rate_limited": 0,
            "backoff_until": 0, "stopped": None, "stopped_at": None, "simulated_at": None,
            "last_decision": None, "last_decision_at": None, "selected_source": None,
            "daemon_ready": None, "export_age": None, "export_age_at": None, "history": [],
            "in_flight": False}


# -- the decision ------------------------------------------------------------


def _decision(reason, detail, next_at=None):
    return {"eligible": reason == "eligible", "reason": reason, "detail": detail,
            "next_at": next_at}


def latest_activity(now, *moments):
    """The newest believable activity time, or None."""
    usable = [moment for moment in moments
              if moment is not None and moment <= now + FUTURE_TOLERANCE_SECONDS]
    return max(usable) if usable else None


def evaluate(state, now, activity_at, interval=DEFAULT_INTERVAL_SECONDS, daemon_ready=True,
             export_age=None, account_changed=False, environ=None, dry_run=False):
    """Whether one request may go out now. Touches no file and no network."""
    interval = max(interval, MIN_INTERVAL_SECONDS)
    environ = os.environ if environ is None else environ
    if state is None:
        return _decision("state_unreadable", "the state file cannot be read; run reenable")
    if state.get("stopped"):
        return _decision("stopped", "%s; run reenable after fixing it"
                         % _STOPPED_TEXT.get(state["stopped"], "stopped"))
    if state.get("in_flight"):
        return _decision("request_incomplete", "a request has no saved outcome; run reenable")
    routes = [name for name in OTHER_ROUTES if environ.get(name)]
    if routes:
        return _decision("other_route", "%s is set here" % ", ".join(routes))
    if activity_at is None:
        return _decision("no_activity", "no Claude activity has been seen")
    if now - activity_at > ACTIVE_SECONDS:
        return _decision("idle", "Claude was last active %d s ago" % (now - activity_at))
    last = [moment for moment in (state.get("last_attempt_at"),
                                  state.get("simulated_at") if dry_run else None)
            if moment is not None]
    last = max(last) if last else None
    backoff = state.get("backoff_until") or 0
    if now < backoff:
        return _decision("backoff", "backing off after %s"
                         % (state.get("last_outcome") or "a failure"), backoff)
    if last is not None and now < last + interval:
        return _decision("cooldown", "the shared cooldown is running", last + interval)
    if last is not None and activity_at <= last:
        return _decision("no_new_activity", "nothing since the last request")
    if not daemon_ready:
        return _decision("daemon_not_ready", "the daemon has not said it can store this reading")
    if export_age is not None and export_age < interval:
        return _decision("status_line_fresh", "the status line reported %d s ago" % export_age)
    if account_changed:
        return _decision("account_changed", "the Claude account behind this profile changed")
    return _decision("eligible", "a request may go out")


def selected_source(now, export_age, oauth_at, cache_at):
    """Which source this computer expects to win: newest first, status line on a tie."""
    export_at = None if export_age is None else now - export_age
    best = SOURCE_NONE, None
    for name, moment in ((SOURCE_STATUS_LINE, export_at), (SOURCE_OAUTH, oauth_at),
                         (SOURCE_CACHE, cache_at)):
        if moment is not None and (best[1] is None or moment > best[1]):
            best = name, moment
    return best[0]


# -- what comes back ---------------------------------------------------------


def retry_after_seconds(value, now):
    """A Retry-After header in seconds, or None when it is missing or unusable."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.isascii() and text.isdigit():
        seconds = int(text) if len(text) <= 9 else MAX_RETRY_AFTER_SECONDS
    else:
        try:
            moment = parsedate_to_datetime(text)
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=timezone.utc)
            seconds = moment.timestamp() - now
        except (TypeError, ValueError, IndexError, OverflowError):
            return None
    return max(0, min(seconds, MAX_RETRY_AFTER_SECONDS))


def apply_outcome(state, status, outcome, now, retry_after=None):
    """The state after one finished request, whatever became of it."""
    state["last_status"] = status
    state["last_outcome"] = outcome
    state["history"] = (state.get("history") or [])[-(MAX_HISTORY - 1):] + [
        {"at": now, "status": status, "outcome": outcome}]
    if outcome == "ok":
        state.update(last_success_at=now, failures=0, rate_limited=0, backoff_until=0)
    elif status in (401, 403):
        state.update(stopped=str(status), stopped_at=now)
    elif status == 429:
        state["rate_limited"] = (state.get("rate_limited") or 0) + 1
        wait = min(RATE_LIMIT_BACKOFF_SECONDS * 2 ** min(state["rate_limited"] - 1, 16),
                   MAX_RATE_LIMIT_BACKOFF_SECONDS)
        state["backoff_until"] = now + max(wait, retry_after or 0)
    else:
        state["failures"] = (state.get("failures") or 0) + 1
        wait = min(FAILURE_BACKOFF_SECONDS * 2 ** min(state["failures"] - 1, 16),
                   MAX_FAILURE_BACKOFF_SECONDS)
        state["backoff_until"] = now + max(wait, retry_after or 0)
    return state


def parse_usage(document, now):
    """The two quota windows of a response, or ValueError. The shape is the cache's."""
    if not isinstance(document, dict):
        raise ValueError("not an object")
    moment = datetime.fromtimestamp(now, timezone.utc)
    reader = UsageCacheReader(())
    windows = []
    for window_id, label, minutes in WINDOWS:
        try:
            windows.append(reader._window(document.get(window_id), window_id, label, minutes,
                                          moment))
        except FieldProblem:
            windows.append({"id": window_id, "label": label, "period_minutes": minutes,
                            "used_percent": None, "resets_at": None, "state": "unavailable"})
    if not any(window["used_percent"] is not None for window in windows):
        raise ValueError("no quota window")
    return windows


# -- the request -------------------------------------------------------------


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        return None


def default_opener():
    """No proxy, so the token only ever meets the endpoint; no redirect is followed."""
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())


def read_access_token(path, now):
    """``(token, None)``, or ``(None, reason)``. Reads the file and changes nothing.

    The reason is a fixed word: nothing from the file reaches it.
    """
    try:
        info = os.lstat(path)
        if not stat_module.S_ISREG(info.st_mode):
            return None, "credentials_unavailable"
        if info.st_size > MAX_CREDENTIALS_BYTES:
            return None, "credentials_unusable"
        with _open_the_file_that_was_checked(path, info) as handle:
            document = json.loads(handle.read(MAX_CREDENTIALS_BYTES).decode("utf-8-sig"),
                                  parse_constant=_reject_constant)
    except (OSError, ValueError, RecursionError):
        return None, "credentials_unavailable"
    block = document.get("claudeAiOauth") if isinstance(document, dict) else None
    if not isinstance(block, dict):
        return None, "credentials_unusable"
    token, expires = block.get("accessToken"), block.get("expiresAt")
    if (not isinstance(token, str) or not 0 < len(token) <= MAX_TOKEN_CHARS
            or not all("!" <= char <= "~" for char in token)):
        return None, "credentials_unusable"
    if isinstance(expires, bool) or not isinstance(expires, (int, float)):
        return None, "credentials_unusable"
    if not 0 < expires <= 1e15:
        return None, "credentials_unusable"
    if expires / 1000.0 <= now + TOKEN_EXPIRY_MARGIN_SECONDS:
        return None, "token_expiring"
    return token, None


def fetch(token, version, opener=None, url=ENDPOINT):
    """One GET. Returns ``(status, body, retry_after_header, outcome)``; never raises.

    Only a status and a fixed word come back from a failure: an exception's text
    can quote a URL or a header, so it is never kept.
    """
    request = urllib.request.Request(url, headers={
        "Authorization": "Bearer " + token, "anthropic-beta": BETA_HEADER,
        "Accept": "application/json",
        "User-Agent": "presto-ai-dash-collector/%s (experimental)" % version})
    try:
        with (opener or default_opener()).open(request, timeout=REQUEST_TIMEOUT_SECONDS) as reply:
            return reply.status, reply.read(MAX_RESPONSE_BYTES + 1), None, "ok"
    except urllib.error.HTTPError as error:
        code, header = error.code, error.headers.get("Retry-After") if error.headers else None
        error.close()
        if code in (401, 403):
            outcome = "unauthorized" if code == 401 else "forbidden"
        elif code == 429:
            outcome = "rate_limited"
        elif 300 <= code < 400:
            outcome = "redirect_refused"
        else:
            outcome = "http_error"
        return code, None, header, outcome
    except Exception:  # noqa: BLE001 - http.client has failures of its own
        return None, None, None, "network_error"


# -- the state file ----------------------------------------------------------


class StateFile:
    """The shared record. Every change is made under a lock and replaces the file whole."""

    def __init__(self, path):
        self.path = path
        self._lock_path = path + ".lock"

    def load(self):
        """The state, a new one when there is no file, or None when it cannot be trusted."""
        try:
            info = os.lstat(self.path)
            if not stat_module.S_ISREG(info.st_mode) or info.st_size > 1024 * 1024:
                return None
            with _open_the_file_that_was_checked(self.path, info) as handle:
                state = json.loads(handle.read(1024 * 1024 + 1).decode("utf-8"),
                                   parse_constant=_reject_constant)
        except FileNotFoundError:
            return new_state()
        except (OSError, ValueError, RecursionError):
            return None
        if not isinstance(state, dict) or state.get("version") != STATE_VERSION:
            return None
        for key in ("last_attempt_at", "last_success_at", "backoff_until", "simulated_at",
                    "last_decision_at", "stopped_at", "export_age", "export_age_at"):
            value = state.get(key)
            if value is not None and (isinstance(value, bool)
                                      or not isinstance(value, (int, float))
                                      or not -1e12 <= value <= 1e12):
                return None
        for key in ("failures", "rate_limited"):
            if type(state.get(key, 0)) is not int or not 0 <= state.get(key, 0) <= 1000000:
                return None
        if (state.get("stopped") not in (None, "401", "403", "manual")
                or type(state.get("in_flight", False)) is not bool
                or not isinstance(state.get("history", []), list)
                or len(state.get("history", [])) > MAX_HISTORY
                or any(not isinstance(item, dict) for item in state.get("history", []))):
            return None
        binding = state.get("binding")
        if binding is not None and (not isinstance(binding, dict)
                or set(binding) != {"account", "credential", "destination"}
                or any(not isinstance(value, str) or not 0 < len(value) <= 256
                       for value in binding.values())):
            return None
        return dict(new_state(), **state)

    def _save(self, state):
        descriptor, temporary = tempfile.mkstemp(prefix=os.path.basename(self.path) + ".",
                                                  suffix=".tmp", dir=os.path.dirname(self.path))
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(state, handle)
                handle.flush()
                os.fsync(handle.fileno())
            for attempt in range(REPLACE_ATTEMPTS):
                try:
                    os.replace(temporary, self.path)
                    return
                except PermissionError:
                    # Windows refuses while another process has the file open.
                    if attempt == REPLACE_ATTEMPTS - 1:
                        raise
                    time.sleep(0.05)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

    def _acquire(self):
        # Kernel locks release on process exit. Never unlink a lock based on age:
        # a paused owner or two stale-lock removers can otherwise both enter.
        handle = None
        try:
            if os.path.islink(self._lock_path):
                return None
            descriptor = os.open(self._lock_path, os.O_RDWR | os.O_CREAT
                                 | getattr(os, "O_NOFOLLOW", 0), 0o600)
            handle = os.fdopen(descriptor, "r+b", buffering=0)
            if os.fstat(descriptor).st_size == 0:
                handle.write(b"0")
        except OSError:
            if handle is not None:
                handle.close()
            return None
        deadline = time.monotonic() + LOCK_WAIT_SECONDS
        while True:
            try:
                handle.seek(0)
                if os.name == "nt":
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return handle
            except OSError:
                pass
            if time.monotonic() >= deadline:
                handle.close()
                return None
            time.sleep(0.02)

    def update(self, change, repair=False):
        """Apply ``change(state)`` under the lock, and return what it returned.

        ``change`` returns None to leave the file alone. The answer is None too
        when the lock, the file or the disk says no, so a caller about to spend
        a request treats every doubt as a refusal. ``repair`` starts over from an
        unreadable file, which only a person asking for it may do.
        """
        lock = self._acquire()
        if lock is None:
            return None
        try:
            state = self.load()
            if state is None:
                if not repair:
                    return None
                state = new_state()
            result = change(state)
            if result is not None:
                self._save(state)
            return result
        except OSError:
            return None
        finally:
            lock.close()


# -- hooks -------------------------------------------------------------------


def write_hook_signal(state_dir, settings_path, event, now):
    """Note that Claude Code ran a hook: the moment and the event's fixed name, nothing else."""
    if event not in HOOK_EVENTS:
        return False
    path = hook_path(state_dir, settings_path)
    if os.path.islink(path):
        return False
    descriptor, temporary = tempfile.mkstemp(prefix=os.path.basename(path) + ".", suffix=".tmp",
                                              dir=os.path.dirname(path))
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump({"at": now, "event": event}, handle)
    os.replace(temporary, path)
    return True


def read_hook_signal(state_dir, settings_path):
    """The moment of the last hook, or None. A hook is an optional extra signal."""
    path = hook_path(state_dir, settings_path)
    try:
        if os.path.islink(path) or os.path.getsize(path) > 1024:
            return None
        with open(path, "r", encoding="utf-8") as handle:
            value = json.load(handle).get("at")
    except (OSError, ValueError, AttributeError, RecursionError):
        return None
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value)):
        return None
    return value


def hook_settings(python, script, settings_path):
    """The ``hooks`` block for settings.json. Exec form and async: no shell, never waited on."""
    def handler(event):
        return {"hooks": [{"type": "command", "command": python.replace("\\", "/"),
                           "args": [script.replace("\\", "/"), "claude-hook", "--event", event,
                                    "--settings-path", str(settings_path).replace("\\", "/")],
                           "async": True, "timeout": 5}]}
    return {"hooks": {event: [handler(event)] for event in HOOK_EVENTS}}


# -- one account -------------------------------------------------------------


class OAuthPoller:
    """The rules for one Claude profile. Holds no secret between calls."""

    def __init__(self, settings_path, state_dir, interval=DEFAULT_INTERVAL_SECONDS,
                 dry_run=False, version="0", opener=None, environ=None, destination=None):
        self.settings_path = settings_path
        self.state_dir = state_dir
        self.interval = max(interval, MIN_INTERVAL_SECONDS)
        self.dry_run = dry_run
        self.version = version
        self.opener = opener
        self.environ = environ
        self.destination = destination
        self.state_file = StateFile(state_path(state_dir, settings_path)) if state_dir else None
        self.identity_file = StateFile(os.path.join(state_dir, "claude-identity-%s.json" %
                                                   account_key(settings_path))) if state_dir else None
        # A 401 whose record could not be saved still stops this process.
        self._halted = False
        self.last_http_status = None
        self.request_seconds = None

    def _binding(self, token):
        if not token:
            return None
        identity = profile_identity(self.settings_path)
        destination = self.destination() if self.destination else None
        if not identity or not destination:
            return None
        return {"account": identity, "credential": hashlib.sha256(token.encode("utf-8")).hexdigest(),
                "destination": destination}

    def attest(self, now):
        """User-confirmed local mapping; no network and no provider-file writes."""
        if self.identity_file is None:
            return False
        token, problem = read_access_token(credentials_path(self.settings_path), now)
        binding = self._binding(token) if token else None
        token = None
        if binding is None:
            return False
        def change(state):
            state.update(binding=binding, suspended=False, attested_at=now)
            return True
        return self.identity_file.update(change) is True

    def identity_verified(self, token):
        """A matching explicit attestation, latched closed on any observed change."""
        if self.identity_file is None:
            return False
        binding = self._binding(token)
        def check(state):
            if not state.get("binding"):
                return None
            if state.get("suspended") or binding is None or state["binding"] != binding:
                state["suspended"] = True
                return False
            return True
        return self.identity_file.update(check) is True

    def identity_current(self, now):
        token, _ = read_access_token(credentials_path(self.settings_path), now)
        return self.identity_verified(token)

    def destination_verified(self, destination):
        """Check the exact token captured for delivery, not a later file read."""
        if self.identity_file is None:
            return False
        def check(state):
            binding = state.get("binding") or {}
            if state.get("suspended") or binding.get("destination") != destination:
                state["suspended"] = True
                return False
            return True
        return self.identity_file.update(check) is True

    def activity_at(self, summary, now):
        """When Claude last did something: its session logs and, if installed, its hooks."""
        logged = None
        if isinstance(summary, dict) and summary.get("available"):
            logged = _parse_iso(summary.get("last_request_at"))
        hook = read_hook_signal(self.state_dir, self.settings_path) if self.state_dir else None
        return latest_activity(now, logged, hook)

    def _evaluate(self, state, now, activity_at, daemon_ready, export_age, account_changed):
        return evaluate(state, now, activity_at, self.interval, daemon_ready, export_age,
                        account_changed, self.environ, self.dry_run)

    def preview(self, now, summary, cache_at=None, account_changed=False):
        """What the next round would decide. Reads state and logs; writes and sends nothing.

        The daemon is not contacted, so its last known answer stands in; with
        none on record it is assumed to be ready.
        """
        if self.state_file is None:
            return _decision("no_state_directory", "there is no private folder for the state file")
        state = self.state_file.load()
        export_age = None
        if state and state.get("export_age") is not None and state.get("export_age_at"):
            export_age = state["export_age"] + max(0, now - state["export_age_at"])
        ready = True if not state or state.get("daemon_ready") is None else state["daemon_ready"]
        result = self._evaluate(state, now, self.activity_at(summary, now), ready, export_age,
                                account_changed)
        result["selected_source"] = selected_source(
            now, export_age, (state or {}).get("last_success_at"), cache_at)
        return result

    def poll(self, now, summary, daemon_ready, export_age, cache_at=None, account_changed=False):
        """One round's decision and, when it allows one, one request.

        Returns the decision plus ``reading`` (windows and the time they were
        fetched, or None) and ``attempted``. Never raises.
        """
        if self.state_file is None:
            return dict(_decision("no_state_directory",
                                  "there is no private folder for the state file"),
                        reading=None, attempted=False)
        activity_at = self.activity_at(summary, now)
        state = self.state_file.load()
        decision = self._evaluate(state, now, activity_at, daemon_ready, export_age,
                                  account_changed)
        if self._halted:
            decision = _decision("stopped", "the provider refused this token; run reenable")
        if decision["eligible"]:
            if self.dry_run:
                decision = _decision("dry_run", "a request would go out now")
                self._note(decision, now, daemon_ready, export_age, cache_at, simulated=True)
                return dict(decision, reading=None, attempted=False)
            token, problem = read_access_token(credentials_path(self.settings_path), now)
            if token is not None and not self.identity_verified(token):
                decision = _decision("identity_unverified",
                                     "account mapping not attested or changed; run attest")
                self._note(decision, now, daemon_ready, export_age, cache_at)
                return dict(decision, reading=None, attempted=False)
            if token is None:
                decision = _decision(problem, "no usable token right now; nothing was sent")
            elif not self._claim(now, activity_at, daemon_ready, export_age, account_changed):
                decision = _decision("claim_refused", "another process has this turn, or the "
                                                      "state file cannot be written")
            else:
                current, _ = read_access_token(credentials_path(self.settings_path), now)
                if current != token or not self.identity_verified(token):
                    # Keep the unresolved claim closed; no request was sent.
                    if current:
                        self.identity_verified(current)
                    return dict(_decision("identity_unverified", "identity changed before request"),
                                reading=None, attempted=False)
                current = None
                reading = self._request(token, now)
                current, _ = read_access_token(credentials_path(self.settings_path), now)
                if current != token or not self.identity_verified(token):
                    if current:
                        self.identity_verified(current)
                    reading = None  # never deliver a result across an observed switch
                current = None
                token = None
                self._note(_decision("requested", "one request was made"), now, daemon_ready,
                           export_age, cache_at)
                state = self.state_file.load() or {}
                return dict(_decision("requested", "request finished: %s"
                                      % (state.get("last_outcome") or "unknown")),
                            reading=reading, attempted=True)
        self._note(decision, now, daemon_ready, export_age, cache_at)
        return dict(decision, reading=None, attempted=False)

    def _claim(self, now, activity_at, daemon_ready, export_age, account_changed):
        """Take this turn: decide again under the lock and stamp it before anything is sent."""
        def change(state):
            again = self._evaluate(state, now, activity_at, daemon_ready, export_age,
                                   account_changed)
            if not again["eligible"]:
                return None
            state["last_attempt_at"] = now
            state["in_flight"] = True
            return True
        return self.state_file.update(change) is True

    def _request(self, token, now):
        started = time.monotonic()
        status, body, header, outcome = fetch(token, self.version, self.opener)
        self.request_seconds = time.monotonic() - started
        self.last_http_status = status
        reading = None
        if outcome == "ok":
            try:
                if len(body) > MAX_RESPONSE_BYTES:
                    raise ValueError("too large")
                document = json.loads(body.decode("utf-8-sig"), parse_constant=_reject_constant)
                reading = {"fetched_at": _iso(now), "windows": parse_usage(document, now)}
            except (ValueError, RecursionError):
                outcome = "format_changed"
        wait = retry_after_seconds(header, now)

        def change(state):
            apply_outcome(state, status, outcome, now, wait)
            state["in_flight"] = False
            return True

        if self.state_file.update(change) is None and status in (401, 403):
            self._halted = True
        return reading

    def _note(self, decision, now, daemon_ready, export_age, cache_at, simulated=False):
        """Keep what the diagnostics show. Losing this write costs nothing."""
        def change(state):
            state.update(last_decision=decision["reason"], last_decision_at=now,
                         daemon_ready=daemon_ready, export_age=export_age,
                         export_age_at=now if export_age is not None else None,
                         selected_source=selected_source(now, export_age,
                                                         state.get("last_success_at"), cache_at))
            if simulated:
                state["simulated_at"] = now
            return True
        self.state_file.update(change)

    def stop(self, now):
        """The kill switch. Takes effect before the next request of every process."""
        def change(state):
            state.update(stopped="manual", stopped_at=now)
            return True
        return self.state_file is not None and self.state_file.update(change, repair=True) is True

    def reenable(self):
        """Lift a stop. Any running backoff stays: being asked to wait is not undone by hand."""
        def change(state):
            state.update(stopped=None, stopped_at=None, in_flight=False)
            return True
        return self.state_file is not None and self.state_file.update(change, repair=True) is True


def status_lines(state, now):
    """The state in plain lines. The file holds no secret, so neither can this."""
    if state is None:
        return ["state: UNREADABLE (no request will go out; run reenable to start over)"]

    def when(key):
        value = state.get(key)
        return "never" if value is None else "%s (%d s ago)" % (_iso(value), now - value)

    lines = ["stopped: %s" % (_STOPPED_TEXT.get(state.get("stopped"), state.get("stopped"))
                              if state.get("stopped") else "no"),
             "last request: " + when("last_attempt_at"),
             "last success: " + when("last_success_at"),
             "last response: status %s, %s" % (state.get("last_status"),
                                               state.get("last_outcome")),
             "backoff until: %s" % (_iso(state["backoff_until"])
                                    if (state.get("backoff_until") or 0) > now else "not backing off"),
             "last decision: %s at %s" % (state.get("last_decision"),
                                          _iso(state.get("last_decision_at"))),
             "selected source: %s" % state.get("selected_source")]
    if state.get("simulated_at") is not None:
        lines.append("last dry-run request: " + when("simulated_at"))
    history = state.get("history") or []
    lines.append("recent requests (%d kept):" % len(history))
    lines.extend("  %s status %s %s" % (_iso(item.get("at")), item.get("status"),
                                        item.get("outcome")) for item in history[-10:])
    return lines
