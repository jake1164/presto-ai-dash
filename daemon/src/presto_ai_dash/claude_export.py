"""Minimal Claude Code status-line bridge: subscription readings straight to SQLite."""

import argparse
import base64
import json
import os
from pathlib import Path
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile

# Also executable by absolute filename from Claude's unrelated working directory.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from presto_ai_dash.collectors.claude_statusline import MAX_INPUT, build_envelope, normalize
from presto_ai_dash.credentials import load_cipher
from presto_ai_dash.ingest import ingest
from presto_ai_dash.models import iso, now_utc
from presto_ai_dash.quotas import quota_report
from presto_ai_dash.storage import Storage


def normalized_export(data, observed=None):
    """Whitelist quota fields; fingerprints prevent cached redraws inventing freshness.

    A thin wrapper over the shared collector normalization: the daemon adds the
    observation timestamp and report framing, the collector owns the parsing.
    """
    normalized = normalize(data)
    if normalized is None:
        return None
    windows, evidence, _progress, version = normalized
    observed = observed or now_utc()
    report = quota_report("anthropic", "Claude",
                          [dict(window, observed_at=iso(observed)) for window in windows], 900)
    report["note"] = "Claude Code terminal export; idle readings become stale. VS Code panel coverage is unverified."
    if version is not None:
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


def _git_bash():
    """Locate Git for Windows' bash by walking up from git.exe (cmd/, bin/ or ucrt64/bin/)."""
    git = shutil.which("git")
    if git:
        for parent in Path(git).resolve().parents[:4]:
            bash = parent / "bin" / "bash.exe"
            if bash.is_file():
                return bash
    return None


def _previous_output(command, raw):
    if os.name == "nt":
        bash = _git_bash()
        argv = [str(bash), "-c", command] if bash else ["powershell", "-NoProfile", "-Command", "& " + command]
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
            envelope = build_envelope(json.loads(raw.decode("utf-8-sig")), socket.gethostname())
            if envelope and envelope.get("windows"):
                # Before a normal response nothing is stored, as today; live
                # liveness heartbeats are a remote-collector signal only.
                ingest(storage, args.instance, envelope, now_utc())
                if output.startswith("Claude quota export waiting"):
                    output = "Claude " + " / ".join(window["label"] + " " +
                        ("%g%%" % window["used_percent"] if window["used_percent"] is not None else "unavailable")
                        for window in envelope["windows"]) + "\n"
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
