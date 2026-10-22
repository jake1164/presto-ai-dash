"""Dashboard-managed Codex ChatGPT sign-in. Credentials stay in Codex."""

from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
import time
import uuid
from pathlib import Path

from .child_env import child_environment
from .sources._codex_rpc import find_executable, CodexReportingError


class CodexLoginError(Exception):
    pass


class _LoginProcess:
    def __init__(self, executable, home, timeout):
        env = child_environment()
        if home:
            profile = Path(home).expanduser()
            if not profile.is_absolute():
                raise CodexLoginError("Codex profile directory must be an absolute path")
            env["CODEX_HOME"] = str(profile)
        self.deadline = time.monotonic() + timeout
        self.messages = queue.Queue(maxsize=128)
        self.closed = threading.Event()
        try:
            self.process = subprocess.Popen(
                [executable, "app-server", "--listen", "stdio://"], env=env,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        except OSError:
            raise CodexLoginError("Unable to start Codex app-server") from None
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()
        self._pending = []
        self._lock = threading.Lock()

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
            raise CodexLoginError("Codex app-server disconnected") from None

    def request(self, method, params=None, timeout=15):
        self._sequence = getattr(self, "_sequence", 0) + 1
        request_id = self._sequence
        self._send({"id": request_id, "method": method, "params": params or {}})
        end = min(self.deadline, time.monotonic() + timeout)
        while time.monotonic() < end:
            remaining = end - time.monotonic()
            if remaining <= 0:
                break
            try:
                message = self.messages.get(timeout=min(remaining, 0.25))
            except queue.Empty:
                continue
            if not isinstance(message, dict):
                with self._lock:
                    self._pending.append(message)
                continue
            if message.get("id") == request_id:
                if "error" in message:
                    raise CodexLoginError("Codex login request failed")
                if not isinstance(message.get("result"), dict):
                    raise CodexLoginError("Codex login schema changed")
                return message["result"]
            with self._lock:
                self._pending.append(message)
        raise CodexLoginError("Codex login timed out")

    def drain(self, timeout=0.25):
        items = list(self._pending)
        self._pending.clear()
        while True:
            try:
                item = self.messages.get(timeout=timeout)
            except queue.Empty:
                break
            if item is not None:
                items.append(item)
            timeout = 0.05
        return items

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
        try:
            self.process.stdin.close()
            self.process.stdout.close()
        except OSError:
            pass


# A finished login stays readable for a while, so the page can show how it
# ended, and then goes. No more than this many are kept at once.
SESSION_KEEP_SECONDS = 3600
SESSION_LIMIT = 20
ERROR_LIMIT = 120


def plain_error(value, default="login failed"):
    """A provider's error text as a short line of printable ASCII.

    The text comes from the Codex program and is shown on the admin page; it
    is not trusted to be short or to hold only printable characters.
    """
    text = " ".join(str(value).split()) if value else ""
    text = "".join(ch if " " <= ch <= "~" else "?" for ch in text)
    if len(text) > ERROR_LIMIT:
        text = text[:ERROR_LIMIT - 3] + "..."
    return text or default


class CodexLoginManager:
    def __init__(self, manager):
        self.manager = manager
        self._sessions = {}
        self._lock = threading.RLock()

    def _record(self, instance_id):
        for row in self.manager.storage.source_instances():
            if row["id"] == instance_id:
                return row
        raise KeyError(instance_id)

    def _start_process(self, instance_id, timeout=15):
        record = self._record(instance_id)
        if record["type"] != "codex":
            raise ValueError("Codex login is only available for Codex sources")
        cfg = dict(record["config"])
        executable = find_executable(cfg.get("executable", ""))
        home = cfg.get("profile_home", "")
        proc = _LoginProcess(executable, home, max(15, timeout))
        proc.request("initialize", {"clientInfo": {
            "name": "presto_ai_dash", "title": "Presto AI Dash", "version": "0.1.0"}})
        proc._send({"method": "initialized"})
        return record, proc

    def start(self, instance_id, flow="device"):
        flow = "browser" if flow == "browser" else "device"
        record, proc = self._start_process(instance_id, record["config"].get("timeout", 15) if False else 15)
        try:
            if flow == "browser":
                params = {"type": "chatgpt", "useHostedLoginSuccessPage": True, "appBrand": "codex"}
            else:
                params = {"type": "chatgptDeviceCode"}
            result = proc.request("account/login/start", params, timeout=15)
        except Exception as exc:
            proc.close()
            raise CodexLoginError(plain_error(exc)) from None
        login_id = result.get("loginId")
        session_id = uuid.uuid4().hex
        session = {
            "id": session_id,
            "source_id": instance_id,
            "flow": flow,
            "status": "pending",
            "login_id": login_id,
            "auth_url": result.get("authUrl"),
            "verification_url": result.get("verificationUrl"),
            "user_code": result.get("userCode"),
            "error": None,
            "started_at": time.time(),
            "completed_at": None,
            "process": proc,
        }
        with self._lock:
            self._prune()
            self._sessions[session_id] = session
        thread = threading.Thread(target=self._watch, args=(session,), daemon=True)
        thread.start()
        session["thread"] = thread
        return self.public(session)

    def _watch(self, session):
        proc = session["process"]
        login_id = session["login_id"]
        deadline = time.monotonic() + 300
        try:
            while time.monotonic() < deadline and not proc.closed.is_set():
                items = proc.drain(timeout=0.5)
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    if item.get("method") == "account/login/completed":
                        params = item.get("params", {})
                        if login_id is None or params.get("loginId") == login_id:
                            if params.get("success"):
                                session["status"] = "success"
                            else:
                                session["status"] = "error"
                                session["error"] = plain_error(params.get("error"))
                            session["completed_at"] = time.time()
                            proc.close()
                            return
                if proc.process.poll() is not None:
                    break
            if session["status"] == "pending":
                session["status"] = "timeout"
                session["error"] = "login timed out"
                session["completed_at"] = time.time()
        finally:
            try:
                proc.close()
            except Exception:
                pass

    def _prune(self, now=None):
        """Forget finished logins that are old, and the oldest beyond the limit.

        Called with the lock held. A login still pending is never dropped.
        """
        now = time.time() if now is None else now
        for session_id, session in list(self._sessions.items()):
            finished = session.get("completed_at")
            if finished is not None and now - finished > SESSION_KEEP_SECONDS:
                del self._sessions[session_id]
        excess = len(self._sessions) - (SESSION_LIMIT - 1)
        if excess > 0:
            finished = sorted((session for session in self._sessions.values()
                               if session.get("completed_at") is not None),
                              key=lambda session: session["completed_at"])
            for session in finished[:excess]:
                del self._sessions[session["id"]]

    def status(self, session_id):
        with self._lock:
            session = self._sessions.get(session_id)
        if not session:
            raise KeyError(session_id)
        return self.public(session)

    def cancel(self, session_id):
        with self._lock:
            session = self._sessions.get(session_id)
        if not session:
            raise KeyError(session_id)
        if session["status"] != "pending":
            return self.public(session)
        proc = session["process"]
        login_id = session["login_id"]
        try:
            if login_id is not None:
                proc.request("account/login/cancel", {"loginId": login_id}, timeout=10)
        except Exception:
            pass
        session["status"] = "cancelled"
        session["completed_at"] = time.time()
        try:
            proc.close()
        except Exception:
            pass
        return self.public(session)

    def logout(self, instance_id):
        record, proc = self._start_process(instance_id, 15)
        try:
            proc.request("account/logout", timeout=15)
        finally:
            proc.close()
        return {"logged_out": True}

    def public(self, session):
        return {key: session[key] for key in (
            "id", "source_id", "flow", "status", "login_id", "auth_url",
            "verification_url", "user_code", "error", "started_at", "completed_at")}


def configure_codex_login(manager, instance_id, data):
    if not isinstance(data, dict):
        raise ValueError("invalid login request")
    flow = data.get("flow", "device")
    if flow not in ("browser", "device"):
        raise ValueError("flow must be browser or device")
    return manager.codex_logins.start(instance_id, flow)
