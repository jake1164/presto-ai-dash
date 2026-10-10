"""One authenticated status endpoint, served using the standard library."""

import hmac
import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from .dashboard import build_status_payload

log = logging.getLogger(__name__)
STATUS_PATH = "/api/v1/status"


def make_handler(storage, shared_token):
    if not shared_token:
        raise ValueError("API requires a nonempty shared token")

    class StatusHandler(BaseHTTPRequestHandler):
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
                payload = build_status_payload(storage.latest_snapshots())
            except Exception:
                log.exception("could not build status payload")
                self._send_json(500, {"error": "internal_error"})
                return
            self._send_json(200, payload)

    return StatusHandler


class ApiServer:
    def __init__(self, host, port, storage, shared_token):
        self.host = host
        self._server = ThreadingHTTPServer((host, port), make_handler(storage, shared_token))
        self._server.daemon_threads = True
        self._thread = None

    @property
    def actual_port(self):
        return self._server.server_address[1]

    def start(self):
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        name="api", daemon=True)
        self._thread.start()
        log.info("API listening on http://%s:%d", self.host, self.actual_port)

    def stop(self):
        # shutdown() would deadlock if serve_forever() was never started.
        if self._thread is not None:
            self._server.shutdown()
            self._thread.join()
        self._server.server_close()
