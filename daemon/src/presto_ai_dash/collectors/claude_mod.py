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

This module only holds the mod's text and the format; installing the mod into
Claude Code and reading its files are separate steps. It uses only the standard
library and is embedded word for word in the one-file collector.
"""

import json

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
