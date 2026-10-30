"""One authenticated status endpoint, served using the standard library."""

import hashlib
import hmac
import io
import json
import logging
import os
import re
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from . import clock_proof
from . import demo
from .dashboard import build_status_payload, lazy_cards
from .device_app import FILE_PATH as DEVICE_FILE_PATTERN
from .device_app import MANIFEST_PATH as DEVICE_APP_MANIFEST_PATH
from .device_protocol import MAX_STATUS_BYTES
from .ingest import ingest as store_observation

log = logging.getLogger(__name__)
STATUS_PATH = "/api/v1/status"
INGEST_PATH = "/api/v1/ingest"
# The container's health check. It says one thing and nothing else: that the
# daemon is alive. No token, no data, no version, no source names.
HEALTH_PATH = "/healthz"
INGEST_BODY_LIMIT = 16384
# The device's only write: one service's alerts on or off. It sets a
# state, so a double tap or a retry is harmless.
ALERTS_PATH = re.compile(r"^/api/v1/alerts/([A-Za-z0-9_-]{1,80})$")
ALERT_BODY_LIMIT = 256
# The lazy card detail: one card's tiles and lines, fetched when the card
# is opened. It is a read-only path with the same shared token.
CARDS_PATH = re.compile(r"^/api/v1/cards/([A-Za-z0-9_-]{1,80})$")
# The device-app routes are matched on a fixed pattern, so a caller cannot
# ask for a path of its own.
DEVICE_APP_FILE_PATH = re.compile(DEVICE_FILE_PATTERN)
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


class _DeadlineReader(io.RawIOBase):
    """Reads a request off a socket, giving up after a total deadline.

    The socket timeout alone only bounds each ``recv``: a client sending one
    byte every few seconds would keep a handler thread for as long as it
    liked. Here every read is also checked against a deadline for the whole
    request.
    """

    def __init__(self, sock, deadline, io_timeout):
        super().__init__()
        self._sock = sock
        self._deadline = deadline
        self._io_timeout = io_timeout

    def readable(self):
        return True

    def readinto(self, buffer):
        remaining = self._deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("request took too long")
        self._sock.settimeout(min(self._io_timeout, remaining))
        return self._sock.recv_into(buffer)


class _TimedWriter(io.RawIOBase):
    """Writes a response with a fresh timeout, whatever the read left behind."""

    def __init__(self, sock, io_timeout):
        super().__init__()
        self._sock = sock
        self._io_timeout = io_timeout

    def writable(self):
        return True

    def write(self, data):
        self._sock.settimeout(self._io_timeout)
        self._sock.sendall(data)
        return len(data)


class _AuthFailures:
    """Counts failed sign-ins per address and says when one is worth a log line.

    A guessing client makes thousands of failures, so a line per failure
    would flood the log and the Problems list. The first failure in a minute
    is reported, then the 10th, 100th and so on. The table is bounded.
    """

    WINDOW_SECONDS = 60.0
    MAX_ADDRESSES = 256
    REPORT_AT = (1, 10, 100, 1000, 10000, 100000)

    def __init__(self):
        self._lock = threading.Lock()
        self._entries = {}

    def record(self, address, now):
        """The count to report for this failure, or None to stay quiet."""
        with self._lock:
            entry = self._entries.get(address)
            if entry is None or now - entry[0] >= self.WINDOW_SECONDS:
                if entry is None and len(self._entries) >= self.MAX_ADDRESSES:
                    oldest = min(self._entries, key=lambda key: self._entries[key][0])
                    del self._entries[oldest]
                entry = self._entries[address] = [now, 0]
            entry[1] += 1
            return entry[1] if entry[1] in self.REPORT_AT else None


def parse_length(value, default=None):
    """A Content-Length made of ASCII digits and nothing else, or ValueError.

    ``int()`` alone also accepts "+16", " 16" and "1_6", which a proxy in front
    may read differently from this server.
    """
    text = default if value is None else value
    if text is None or not text.isascii() or not text.isdigit() or len(text) > 12:
        raise ValueError("not a plain length")
    return int(text)


class BoundedRequestHandler(BaseHTTPRequestHandler):
    """A handler whose whole request must arrive within ``request_deadline``."""

    # The Server header names the program, not the Python that runs it.
    server_version = "presto-ai-dash"
    sys_version = ""

    def version_string(self):
        return self.server_version

    def send_error(self, code, message=None, explain=None):
        """The server's own error answer: plain text, with the safety headers.

        The standard one is an HTML page without them. Nothing the client sent
        is repeated in the body.
        """
        short = self.responses.get(code, ("error", ""))[0]
        body = ("%d %s\n" % (code, short)).encode("ascii", "replace")
        self.send_response(code, message)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy",
                         "default-src 'none'; frame-ancestors 'none'")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        if self.command != "HEAD" and code >= 200 and code not in (204, 205, 304):
            self.wfile.write(body)

    request_deadline = 10.0
    io_timeout = 3.0
    # Pause before answering a bearer token that did not match. It caps how
    # fast one connection can guess, and a correct client never waits.
    auth_failure_delay = 0.25
    server_label = "status"
    auth_failures = _AuthFailures()

    # After the answer, how much of a body the handler left unread is read and
    # thrown away, for how long at most, before the connection closes.
    drain_bytes = 64 * 1024
    drain_seconds = 0.5

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        cls.auth_failures = _AuthFailures()

    def finish(self):
        super().finish()
        self._drain_unread()

    def _drain_unread(self):
        """Read and discard what the client is still sending, then let it close.

        A handler that rejects a request early, a 401 or a 413, has not read the
        body. Closing a socket with unread bytes in it makes Windows, and
        sometimes Linux, reset the connection, and the reset can destroy the
        answer before the client reads it: the client sees a reset instead of
        the error. So the end of the answer is signalled first, and what is
        still arriving is drained, within a small bound.
        """
        try:
            self.connection.shutdown(socket.SHUT_WR)
        except OSError:
            return
        deadline = time.monotonic() + self.drain_seconds
        remaining = self.drain_bytes
        try:
            while remaining > 0:
                left = deadline - time.monotonic()
                if left <= 0:
                    break
                self.connection.settimeout(left)
                chunk = self.connection.recv(min(4096, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
        except OSError:
            pass

    def note_failed_auth(self):
        """Record and slow down a bearer token that did not match.

        Only the address is logged, never the token or the header.
        """
        count = self.auth_failures.record(self.client_address[0], time.monotonic())
        if count:
            log.warning("failed %s sign-in from %s (%d in the last minute)",
                        self.server_label, self.client_address[0], count)
        if self.auth_failure_delay:
            time.sleep(self.auth_failure_delay)

    def setup(self):
        super().setup()
        self.rfile.close()
        self.rfile = io.BufferedReader(_DeadlineReader(
            self.connection, time.monotonic() + self.request_deadline, self.io_timeout))
        self.wfile = _TimedWriter(self.connection, self.io_timeout)


def make_handler(storage, shared_token, source_ids=None, status_provider=None,
                 *, ingest_enabled=None, ingest_rate_limiter=None, client_registry=None,
                 alert_writer=None, alert_rate_limiter=None, card_provider=None,
                 demo_enabled=None, demo_second=None, demo_device_alerts=None,
                 device_lifecycle=None, device_app=None):
    if not shared_token:
        raise ValueError("API requires a nonempty shared token")
    limiter = ingest_rate_limiter if ingest_rate_limiter is not None else _IngestRateLimiter()
    alert_limiter = (alert_rate_limiter if alert_rate_limiter is not None
                     else _AlertRateLimiter())
    # The bells switched in demo mode. Demo mode is sample data with no stored
    # settings, so a tap is remembered here and nowhere else - and it lives in
    # this one server, so it is gone when the daemon restarts.
    demo_alerts = {}

    class StatusHandler(BoundedRequestHandler):
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
            # A HEAD answer has the headers of the GET answer and no body.
            if self.command != "HEAD":
                self.wfile.write(body)

        def _authorized(self):
            """The shared bearer token, compared in constant time."""
            parts = self.headers.get("Authorization", "").split()
            matched = (len(parts) == 2 and parts[0].lower() == "bearer" and
                       hmac.compare_digest(parts[1].encode("utf-8"),
                                           shared_token.encode("utf-8")))
            if parts and not matched:
                self.note_failed_auth()
            return matched

        def _client_name(self):
            """The client's own name for itself, sanitised."""
            from .clients import sanitize_header
            return sanitize_header(self.headers.get("X-Presto-Client"))

        def _demo_on(self):
            """Whether demo mode is on right now, re-read on every request.

            Demo mode is a setting of this daemon, so it can be turned on and
            off without a restart. A broken check must never serve sample data
            to a reader who did not ask for it, so it fails closed.
            """
            if demo_enabled is None:
                return False
            try:
                return bool(demo_enabled())
            except Exception:
                log.exception("demo mode check failed")
                return False

        def _demo_payload(self, query):
            """The demo's own payload, with the bells this server was told.

            It is built by the daemon's builders in demo.py, so it has the
            same shape and size as a real payload. The builder resolves the
            in-memory bell taps before computing Home: no stored setting is read.
            """
            device_alerts = None
            if demo_device_alerts is not None:
                try:
                    device_alerts = demo_device_alerts()
                except Exception:
                    log.exception("the demo's alert settings could not be read")
            payload = demo.demo_payload(demo_second() if demo_second else 0,
                                        alert_overrides=demo_alerts,
                                        device_alerts=device_alerts)
            if LAZY_QUERY in (query or ""):
                lazy_cards(payload)
            return payload

        def do_GET(self):
            parsed = urlparse(self.path)
            path = parsed.path
            if path == HEALTH_PATH:
                # The only unauthenticated route: a container checks that the
                # daemon is alive. It answers {"ok": true} and nothing else.
                self._send_json(200, {"ok": True})
                return
            if ALERTS_PATH.match(path):
                # The route takes a body: only POST may be used here.
                self._send_json(405, {"error": "method_not_allowed"},
                                headers={"Allow": "POST"})
                return
            card_match = CARDS_PATH.match(path)
            if card_match:
                self._card_detail(card_match[1])
                return
            if path in (DEVICE_APP_MANIFEST_PATH,) or DEVICE_APP_FILE_PATH.match(path):
                self._device_app(path)
                return
            if path != STATUS_PATH:
                self._send_json(404, {"error": "not_found"})
                return
            if not self._authorized():
                self._send_json(401, {"error": "unauthorised"})
                return
            try:
                if self._demo_on():
                    # Demo mode overrides the real sources on both daemon
                    # paths, and asks no collector for anything.
                    payload = self._demo_payload(parsed.query)
                elif status_provider is not None:
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
            if device_lifecycle is not None and not self._demo_on():
                # The lifecycle extension describes only the requesting device.
                # Demo mode is skipped on purpose: sample data must never
                # create a durable device record, spend a retry or confirm an
                # update, and real unresolved state is left exactly as it is.
                payload = self._with_lifecycle(payload, device_lifecycle)
            if client_registry is not None:
                # Who is polling: remembered in memory only, and never part of
                # the response, which stays exactly as it was. The device's
                # own diagnostics come along in a header and are validated
                # against a whitelist before they are kept.
                try:
                    client_registry.record(self.headers.get("X-Presto-Client"),
                                           self.client_address[0],
                                           self.headers.get("X-Presto-Revision"),
                                           diag=self.headers.get("X-Presto-Diag"))
                except Exception:
                    log.debug("could not record the client", exc_info=True)
            # A board with no clock battery asks for the time with a nonce;
            # the proof is single-use, so it cannot be replayed (clock_proof).
            payload = clock_proof.attach(payload, self.headers, shared_token, MAX_STATUS_BYTES)
            self._send_json(200, payload)

        def _with_lifecycle(self, payload, lifecycle):
            """A request-local copy carrying the two optional extensions.

            The provider's payload is never mutated: a cached or shared dict
            must not grow one device's state. If the result would exceed the
            device's own response bound, the extensions are dropped rather
            than the quota data: a device with no room is told nothing new,
            and nothing is dispatched or acknowledged.
            """
            merged = dict(payload)
            try:
                extension = lifecycle.observe(self.headers)
                release = lifecycle.release()
            except Exception:
                log.debug("the lifecycle extension was skipped", exc_info=True)
                return merged
            if extension is not None:
                merged["device_lifecycle"] = extension
            if release is not None:
                merged["daemon_release"] = release
            try:
                size = len(json.dumps(merged).encode("utf-8"))
            except (TypeError, ValueError):
                return dict(payload)
            if size > MAX_STATUS_BYTES:
                log.debug("no room for the lifecycle extension in a %d byte payload", size)
                return dict(payload)
            return merged

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

        def _device_app(self, path):
            """The running daemon's own slot bundle, one file per request.

            It is deliberately dull: no redirect (a redirect is how a device
            is steered to another host), no query, no directory listing and no
            path the caller makes up. The shared token authenticates the
            account; the device still has to trust the bytes through TLS.
            """
            if not self._authorized():
                self._send_json(401, {"error": "unauthorised"})
                return
            if device_app is None or self._demo_on():
                # Demo mode has no real bundle to install, and pretending it
                # does would offer a device an update that is not this build.
                self._send_json(404, {"error": "not_found"})
                return
            if path == DEVICE_APP_MANIFEST_PATH:
                body = device_app.manifest()
                if body is None:
                    self._send_json(404, {"error": "bundle_unavailable"})
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)
                return
            match = DEVICE_APP_FILE_PATH.match(path)
            body = device_app.file(match[1]) if match else None
            if body is None:
                self._send_json(404, {"error": "not_found"})
                return
            import hashlib as _hashlib
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Presto-SHA256", _hashlib.sha256(body).hexdigest())
            self.end_headers()
            if self.command != "HEAD":
                # Bounded writes, so a large file does not become one buffer.
                for start in range(0, len(body), 8192):
                    self.wfile.write(body[start:start + 8192])

        def _card_detail(self, card_id):
            """One card's tiles and lines. Read-only, with the shared token."""
            if not self._authorized():
                self._send_json(401, {"error": "unauthorised"})
                return
            if self._demo_on():
                # The demo's cards are its own, on either daemon path.
                try:
                    detail = demo.demo_card_detail(card_id,
                                                   demo_second() if demo_second else 0)
                except Exception:
                    log.exception("could not build the demo card detail")
                    self._send_json(500, {"error": "internal_error"})
                    return
                if not isinstance(detail, dict):
                    self._send_json(404, {"error": "not_found"})
                    return
                self._send_json(200, detail)
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
            # A chunked body has no Content-Length, so it is named for what it
            # is before the length is looked at.
            if self.headers.get("Transfer-Encoding"):
                self._send_json(400, {"error": "unsupported_transfer_encoding"})
                return
            try:
                length = parse_length(self.headers.get("Content-Length"))
            except ValueError:
                length = -1
            if length < 0:
                self._send_json(400, {"error": "invalid_length"})
                return
            if length > INGEST_BODY_LIMIT:
                # Refuse the size before reading a single body byte.
                self._send_json(413, {"error": "body_too_large"})
                return
            parts = self.headers.get("Authorization", "").split()
            token = parts[1] if len(parts) == 2 and parts[0].lower() == "bearer" else ""
            instance_id = storage.instance_for_ingest_token(token)
            if instance_id is None:
                # Unknown, revoked or malformed bearer: the input is never echoed.
                if token:
                    self.note_failed_auth()
                self._send_json(401, {"error": "unauthorised"})
                return
            key = hashlib.sha256(token.encode("utf-8")).hexdigest()
            if not limiter.check(key, time.monotonic()):
                self._send_json(429, {"error": "rate_limited"})
                return
            data = self.rfile.read(length)
            try:
                envelope = json.loads(data.decode("utf-8"))
            except (ValueError, UnicodeError, RecursionError):
                # RecursionError: a body nested thousands of levels deep.
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
            demo_tap = self._demo_on() and demo.is_demo_id(instance_id)
            if alert_writer is None and not demo_tap:
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
                length = parse_length(raw_length)
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
            except (ValueError, UnicodeError, RecursionError):
                self._send_json(400, {"error": "invalid_json"})
                return
            if (not isinstance(payload, dict) or set(payload) != {"enabled"} or
                    type(payload["enabled"]) is not bool):
                self._send_json(400, {"error": "invalid_body"})
                return
            enabled = payload["enabled"]
            if demo_tap:
                # Sample data has no stored settings: the tap is remembered in
                # memory for this server and written nowhere.
                demo_alerts[instance_id] = enabled
                log.info("demo alerts for %s turned %s by %s", instance_id,
                         "on" if enabled else "off", self._client_name())
                self._send_json(200, {"ok": True, "alerts": enabled, "master": True})
                return
            if self._demo_on():
                # Demo mode knows only its own accounts. A real account's
                # setting is never written while it is on, even by mistake.
                self._send_json(404, {"error": "not_found"})
                return
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
    # The default backlog of 5 refuses real clients during a connection burst.
    request_queue_size = 128
    # Requests handled at once. A connection over the limit is answered 503 and
    # closed, so a flood cannot grow threads and memory without bound.
    max_handlers = 64
    # Requests handled at once for one client address, so one machine cannot
    # take every slot from the others. Behind a proxy or Docker's userland
    # proxy every client shares one address, and this is then a second total.
    max_per_address = 16

    def __init__(self, *args, **kwargs):
        self._slots_lock = threading.Lock()
        self._slots_used = 0
        self._open_requests = {}
        self._address_counts = {}
        super().__init__(*args, **kwargs)

    def server_bind(self):
        if os.name == "nt":
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()

    def process_request(self, request, client_address):
        address = client_address[0] if client_address else ""
        with self._slots_lock:
            if (self._slots_used >= self.max_handlers or
                    self._address_counts.get(address, 0) >= self.max_per_address):
                full = True
            else:
                full = False
                self._slots_used += 1
                self._address_counts[address] = self._address_counts.get(address, 0) + 1
                self._open_requests[request] = address
        if full:
            try:
                request.settimeout(0.5)
                request.sendall(b"HTTP/1.0 503 Service Unavailable\r\n"
                                b"Connection: close\r\nContent-Length: 0\r\n\r\n")
            except OSError:
                pass
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._release(request)
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._release(request)

    def _release(self, request):
        with self._slots_lock:
            self._slots_used -= 1
            address = self._open_requests.pop(request, None)
            if address is not None:
                left = self._address_counts.get(address, 0) - 1
                if left > 0:
                    self._address_counts[address] = left
                else:
                    self._address_counts.pop(address, None)

    def handle_error(self, request, client_address):
        # A client that hangs up mid-answer is routine on a LAN, not an error
        # worth a traceback on stderr.
        if isinstance(sys.exc_info()[1], (ConnectionError, TimeoutError)):
            log.debug("client %s dropped the connection", client_address[0])
            return
        super().handle_error(request, client_address)

    def abort_open_requests(self):
        """Cut every connection still being handled, ending its blocked read."""
        with self._slots_lock:
            requests = list(self._open_requests)
        for request in requests:
            try:
                request.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


class ApiServer:
    STOP_GRACE_SECONDS = 2.0

    def __init__(self, host, port, storage, shared_token, source_ids=None,
                 status_provider=None, *, ingest_enabled=None, ingest_rate_limiter=None,
                 handler_class=None, client_registry=None, alert_writer=None,
                 alert_rate_limiter=None, card_provider=None, demo_enabled=None,
                 demo_second=None, demo_device_alerts=None, device_lifecycle=None,
                 device_app=None):
        self.host = host
        self._server = _ExclusiveHTTPServer(
            (host, port), handler_class or make_handler(
                storage, shared_token, source_ids, status_provider,
                ingest_enabled=ingest_enabled,
                ingest_rate_limiter=ingest_rate_limiter,
                client_registry=client_registry,
                alert_writer=alert_writer,
                alert_rate_limiter=alert_rate_limiter,
                card_provider=card_provider,
                demo_enabled=demo_enabled,
                demo_second=demo_second,
                demo_device_alerts=demo_device_alerts,
                device_lifecycle=device_lifecycle,
                device_app=device_app))
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
        # A client trickling bytes must not hold a shutdown hostage: give
        # handlers a moment to finish, then cut their connections.
        deadline = time.monotonic() + self.STOP_GRACE_SECONDS
        with self._server._active_condition:
            while self._server._active_handlers:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._server._active_condition.wait(remaining)
        self._server.abort_open_requests()
        with self._server._active_condition:
            while self._server._active_handlers:
                self._server._active_condition.wait()
        self._server.server_close()
