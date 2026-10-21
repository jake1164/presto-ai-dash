"""One authenticated status endpoint, served using the standard library."""

import hashlib
import hmac
import json
import logging
import os
import re
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
# The device's only write: one service's alerts on or off. It sets a
# state, so a double tap or a retry is harmless.
ALERTS_PATH = re.compile(r"^/api/v1/alerts/([A-Za-z0-9_-]{1,80})$")
ALERT_BODY_LIMIT = 256
# The lazy card detail: one card's tiles and lines, fetched when the card
# is opened. It is a read-only path with the same shared token.
CARDS_PATH = re.compile(r"^/api/v1/cards/([A-Za-z0-9_-]{1,80})$")
# The query that asks for the small summary: "?cards=lazy".
LAZY_QUERY = "cards=lazy"


class _IngestRateLimiter:
    """Two-second per-token throttle for the ingest route.

    Only tokens that passed authentication become entries, and the map drops
    or clears old entries instead of growing without bound. The server is
    multi-threaded, so the map is only read and written under the lock.
    """

    INTERVAL_SECONDS = 2.0
    MAX_ENTRIES = 1024

    def __init__(self):
        self._seen = {}
        self._lock = threading.Lock()

    def check(self, key, now):
        """Whether a request with this key may proceed; it is registered when so."""
        with self._lock:
            seen = {past_key: past for past_key, past in self._seen.items()
                    if now - past < self.INTERVAL_SECONDS}
            self._seen = seen
            if key in seen:
                return False
            if len(seen) >= self.MAX_ENTRIES:
                seen.clear()
            seen[key] = now
            return True


class _AlertRateLimiter:
    """A per-address limit for the device's alert writes.

    It counts the writes of one address inside a sliding window, so a
    device that taps quickly is stopped without stopping the others. It is
    separate from the ingest limiter on purpose: the two routes have
    different shapes and different clients.
    """

    LIMIT = 30
    WINDOW_SECONDS = 60.0
    MAX_ENTRIES = 1024

    def __init__(self):
        self._hits = {}
        self._lock = threading.Lock()

    def check(self, key, now):
        """Whether a request with this key may proceed; it is registered when so."""
        with self._lock:
            times = [stamp for stamp in self._hits.get(key, ())
                     if now - stamp < self.WINDOW_SECONDS]
            if len(times) >= self.LIMIT:
                self._hits[key] = times
                return False
            times.append(now)
            if len(self._hits) >= self.MAX_ENTRIES:
                oldest = min(self._hits,
                             key=lambda name: self._hits[name][-1] if self._hits[name] else 0)
                self._hits.pop(oldest, None)
            self._hits[key] = times
            return True


def make_handler(storage, shared_token, source_ids=None, status_provider=None,
                 *, ingest_enabled=None, ingest_rate_limiter=None, client_registry=None,
                 alert_writer=None, alert_rate_limiter=None, card_provider=None):
    if not shared_token:
        raise ValueError("API requires a nonempty shared token")
    limiter = ingest_rate_limiter if ingest_rate_limiter is not None else _IngestRateLimiter()
    alert_limiter = (alert_rate_limiter if alert_rate_limiter is not None
                     else _AlertRateLimiter())

    class StatusHandler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(3.0)

        def log_message(self, fmt, *args):
            log.debug(fmt, *args)

        def _send_json(self, status, payload, headers=None):
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            if status == 401:
                self.send_header("WWW-Authenticate", "Bearer")
            for name, value in (headers or {}).items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body)

        def _authorized(self):
            """The shared bearer token, compared in constant time."""
            parts = self.headers.get("Authorization", "").split()
            return (len(parts) == 2 and parts[0].lower() == "bearer" and
                    hmac.compare_digest(parts[1].encode("utf-8"),
                                        shared_token.encode("utf-8")))

        def _client_name(self):
            """The client's own name for itself, sanitised."""
            from .clients import sanitize_header
            return sanitize_header(self.headers.get("X-Presto-Client"))

        def do_GET(self):
            parsed = urlparse(self.path)
            path = parsed.path
            if ALERTS_PATH.match(path):
                # The route takes a body: only POST may be used here.
                self._send_json(405, {"error": "method_not_allowed"},
                                headers={"Allow": "POST"})
                return
            card_match = CARDS_PATH.match(path)
            if card_match:
                self._card_detail(card_match[1])
                return
            if path != STATUS_PATH:
                self._send_json(404, {"error": "not_found"})
                return
            if not self._authorized():
                self._send_json(401, {"error": "unauthorised"})
                return
            try:
                if status_provider is not None:
                    payload = self._status_payload(parsed.query)
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
            if client_registry is not None:
                # Who is polling: remembered in memory only, and never part of
                # the response, which stays exactly as it was.
                try:
                    client_registry.record(self.headers.get("X-Presto-Client"),
                                           self.client_address[0],
                                           self.headers.get("X-Presto-Revision"))
                except Exception:
                    log.debug("could not record the client", exc_info=True)
            self._send_json(200, payload)

        def _status_payload(self, query):
            """The status payload, small when the client asks for lazy cards.

            A client that does not ask gets exactly the payload it got
            before: the summary without the query is unchanged.
            """
            lazy = LAZY_QUERY in (query or "")
            if not lazy:
                return status_provider()
            try:
                return status_provider(True)
            except TypeError:
                # An older provider that cannot make a lazy payload.
                return status_provider()

        def _card_detail(self, card_id):
            """One card's tiles and lines. Read-only, with the shared token."""
            if not self._authorized():
                self._send_json(401, {"error": "unauthorised"})
                return
            if card_provider is None:
                self._send_json(404, {"error": "not_found"})
                return
            try:
                detail = card_provider(card_id)
            except Exception:
                log.exception("could not build the card detail")
                self._send_json(500, {"error": "internal_error"})
                return
            if not isinstance(detail, dict):
                self._send_json(404, {"error": "not_found"})
                return
            self._send_json(200, detail)

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
            path = urlparse(self.path).path
            match = ALERTS_PATH.match(path)
            if match:
                self._set_alert(match[1])
                return
            if path != INGEST_PATH:
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

        def _set_alert(self, instance_id):
            """One service's alerts on or off. The device's only write.

            It sets a state and never toggles, so a double tap or a retry
            is harmless, and it changes only that one setting: never the
            thresholds, never the master switch, never a provider.
            """
            if not self._authorized():
                self._send_json(401, {"error": "unauthorised"})
                return
            if alert_writer is None:
                # No managed sources: there is nothing to switch.
                self._send_json(404, {"error": "not_found"})
                return
            if not alert_limiter.check(self.client_address[0], time.monotonic()):
                self._send_json(429, {"error": "rate_limited"})
                return
            if (self.headers.get("Content-Type", "").split(";")[0].strip() !=
                    "application/json"):
                self._send_json(415, {"error": "send an application/json body"})
                return
            if self.headers.get("Transfer-Encoding"):
                self._send_json(400, {"error": "unsupported_transfer_encoding"})
                return
            raw_length = self.headers.get("Content-Length")
            if raw_length is None:
                self._send_json(400, {"error": "invalid_length"})
                return
            try:
                length = int(raw_length)
            except ValueError:
                self._send_json(400, {"error": "invalid_length"})
                return
            if length < 0:
                self._send_json(400, {"error": "invalid_length"})
                return
            if length > ALERT_BODY_LIMIT:
                # Refuse the size before reading a single body byte.
                self._send_json(413, {"error": "body_too_large"})
                return
            data = self.rfile.read(length) if length else b""
            try:
                payload = json.loads(data.decode("utf-8")) if data else None
            except (ValueError, UnicodeError):
                self._send_json(400, {"error": "invalid_json"})
                return
            if (not isinstance(payload, dict) or set(payload) != {"enabled"} or
                    type(payload["enabled"]) is not bool):
                self._send_json(400, {"error": "invalid_body"})
                return
            enabled = payload["enabled"]
            try:
                state = alert_writer(instance_id, enabled)
            except KeyError:
                self._send_json(404, {"error": "not_found"})
                return
            except ValueError:
                self._send_json(400, {"error": "invalid_body"})
                return
            except Exception:
                log.exception("could not save the alert setting")
                self._send_json(500, {"error": "internal_error"})
                return
            # The token is never logged, and neither is anything else the
            # client sent: only the name the client gives itself.
            log.info("alerts for %s turned %s by %s", instance_id,
                     "on" if enabled else "off", self._client_name())
            self._send_json(200, {"ok": True,
                                  "alerts": bool(state["effective"]["enabled"]),
                                  "master": bool(state["master"])})

        def do_PUT(self):
            if ALERTS_PATH.match(urlparse(self.path).path):
                self._send_json(405, {"error": "method_not_allowed"},
                                headers={"Allow": "POST"})
                return
            self._send_json(404, {"error": "not_found"})

        def do_DELETE(self):
            if ALERTS_PATH.match(urlparse(self.path).path):
                self._send_json(405, {"error": "method_not_allowed"},
                                headers={"Allow": "POST"})
                return
            self._send_json(404, {"error": "not_found"})

        def do_PATCH(self):
            if ALERTS_PATH.match(urlparse(self.path).path):
                self._send_json(405, {"error": "method_not_allowed"},
                                headers={"Allow": "POST"})
                return
            self._send_json(404, {"error": "not_found"})

        def do_HEAD(self):
            if ALERTS_PATH.match(urlparse(self.path).path):
                self._send_json(405, {"error": "method_not_allowed"},
                                headers={"Allow": "POST"})
                return
            self._send_json(404, {"error": "not_found"})

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
                 status_provider=None, *, ingest_enabled=None, ingest_rate_limiter=None,
                 handler_class=None, client_registry=None, alert_writer=None,
                 alert_rate_limiter=None, card_provider=None):
        self.host = host
        self._server = _ExclusiveHTTPServer(
            (host, port), handler_class or make_handler(
                storage, shared_token, source_ids, status_provider,
                ingest_enabled=ingest_enabled,
                ingest_rate_limiter=ingest_rate_limiter,
                client_registry=client_registry,
                alert_writer=alert_writer,
                alert_rate_limiter=alert_rate_limiter,
                card_provider=card_provider))
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
