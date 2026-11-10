"""The Presto Claude mod: the files it is made of, and the file format it writes.

Claude Code in the VS Code panel never runs the status line, so the quota it
learns from each response never reaches a collector. A Claude Code *mod* can
see it: the ``session.measure`` event carries the 5-hour and weekly figures the
last response reported. This mod writes them to one small file per session in a
folder the collector watches, and does nothing else: it reads no prompts, files
or credentials, makes no network request, starts no process and sends nothing
to Claude. Each file says when Claude produced its figures (the moment of the
event), never when the file was written for some other reason, so a collector
can tell a new reading from a file that was merely touched.

This module holds the mod's text, the file format, and the reader that turns the
files into new readings; installing the mod into Claude Code is a separate step.
It uses only the standard library and is embedded word for word in the one-file
collector.
"""

from datetime import datetime, timedelta, timezone
import json
import os
import re
import stat
import time

MOD_NAME = "presto-quota"
MOD_VERSION = 1
MOD_SCHEMA = "presto-ai-dash.claude-mod/1"
# The folder a mod writes into holds one file per Claude session, so several
# windows never overwrite each other.
FILE_PREFIX = "session-"
FILE_SUFFIX = ".json"
# The windows the mod exports; Claude also reports a gateway spend limit, which
# Presto does not show.
EXPORTED_KINDS = ("five_hour", "seven_day")

PLUGIN_JSON = {
    "name": MOD_NAME,
    "version": "0.%d.0" % MOD_VERSION,
    "description": ("Writes the 5-hour and weekly Claude quota to a local file for the "
                    "Presto AI Dash collector. Reads and sends nothing else."),
    "author": {"name": "Presto AI Dash"},
}

HOOKS_JSON = {
    "description": "Presto AI Dash quota export",
    "modules": ["./register.js"],
}

# The hooks module. __FOLDER__ is replaced by the folder the files go in, as a
# JSON string, so the mod never has to look anything up: it holds an absolute
# path and nothing else about the machine. Every handler catches its own
# errors and always passes the event on, so the mod can never get in the way of
# Claude Code.
REGISTER_JS = r"""// Presto AI Dash quota export, mod version __VERSION__. Generated; do not edit.
//
// After each Claude response, writes the 5-hour and weekly quota Claude Code
// reports to one small file in the folder below, for the Presto collector to
// forward. It reads no prompts, files or credentials, makes no network request
// and starts no process. Remove it with: python presto_collector.py claude-mod remove

const FOLDER = __FOLDER__;
const SCHEMA = "__SCHEMA__";
const MOD_VERSION = __VERSION__;
const KINDS = __KINDS__;
const INSTANCE = (globalThis.crypto && globalThis.crypto.randomUUID
  ? globalThis.crypto.randomUUID()
  : String(Date.now()) + "-" + String(Math.random()).slice(2)).replace(/[^A-Za-z0-9-]/g, "");
const FILE = FOLDER + "__FILE_PREFIX__" + INSTANCE + "__FILE_SUFFIX__";
let seq = 0;

function limitsOf(rateLimits) {
  const out = [];
  if (!Array.isArray(rateLimits)) return out;
  for (const item of rateLimits) {
    if (!item || KINDS.indexOf(item.kind) < 0) continue;
    const percent = Number(item.percentUsed);
    const resets = new Date(item.resetsAt);
    if (!Number.isFinite(percent) || percent < 0 || percent > 1000) continue;
    if (Number.isNaN(resets.getTime())) continue;
    out.push({ kind: item.kind, percent_used: percent, resets_at: resets.toISOString() });
  }
  return out;
}

async function nowIso($) {
  try {
    const at = new Date(await $.clock.now());
    if (!Number.isNaN(at.getTime())) return at.toISOString();
  } catch (error) { /* fall back to the system clock */ }
  return new Date().toISOString();
}

async function save($, observedAt, limits) {
  seq += 1;
  const body = { schema: SCHEMA, mod_version: MOD_VERSION, instance: INSTANCE, seq: seq,
                 observed_at: observedAt, rate_limits: limits };
  await $.fs.write(FILE, JSON.stringify(body) + "\n");
}

export function register(on) {
  // Loaded, no reading yet. The figures at session start are whatever the last
  // response of an earlier session said, so they are never exported as new.
  on("session.start", async ($, e, next) => {
    try { await save($, null, []); } catch (error) { /* never get in Claude's way */ }
    return next(e);
  });
  // After a response, or when a window moves a whole point: a new reading.
  on("session.measure", async ($, e, next) => {
    try {
      const limits = limitsOf(e && e.rateLimits);
      if (limits.length > 0) await save($, await nowIso($), limits);
    } catch (error) { /* never get in Claude's way */ }
    return next(e);
  });
}
"""


def register_js(folder):
    """The hooks module for a mod that writes into ``folder``.

    ``folder`` is an absolute path; the mod writes ``<folder><separator>session-<id>.json``.
    """
    if not isinstance(folder, str) or not folder:
        raise ValueError("the mod needs a folder to write into")
    separator = "\\" if "\\" in folder and "/" not in folder else "/"
    base = folder if folder.endswith(("/", "\\")) else folder + separator
    return (REGISTER_JS
            .replace("__FOLDER__", json.dumps(base))
            .replace("__SCHEMA__", MOD_SCHEMA)
            .replace("__VERSION__", str(MOD_VERSION))
            .replace("__KINDS__", json.dumps(list(EXPORTED_KINDS)))
            .replace("__FILE_PREFIX__", FILE_PREFIX)
            .replace("__FILE_SUFFIX__", FILE_SUFFIX))


def mod_files(folder):
    """The mod's files as ``{relative path: text}`` for a mod writing into ``folder``."""
    return {
        ".claude-plugin/plugin.json": json.dumps(PLUGIN_JSON, indent=2) + "\n",
        "hooks/hooks.json": json.dumps(HOOKS_JSON, indent=2) + "\n",
        "hooks/register.js": register_js(folder),
    }


# ---- reading the files the mod writes ------------------------------------

# A mod file is a few hundred bytes; anything bigger is not one.
MAX_FILE_BYTES = 8192
MAX_FILES = 50
# A reading older than this is never forwarded. The daemon only believes a
# sender's time up to a day back, and beyond that it would date the reading by
# its own receive time, which would make an old figure look new.
MAX_READING_AGE_SECONDS = 6 * 3600
# The mod and the collector share a computer, so their clocks agree; a reading
# from the future is a broken file, not a fresh one.
FUTURE_SECONDS = 300
# A session that has been silent this long no longer needs its file.
PRUNE_AFTER_SECONDS = 24 * 3600
# A torn read (the write is not atomic) is read once more after a moment.
RETRY_SECONDS = 0.05

_FILE_NAME = re.compile(re.escape(FILE_PREFIX) + r"[A-Za-z0-9-]{1,64}" + re.escape(FILE_SUFFIX) + r"\Z")
_INSTANCE = re.compile(r"[A-Za-z0-9-]{1,64}\Z")
_FILE_KEYS = frozenset(("schema", "mod_version", "instance", "seq", "observed_at", "rate_limits"))
_LIMIT_KEYS = frozenset(("kind", "percent_used", "resets_at"))


class ModFileError(ValueError):
    """A mod file that cannot be used; the message says why, never what it held."""


def _moment(value, what):
    if not isinstance(value, str) or not value or len(value) > 40:
        raise ModFileError("%s is not a time" % what)
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ModFileError("%s is not a time" % what) from None
    if moment.tzinfo is None:
        raise ModFileError("%s has no UTC offset" % what)
    return moment.astimezone(timezone.utc)


def parse_file(text):
    """One mod file's text as ``{instance, seq, observed_at, limits}``, or ModFileError.

    ``observed_at`` is None for the marker a session writes when it starts, which
    holds no reading. Unknown keys are errors: a file this reader does not fully
    understand is not trusted for a number.
    """
    try:
        data = json.loads(text)
    except (ValueError, RecursionError):
        raise ModFileError("the file is not valid JSON") from None
    if not isinstance(data, dict) or set(data) != _FILE_KEYS:
        raise ModFileError("the file does not have exactly the mod's fields")
    if data["schema"] != MOD_SCHEMA:
        raise ModFileError("the file is in a different format; update the collector or the mod")
    instance, seq = data["instance"], data["seq"]
    if not isinstance(instance, str) or not _INSTANCE.match(instance):
        raise ModFileError("the session id is not usable")
    if type(seq) is not int or not 1 <= seq <= 10 ** 12:
        raise ModFileError("the sequence number is not usable")
    if type(data["mod_version"]) is not int:
        raise ModFileError("the mod version is not a number")
    observed = None if data["observed_at"] is None else _moment(data["observed_at"], "observed_at")
    entries = data["rate_limits"]
    if not isinstance(entries, list) or len(entries) > len(EXPORTED_KINDS):
        raise ModFileError("rate_limits is not a short list")
    if (observed is None) != (not entries):
        raise ModFileError("a reading needs windows and a time, and a marker needs neither")
    limits, seen = [], set()
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != _LIMIT_KEYS:
            raise ModFileError("a window does not have exactly the mod's fields")
        kind, percent = entry["kind"], entry["percent_used"]
        if kind not in EXPORTED_KINDS or kind in seen:
            raise ModFileError("a window kind is unknown or repeated")
        seen.add(kind)
        if type(percent) not in (int, float) or not 0 <= percent <= 1000 or percent != percent:
            raise ModFileError("a percentage is not usable")
        limits.append({"kind": kind, "percent_used": percent,
                       "resets_at": _moment(entry["resets_at"], "resets_at")})
    return {"instance": instance, "seq": seq, "observed_at": observed, "limits": limits}


class ModReader(object):
    """Reads the folder the Presto mod writes into, and says what is new in it.

    A file counts as a new reading only when its sequence number is higher than
    the last one seen for that session, never because it was touched or
    rewritten, so a reading is dated by the response that produced it.
    """

    def __init__(self, folder):
        self.folder = folder
        self.seen = {}

    def _read(self, path):
        with open(path, "rb") as handle:
            raw = handle.read(MAX_FILE_BYTES + 1)
        if len(raw) > MAX_FILE_BYTES:
            raise ModFileError("the file is too large to be a mod file")
        try:
            return raw.decode("utf-8")
        except UnicodeError:
            raise ModFileError("the file is not text") from None

    def _files(self, now):
        """The mod files in the folder, newest first; stale ones are removed."""
        found = []
        for name in os.listdir(self.folder):
            if not _FILE_NAME.match(name):
                continue
            path = os.path.join(self.folder, name)
            try:
                info = os.lstat(path)
            except OSError:
                continue
            if not stat.S_ISREG(info.st_mode):
                continue  # a link or a folder is not something the mod wrote
            if now - info.st_mtime > PRUNE_AFTER_SECONDS:
                try:
                    os.remove(path)
                except OSError:
                    pass
                continue
            found.append((info.st_mtime, path))
        found.sort(reverse=True)
        return [path for _, path in found[:MAX_FILES]]

    def refresh(self, now=None):
        """What the folder holds now.

        Returns ``{"status", "reading", "notes"}``. ``reading`` is the newest
        new reading as ``{instance, seq, observed_at, limits}`` or None. The
        status is one of: ``not installed``, ``no sessions yet``,
        ``loaded, no reading yet``, ``waiting for a response``, ``reading``,
        ``unreadable files``.
        """
        now = time.time() if now is None else now
        moment = datetime.fromtimestamp(now, timezone.utc)
        if not os.path.isdir(self.folder):
            return {"status": "not installed", "reading": None,
                    "notes": ["no mod folder yet: the mod is not installed, or no Claude "
                              "session has started since it was"]}
        parsed, unreadable = [], 0
        try:
            paths = self._files(now)
        except OSError:
            paths = []
            unreadable += 1
        for path in paths:
            result = None
            for attempt in (0, 1):
                try:
                    result = parse_file(self._read(path))
                    break
                except (ModFileError, OSError):
                    if attempt == 0:
                        time.sleep(RETRY_SECONDS)  # a write may have been caught half done
            if result is None:
                unreadable += 1
            else:
                parsed.append(result)
        candidates = []
        for item in parsed:
            observed = item["observed_at"]
            if (observed is not None and item["seq"] > self.seen.get(item["instance"], 0)
                    and moment - timedelta(seconds=MAX_READING_AGE_SECONDS) <= observed
                    <= moment + timedelta(seconds=FUTURE_SECONDS)):
                candidates.append(item)
        for item in parsed:
            self.seen[item["instance"]] = max(self.seen.get(item["instance"], 0), item["seq"])
        notes = []
        if unreadable:
            notes.append("%d mod file(s) could not be read and were ignored" % unreadable)
        if candidates:
            return {"status": "reading",
                    "reading": max(candidates, key=lambda item: item["observed_at"]),
                    "notes": notes}
        if any(item["observed_at"] is not None for item in parsed):
            return {"status": "waiting for a response", "reading": None, "notes": notes}
        if parsed:
            return {"status": "loaded, no reading yet", "reading": None, "notes": notes}
        return {"status": "unreadable files" if unreadable else "no sessions yet",
                "reading": None, "notes": notes}
