"""One authenticated status endpoint, served using the standard library."""

import hashlib
import hmac
import json
import logging
import os
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from .dashboard import build_status_payload
from .ingest import ingest as store_observation

log = logging.getLogger(__name__)
STATUS_PATH = "/api/v1/status"
INGEST_PATH = "/api/v1/ingest"
INGEST_BODY_LIMIT = 16384


class _IngestRateLimiter:
    """Two-second per-token throttle for the ingest route.

    Only tokens that passed authentication become entries, and the map drops
    or clears old entries instead of growing without bound.
    """

    INTERVAL_SECONDS = 2.0
    MAX_ENTRIES = 1024

    def __init__(self):
        self._seen = {}

    def check(self, key, now):
        """Whether a request with this key may proceed; it is registered when so."""
        seen = {past_key: past for past_key, past in self._seen.items()
                if now - past < self.INTERVAL_SECONDS}
        self._seen = seen
        if key in seen:
            return False
        if len(seen) >= self.MAX_ENTRIES:
            seen.clear()
        seen[key] = now
        return True


def make_handler(storage, shared_token, source_ids=None, status_provider=None,
                 ingest_enabled=None, ingest_rate_limiter=None):
    if not shared_token:
        raise ValueError("API requires a nonempty shared token")
    limiter = ingest_rate_limiter if ingest_rate_limiter is not None else _IngestRateLimiter()

    class StatusHandler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(3.0)

        def log_message(self, fmt, *args):
            log.debug(fmt, *args)

        def _send_json(self, status, payload):
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            if status == 401:
                self.send_header("WWW-Authenticate", "Bearer")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if urlparse(self.path).path != STATUS_PATH:
                self._send_json(404, {"error": "not_found"})
                return
            parts = self.headers.get("Authorization", "").split()
            if (len(parts) != 2 or parts[0].lower() != "bearer" or
                    not hmac.compare_digest(parts[1].encode("utf-8"),
                                            shared_token.encode("utf-8"))):
                self._send_json(401, {"error": "unauthorised"})
                return
            try:
                if status_provider is not None:
                    payload = status_provider()
                else:
                    snapshots = storage.latest_snapshots()
                    if source_ids is not None:
                        snapshots = {key: value for key, value in snapshots.items()
                                     if key in source_ids}
                    payload = build_status_payload(snapshots)
            except Exception:
                log.exception("could not build status payload")
                self._send_json(500, {"error": "internal_error"})
                return
            self._send_json(200, payload)

        def _ingest_allowed(self):
            """Re-evaluated per request so an admin toggle applies without a restart."""
            if ingest_enabled is None:
                return False
            try:
                return bool(ingest_enabled())
            except Exception:
                # A broken availability check must never open the route.
                log.exception("ingest availability check failed")
                return False

        def do_POST(self):
            if urlparse(self.path).path != INGEST_PATH:
                self._send_json(404, {"error": "not_found"})
                return
            if not self._ingest_allowed():
                # A disabled route looks like an unknown one and hides itself.
                self._send_json(404, {"error": "not_found"})
                return
            try:
                length = int(self.headers.get("Content-Length", ""))
            except ValueError:
                length = -1
            if length < 0:
                self._send_json(400, {"error": "invalid_length"})
                return
            if length > INGEST_BODY_LIMIT:
                # Refuse the size before reading a single body byte.
                self._send_json(413, {"error": "body_too_large"})
                return
            if self.headers.get("Transfer-Encoding"):
                self._send_json(400, {"error": "unsupported_transfer_encoding"})
                return
            parts = self.headers.get("Authorization", "").split()
            token = parts[1] if len(parts) == 2 and parts[0].lower() == "bearer" else ""
            instance_id = storage.instance_for_ingest_token(token)
            if instance_id is None:
                # Unknown, revoked or malformed bearer: the input is never echoed.
                self._send_json(401, {"error": "unauthorised"})
                return
            key = hashlib.sha256(token.encode("utf-8")).hexdigest()
            if not limiter.check(key, time.monotonic()):
                self._send_json(429, {"error": "rate_limited"})
                return
            data = self.rfile.read(length)
            try:
                envelope = json.loads(data.decode("utf-8"))
            except (ValueError, UnicodeError):
                self._send_json(400, {"error": "invalid_json"})
                return
            try:
                accepted = store_observation(storage, instance_id, envelope)
            except ValueError:
                self._send_json(400, {"error": "invalid_envelope"})
                return
            if not accepted:
                # A disabled or deleted instance is refused like a bad token.
                self._send_json(401, {"error": "unauthorised"})
                return
            log.debug("ingest accepted for instance %s", instance_id)
            self.send_response(204)
            self.end_headers()

    return StatusHandler


class _ExclusiveHTTPServer(ThreadingHTTPServer):
    # On Windows SO_REUSEADDR lets two daemons listen on the same port.
    # Keep normal restart behavior elsewhere, without allowing shared listeners.
    allow_reuse_address = os.name != "nt"
    allow_reuse_port = False

    def server_bind(self):
        if os.name == "nt":
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


class ApiServer:
    def __init__(self, host, port, storage, shared_token, source_ids=None,
                 status_provider=None, ingest_enabled=None, ingest_rate_limiter=None,
                 handler_class=None):
        self.host = host
        self._server = _ExclusiveHTTPServer(
            (host, port), handler_class or make_handler(
                storage, shared_token, source_ids, status_provider,
                ingest_enabled, ingest_rate_limiter))
        self._server.daemon_threads = False
        self._server.block_on_close = True
        self._server._active_condition = threading.Condition()
        self._server._active_handlers = 0
        original_process_request_thread = self._server.process_request_thread

        def tracked_process_request_thread(request, client_address):
            with self._server._active_condition:
                self._server._active_handlers += 1
            try:
                original_process_request_thread(request, client_address)
            finally:
                with self._server._active_condition:
                    self._server._active_handlers -= 1
                    self._server._active_condition.notify_all()

        self._server.process_request_thread = tracked_process_request_thread
        self._server.timeout = 0.25
        self._thread = None
        self._stopped = False

    @property
    def actual_port(self):
        return self._server.server_address[1]

    def start(self):
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        name="api", daemon=True)
        self._thread.start()
        log.info("API listening on http://%s:%d", self.host, self.actual_port)

    def stop(self):
        if self._stopped:
            return
        self._stopped = True
        # shutdown() would deadlock if serve_forever() was never started.
        if self._thread is not None:
            self._server.shutdown()
            self._thread.join()
        with self._server._active_condition:
            while self._server._active_handlers:
                self._server._active_condition.wait()
        self._server.server_close()
