"""Persistent, revocable browser sessions independent of the startup admin code."""

import hashlib
import re
import secrets
import time

SESSION_SECONDS = 7 * 24 * 60 * 60


class AdminSessions:
    def __init__(self, storage):
        self.storage = storage

    @staticmethod
    def _digest(value):
        if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", value):
            return None
        return hashlib.sha256(value.encode("ascii")).hexdigest()

    def create(self):
        credential = secrets.token_urlsafe(32)
        csrf_token = secrets.token_urlsafe(32)
        now = time.time()
        expires_at = now + SESSION_SECONDS
        self.storage.create_admin_session(self._digest(credential), csrf_token, expires_at, now)
        return credential, {"csrf_token": csrf_token, "expires_at": expires_at}

    def read(self, credential):
        digest = self._digest(credential)
        return self.storage.admin_session(digest, time.time()) if digest else None

    def revoke(self, credential):
        digest = self._digest(credential)
        if digest:
            self.storage.revoke_admin_session(digest)
