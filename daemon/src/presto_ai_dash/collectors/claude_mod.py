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
import shutil
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


# What the collector tells the daemon about the mod. Files that exist prove the
# mod is loaded, so those states are always sent; "not loaded" and "unreadable"
# are only a problem on a computer where the mod was installed, and say nothing
# on one where it never was.
_LOADED_STATES = {"reading": "working", "waiting for a response": "idle",
                  "loaded, no reading yet": "loaded"}


def daemon_state(status, expected):
    """The state to report for a reader status, or None to report nothing."""
    if status in _LOADED_STATES:
        return _LOADED_STATES[status]
    if not expected:
        return None
    return "unreadable" if status == "unreadable files" else "not_loaded"


# ---- installing the mod into Claude Code ----------------------------------
#
# Claude Code loads a mod from any folder named in the CLAUDE_CODE_PLUGIN_DIRS
# setting (absolute paths, ";" apart on Windows and ":" elsewhere), which can
# live in the "env" block of its settings file. So installing is: write the
# mod's three files into a folder of ours, and add that folder to the setting.
# Nothing else of Claude Code's is touched, the settings file is copied to a
# backup first, and removing takes back exactly what was added.

PLUGIN_DIRS_SETTING = "CLAUDE_CODE_PLUGIN_DIRS"
BACKUP_MARKER = ".presto-backup-"
RECORD_NAME = "install.json"
MAX_SETTINGS_BYTES = 1024 * 1024


class ModInstallError(Exception):
    """Something to tell the user, in plain words, before anything was changed."""


def locations(state_dir, settings_path):
    """Where everything lives, from the folder the collector keeps its files in."""
    state_dir = os.path.abspath(state_dir)
    base = os.path.join(state_dir, "claude-mod")
    return {"settings": os.path.abspath(settings_path),
            "plugin_dir": os.path.join(base, MOD_NAME),
            "record": os.path.join(base, RECORD_NAME),
            "out_dir": os.path.join(state_dir, "claude-quota")}


def _same_folder(first, second):
    return os.path.normcase(os.path.normpath(first)) == os.path.normcase(os.path.normpath(second))


def _entries(value):
    return [item for item in value.split(os.pathsep) if item.strip()]


def _load_settings(path):
    """The settings as ``(dict, existed)``. Refuses what it cannot safely change."""
    try:
        with open(path, "rb") as handle:
            raw = handle.read(MAX_SETTINGS_BYTES + 1)
    except FileNotFoundError:
        return {}, False
    except OSError as error:
        raise ModInstallError("cannot read %s (%s)" % (path, type(error).__name__)) from None
    if len(raw) > MAX_SETTINGS_BYTES:
        raise ModInstallError("%s is larger than %d bytes; not touching it" % (path, MAX_SETTINGS_BYTES))
    try:
        data = json.loads(raw.decode("utf-8-sig"))
    except (ValueError, UnicodeError, RecursionError):
        raise ModInstallError("%s is not plain JSON (comments or a typo?), so it was left "
                              "untouched. Fix it, or add the setting yourself: in its \"env\" "
                              "block set %s to the mod folder." % (path, PLUGIN_DIRS_SETTING)) from None
    if not isinstance(data, dict):
        raise ModInstallError("%s does not hold one JSON object; not touching it" % path)
    env = data.get("env")
    if env is not None and not isinstance(env, dict):
        raise ModInstallError("the \"env\" setting in %s is not an object; not touching it" % path)
    if env is not None and PLUGIN_DIRS_SETTING in env and not isinstance(env[PLUGIN_DIRS_SETTING], str):
        raise ModInstallError("%s in %s is not text; not touching it" % (PLUGIN_DIRS_SETTING, path))
    return data, True


def _write_atomically(path, text):
    """Write a file in whole or not at all, keeping the mode of the one it replaces."""
    folder = os.path.dirname(path)
    os.makedirs(folder, exist_ok=True)
    temporary = os.path.join(folder, ".%s.presto-tmp" % os.path.basename(path))
    try:
        with open(temporary, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        if os.path.exists(path):
            shutil.copymode(path, temporary)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.remove(temporary)


def _save_settings(path, data):
    _write_atomically(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")


def make_backup(path, now=None):
    """Copy the settings file beside itself, never over an earlier copy. None if there is none."""
    if not os.path.exists(path):
        return None
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%d-%H%M%S")
    candidate, number = path + BACKUP_MARKER + stamp, 0
    while os.path.exists(candidate):
        number += 1
        candidate = "%s%s%s-%d" % (path, BACKUP_MARKER, stamp, number)
    shutil.copy2(path, candidate)
    return candidate


def list_backups(settings_path):
    folder = os.path.dirname(settings_path) or "."
    prefix = os.path.basename(settings_path) + BACKUP_MARKER
    try:
        return sorted(os.path.join(folder, name) for name in os.listdir(folder)
                      if name.startswith(prefix))
    except OSError:
        return []


def _read_record(record_path):
    try:
        with open(record_path, "r", encoding="utf-8") as handle:
            data = json.loads(handle.read(65536))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def inspect(state_dir, settings_path):
    """Whether the mod is listed in the settings and whether its files are the current ones.

    Returns ``{"listed", "files_current"}``; raises ModInstallError for settings
    that cannot be read, as installing would.
    """
    place = locations(state_dir, settings_path)
    data, _ = _load_settings(place["settings"])
    env = data.get("env") if isinstance(data.get("env"), dict) else {}
    listed = any(_same_folder(item, place["plugin_dir"])
                 for item in _entries(env.get(PLUGIN_DIRS_SETTING, "")))
    current = True
    for relative, text in mod_files(place["out_dir"]).items():
        try:
            with open(os.path.join(place["plugin_dir"], *relative.split("/")), "r",
                      encoding="utf-8", newline="") as handle:
                current = current and handle.read() == text
        except OSError:
            current = False
    return {"listed": listed, "files_current": current}


def describe_install(state_dir, settings_path, environ=None):
    """The plain-English account of what installing will do, as a list of lines."""
    place = locations(state_dir, settings_path)
    environ = os.environ if environ is None else environ
    lines = [
        "Install the Presto Claude mod",
        "",
        "What will change on this computer:",
        "  1. A small Claude Code mod (3 files) is written to:",
        "       " + place["plugin_dir"],
        "  2. That folder is added to the %s setting in:" % PLUGIN_DIRS_SETTING,
        "       " + place["settings"],
        "     Before that file is changed, a copy is saved beside it as:",
        "       " + os.path.basename(place["settings"]) + BACKUP_MARKER + "<date and time>",
        "",
        "What the mod does:",
        "  After each Claude response it writes your 5-hour and weekly usage",
        "  percentages and their reset times to:",
        "       " + place["out_dir"],
        "  The collector sends those to your Presto daemon. The mod reads no",
        "  prompts, files or credentials, makes no network request and starts no",
        "  process. Claude Code does not sandbox mods, so you can read all of it:",
        "       " + os.path.join(place["plugin_dir"], "hooks", "register.js"),
        "",
        "To undo it at any time:  python presto_collector.py claude-mod remove",
    ]
    if environ.get(PLUGIN_DIRS_SETTING):
        lines[9:9] = ["     Note: your environment already sets %s; the setting added here"
                      % PLUGIN_DIRS_SETTING,
                      "     may replace it for Claude Code. Check it still lists your other mods."]
    return lines


def install(state_dir, settings_path, now=None):
    """Install or refresh the mod. Returns what was done; raises ModInstallError first if unsafe."""
    place = locations(state_dir, settings_path)
    data, existed = _load_settings(place["settings"])      # refuse before changing anything
    env = data.get("env") if isinstance(data.get("env"), dict) else None
    current = _entries(env.get(PLUGIN_DIRS_SETTING, "")) if env else []
    listed = any(_same_folder(item, place["plugin_dir"]) for item in current)
    result = {"plugin_dir": place["plugin_dir"], "settings": place["settings"], "backup": None,
              "files_changed": False, "settings_changed": False}
    for relative, text in mod_files(place["out_dir"]).items():
        target = os.path.join(place["plugin_dir"], *relative.split("/"))
        try:
            with open(target, "r", encoding="utf-8", newline="") as handle:
                same = handle.read() == text
        except OSError:
            same = False
        if not same:
            _write_atomically(target, text)
            result["files_changed"] = True
    os.makedirs(place["out_dir"], exist_ok=True)
    if not listed:
        result["backup"] = make_backup(place["settings"], now)
        record = {"settings": place["settings"], "plugin_dir": place["plugin_dir"],
                  "backup": result["backup"], "created_file": not existed,
                  "created_env": env is None,
                  "created_setting": env is None or PLUGIN_DIRS_SETTING not in env}
        if env is None:
            env = data["env"] = {}
        env[PLUGIN_DIRS_SETTING] = os.pathsep.join(current + [place["plugin_dir"]])
        _save_settings(place["settings"], data)
        _write_atomically(place["record"], json.dumps(record, indent=2) + "\n")
        result["settings_changed"] = True
    return result


def remove(state_dir, settings_path, now=None):
    """Take the mod back out. Returns what was done; the backups and readings are kept."""
    place = locations(state_dir, settings_path)
    record = _read_record(place["record"])
    data, existed = _load_settings(place["settings"])
    result = {"settings": place["settings"], "backup": None, "settings_changed": False,
              "files_removed": False}
    env = data.get("env") if isinstance(data.get("env"), dict) else None
    current = _entries(env.get(PLUGIN_DIRS_SETTING, "")) if env else []
    kept = [item for item in current if not _same_folder(item, place["plugin_dir"])]
    if len(kept) != len(current):
        result["backup"] = make_backup(place["settings"], now)
        if kept:
            env[PLUGIN_DIRS_SETTING] = os.pathsep.join(kept)
        else:
            del env[PLUGIN_DIRS_SETTING]
            if not env and record.get("created_env", True):
                del data["env"]
        if not data and record.get("created_file"):
            os.remove(place["settings"])
        else:
            _save_settings(place["settings"], data)
        result["settings_changed"] = True
    if os.path.isdir(place["plugin_dir"]):
        shutil.rmtree(place["plugin_dir"], ignore_errors=True)
        result["files_removed"] = True
    try:
        os.remove(place["record"])
    except OSError:
        pass
    return result


def status(state_dir, settings_path, now=None):
    """What is installed and what the mod has written lately, as ``(lines, healthy)``."""
    place = locations(state_dir, settings_path)
    lines, problems = [], 0
    try:
        data, _ = _load_settings(place["settings"])
        env = data.get("env") if isinstance(data.get("env"), dict) else {}
        listed = any(_same_folder(item, place["plugin_dir"])
                     for item in _entries(env.get(PLUGIN_DIRS_SETTING, "")))
    except ModInstallError as error:
        lines.append("Claude settings: " + str(error))
        listed, problems = False, problems + 1
    else:
        lines.append("Claude settings (%s): %s" % (
            place["settings"], "lists the mod" if listed else "does not list the mod"))
    expected = mod_files(place["out_dir"])
    present = {}
    for relative in expected:
        try:
            with open(os.path.join(place["plugin_dir"], *relative.split("/")), "r",
                      encoding="utf-8", newline="") as handle:
                present[relative] = handle.read()
        except OSError:
            present[relative] = None
    if all(text is None for text in present.values()):
        lines.append("Mod files (%s): not installed" % place["plugin_dir"])
    elif present == expected:
        lines.append("Mod files (%s): present and current" % place["plugin_dir"])
    else:
        lines.append("Mod files (%s): out of date or incomplete; run claude-mod install"
                     % place["plugin_dir"])
        problems += 1
    if not listed and any(text is not None for text in present.values()):
        problems += 1
    outcome = ModReader(place["out_dir"]).refresh(now)
    lines.append("Mod output (%s): %s" % (place["out_dir"], outcome["status"]))
    lines.extend("  " + note for note in outcome["notes"])
    backups = list_backups(place["settings"])
    lines.append("Settings backups: %d%s" % (len(backups), " (newest %s)" % os.path.basename(backups[-1])
                                              if backups else ""))
    return lines, problems == 0
