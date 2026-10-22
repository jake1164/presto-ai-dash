"""Persistent, revocable browser sessions independent of the startup admin code."""

from functools import lru_cache
import hashlib
import re
import secrets
import time

SESSION_SECONDS = 7 * 24 * 60 * 60

# Fixed, public salt: the binding only has to tell one token from another, and
# it is computed once, so a slow derivation costs nothing.
_BINDING_SALT = b"presto-ai-dash admin sessions v1"
_BINDING_ROUNDS = 100_000


@lru_cache(maxsize=8)
def token_binding(token, generated):
    """What a browser session is bound to.

    A token the operator set is stable, so a session made under it dies when it
    is changed. A generated one is new at every start by design, and the page
    promises that the browser stays signed in across restarts, so those sessions
    share one binding. The token itself is never stored: a derived value is, and
    it is slow to derive so a stolen database does not cheaply give a weak token
    back.
    """
    if generated:
        return "generated"
    derived = hashlib.pbkdf2_hmac("sha256", token.encode("utf-8"), _BINDING_SALT, _BINDING_ROUNDS)
    return "token:" + derived.hex()[:32]


class AdminSessions:
    def __init__(self, storage, binding=""):
        self.storage = storage
        self.binding = binding

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
        self.storage.create_admin_session(self._digest(credential), csrf_token, expires_at, now,
                                          self.binding)
        return credential, {"csrf_token": csrf_token, "expires_at": expires_at}

    def read(self, credential):
        digest = self._digest(credential)
        return self.storage.admin_session(digest, time.time(), self.binding) if digest else None

    def revoke(self, credential):
        digest = self._digest(credential)
        if digest:
            self.storage.revoke_admin_session(digest)

    def revoke_all(self):
        """Sign every browser out. Returns how many were signed in."""
        return self.storage.revoke_all_admin_sessions()

    def count(self):
        return self.storage.admin_session_count(time.time(), self.binding)
