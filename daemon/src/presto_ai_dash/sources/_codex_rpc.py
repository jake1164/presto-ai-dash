"""Bounded read-only Codex app-server session; OAuth remains CLI-owned."""

import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import threading
import time

from ..child_env import child_environment


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
    name = os.path.basename(text.rstrip("/\\"))
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
