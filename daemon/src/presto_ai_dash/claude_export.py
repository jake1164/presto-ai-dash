"""Minimal Claude Code status-line bridge: subscription readings straight to SQLite."""

import argparse
import base64
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile

# Also executable by absolute filename from Claude's unrelated working directory.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from presto_ai_dash.credentials import load_cipher
from presto_ai_dash.models import now_utc
from presto_ai_dash.quotas import quota_report, quota_window
from presto_ai_dash.storage import Storage

WINDOWS = (("five_hour", "5h", 300), ("seven_day", "Weekly", 10080))
MAX_INPUT = 65536


def normalized_export(data, observed=None):
    """Whitelist quota fields; fingerprints prevent cached redraws inventing freshness."""
    if not isinstance(data, dict):
        raise ValueError("invalid status-line input")
    limits = data.get("rate_limits")
    if limits is None:
        return None  # Before a normal response: don't erase earlier telemetry.
    if not isinstance(limits, dict):
        raise ValueError("invalid quota fields")
    observed = observed or now_utc()
    # Wall-clock/session duration and prompt IDs can change without a response.
    # Use only API-response progress. Store hashes, never session identity.
    markers = []
    for section, keys in (("cost", ("total_api_duration_ms",)),
                          ("context_window", ("total_input_tokens", "total_output_tokens"))):
        value = data.get(section)
        if isinstance(value, dict):
            for key in keys:
                marker = value.get(key)
                if type(marker) in (int, float) and math.isfinite(marker) and marker >= 0:
                    markers.append((section + "." + key, marker))
    progress = [data.get("session_id") if isinstance(data.get("session_id"), str) else None,
                markers] if markers else None
    windows, evidence = [], {}
    for key, label, minutes in WINDOWS:
        value = limits.get(key)
        if value is None:
            window = quota_window(key, label, None, None, observed, minutes, state="unavailable")
        else:
            if not isinstance(value, dict):
                raise ValueError("invalid quota window")
            percent, reset = value.get("used_percentage"), value.get("resets_at")
            if percent is None or type(reset) not in (int, float) or not math.isfinite(reset):
                raise ValueError("incomplete quota window")
            window = quota_window(key, label, percent, datetime.fromtimestamp(reset, timezone.utc),
                                  observed, minutes)
        windows.append(window)
        signal = [window["state"], window["used_percent"], window["resets_at"],
                  progress if window["state"] == "available" else None]
        evidence[key] = hashlib.sha256(json.dumps(signal, sort_keys=True).encode()).hexdigest()
    report = quota_report("anthropic", "Claude", windows, 900)
    report["note"] = "Claude Code terminal export; idle readings become stale. VS Code panel coverage is unverified."
    version = data.get("version")
    if isinstance(version, str) and len(version) <= 64 and all(c.isdigit() or c in ".-abcdefghijklmnopqrstuvwxyz" for c in version):
        report["collector_version"] = version
    return report, evidence


def helper_command(database, instance_id, key_file=""):
    args = [sys.executable, str(Path(__file__).resolve()), "--database", str(Path(database).resolve()),
            "--instance", instance_id]
    if key_file:
        args += ["--key-file", str(Path(key_file).resolve())]
    if os.name == "nt":
        # Encoded PowerShell avoids shell interpolation by either Git Bash or PS.
        quoted = " ".join("'" + value.replace("\\", "/").replace("'", "''") + "'" for value in args)
        script = ("$ProgressPreference = 'SilentlyContinue'; "
                  "$OutputEncoding = [Console]::InputEncoding = [Text.UTF8Encoding]::new($false); "
                  "$prestoInput = [Console]::In.ReadToEnd(); $prestoInput | & " + quoted)
        return "powershell -NoProfile -NonInteractive -EncodedCommand " + base64.b64encode(script.encode("utf-16-le")).decode()
    return " ".join(shlex.quote(value) for value in args)


def _settings(path):
    path = Path(path).expanduser()
    if not path.is_absolute() or path.name != "settings.json" or path.is_symlink():
        raise ValueError("Choose an absolute Claude settings.json path without a symlink")
    if not path.parent.is_dir():
        raise ValueError("Start Claude Code in this profile first to create its settings directory")
    raw = path.read_bytes() if path.exists() else b""
    if len(raw) > MAX_INPUT:
        raise ValueError("Claude settings file is too large")
    try:
        settings = json.loads(raw.decode("utf-8-sig")) if raw else {}
    except (ValueError, UnicodeError):
        raise ValueError("Claude settings must be valid JSON; no settings were changed") from None
    if not isinstance(settings, dict):
        raise ValueError("Claude settings must be a JSON object")
    return path.resolve(), raw, settings


def _replace(path, expected, settings):
    current = path.read_bytes() if path.exists() else b""
    if current != expected or path.is_symlink():
        raise ValueError("Claude settings changed; reload before retrying")
    descriptor, temporary = tempfile.mkstemp(prefix=".presto-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(settings, handle, indent=2, ensure_ascii=False)
            handle.write("\n"); handle.flush(); os.fsync(handle.fileno())
        if path.exists():
            os.chmod(temporary, path.stat().st_mode & 0o777)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def configure_bridge(manager, record, action):
    bridge = manager.storage.claude_bridge(record["id"])
    path, raw, settings = _settings(bridge["settings_path"] if bridge else record["config"]["settings_path"])
    if action == "remove":
        if bridge:
            if isinstance(settings.get("statusLine"), dict) and settings["statusLine"].get("command") == bridge["command"]:
                prior = json.loads(manager.cipher.decrypt(record["id"], bridge["previous"]))
                if prior["present"]:
                    settings["statusLine"] = prior["value"]
                else:
                    settings.pop("statusLine", None)
                _replace(path, raw, settings)
            manager.storage.remove_claude_bridge(record["id"])
        return {"export_installed": False}
    if action != "install":
        raise ValueError("unknown export action")
    if bridge:
        if not isinstance(settings.get("statusLine"), dict) or settings["statusLine"].get("command") != bridge["command"]:
            raise ValueError("Status line changed; remove the saved export setup before reinstalling")
        return {"export_installed": True}
    prior = settings.get("statusLine")
    if prior is not None and (not isinstance(prior, dict) or prior.get("type") != "command" or
                              not isinstance(prior.get("command"), str)):
        raise ValueError("Existing Claude status line is unsupported; no settings were changed")
    command = helper_command(manager.config["daemon"]["database"], record["id"],
                             manager.config["admin"]["key_file"])
    previous = manager.cipher.encrypt(record["id"], json.dumps({"present": "statusLine" in settings, "value": prior}))
    manager.storage.save_claude_bridge(record["id"], os.path.normcase(str(path)), previous, command)
    settings["statusLine"] = {**(prior or {}), "type": "command", "command": command}
    try:
        _replace(path, raw, settings)
    except Exception:
        manager.storage.remove_claude_bridge(record["id"])
        raise
    return {"export_installed": True}


def _previous_output(command, raw):
    if os.name == "nt":
        git = shutil.which("git")
        bash = Path(git).resolve().parents[1] / "bin/bash.exe" if git else Path("/missing")
        argv = [str(bash), "-c", command] if bash.is_file() else ["powershell", "-NoProfile", "-Command", command]
    else:
        argv = ["/bin/sh", "-c", command]
    result = subprocess.run(argv, input=raw, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            timeout=2, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    return result.stdout.decode("utf-8", errors="replace")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True)
    parser.add_argument("--instance", required=True)
    parser.add_argument("--key-file", default="")
    args = parser.parse_args(argv)
    raw = sys.stdin.buffer.read(MAX_INPUT + 1)
    output, storage = "Claude quota export waiting for a normal response\n", None
    try:
        if not Path(args.database).is_file():
            raise ValueError("Saved dashboard database is missing")
        storage = Storage(args.database)
        bridge = storage.claude_bridge(args.instance)
        if bridge:
            cipher = load_cipher(args.database, args.key_file, existing=True)
            prior = json.loads(cipher.decrypt(args.instance, bridge["previous"]))["value"]
            if isinstance(prior, dict) and prior.get("command"):
                try:
                    output = _previous_output(prior["command"], raw)
                except (OSError, subprocess.TimeoutExpired):
                    pass
        if len(raw) <= MAX_INPUT:
            normalized = normalized_export(json.loads(raw.decode("utf-8-sig")))
            if normalized:
                report, evidence = normalized
                storage.save_claude_export(args.instance, report, evidence)
                if output.startswith("Claude quota export waiting"):
                    output = "Claude " + " / ".join(w["label"] + " " +
                        ("%g%%" % w["used_percent"] if w["used_percent"] is not None else "unavailable")
                        for w in report["windows"]) + "\n"
    except Exception:
        # A bridge must never reflect full input/provider errors or break Claude.
        pass
    finally:
        if storage:
            storage.close()
    sys.stdout.write(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
