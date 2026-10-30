"""Loopback-only admin HTTP surface; the device token cannot edit credentials."""

import hmac
from http.cookies import CookieError, SimpleCookie
from importlib.resources import files
import json
import logging
import re
from urllib.parse import parse_qs, urlsplit

from .api import ApiServer, BoundedRequestHandler, parse_length
from .collectors import COLLECTOR_FILE, collector_bytes
from .admin_sessions import AdminSessions, SESSION_SECONDS, token_binding
from .codex_login import CodexLoginError
from .device_setup import HANDOFF_NAME, SCRIPT_NAME, SetupError, SetupUnavailable
from .device_lifecycle import AdminRequestError
from .sources._codex_rpc import CodexReportingError
from .storage import ConfigurationConflict

log = logging.getLogger(__name__)
MAX_BODY = 65536
INGEST_TOKEN_PATH = r"/api/admin/sources/([A-Za-z0-9_-]{1,80})/ingest-token"
SERVICE_ALERTS_PATH = r"/api/admin/sources/([A-Za-z0-9_-]{1,80})/alerts"
SETUP_PATH = "/api/admin/presto-setup"
SETUP_SCRIPT_PATH = r"/api/admin/presto-setup/([A-Za-z0-9_-]{1,80})/script"
SETUP_HANDOFF_FILE_PATH = r"/api/admin/presto-setup/([A-Za-z0-9_-]{1,80})/handoff"
SETUP_HANDOFF_PATH = r"/api/admin/presto-setup/([A-Za-z0-9_-]{1,80})"
SETUP_BODY_LIMIT = 1024
# Device identity, revision and update state. The ID is the 32 lowercase
# hexadecimal characters the setup utility generates; the route takes the same
# shape as every other admin identifier and the lifecycle coordinator refuses
# anything that is not a well-formed device ID.
DEVICE_PATH = r"/api/admin/devices/([A-Za-z0-9_-]{1,80})"
DEVICE_RETRY_PATH = r"/api/admin/devices/([A-Za-z0-9_-]{1,80})/retry"
DEVICE_RECOVERY_PATH = r"/api/admin/devices/([A-Za-z0-9_-]{1,80})/confirm-recovery"
RELEASES_PATH = "/api/admin/releases"
RELEASES_CHECK_PATH = "/api/admin/releases/check"
# The check request carries nothing; a small body is read and ignored.
RELEASE_CHECK_BODY_LIMIT = 512
# The retry and recovery bodies: two fields, and an idempotency key.
DEVICE_BODY_LIMIT = 512
IDEMPOTENCY_CHARS = 32
HEX_DIGITS = "0123456789abcdef"
ASSETS = {"/": ("index.html", "text/html; charset=utf-8"),
          "/admin.js": ("admin.js", "text/javascript; charset=utf-8"),
          "/admin.css": ("admin.css", "text/css; charset=utf-8")}
# The names the admin page always answers to: this machine, over plain HTTP.
LOOPBACK_NAMES = ("127.0.0.1", "localhost")


def split_host_port(value):
    """A ``(hostname, port)`` pair for a Host header or an allowed name.

    The port is None when the value carries none. A bracketed IPv6 address
    keeps its brackets out of the way, so ``[::1]:8788`` is ``("::1", 8788)``.
    """
    text = (value or "").strip().lower()
    if not text:
        return "", None
    if text.startswith("["):
        end = text.find("]")
        if end < 0:
            return text, None
        host = text[1:end]
        rest = text[end + 1:]
        if rest.startswith(":") and rest[1:].isdigit():
            return host, int(rest[1:])
        return host, None
    if text.count(":") > 1:
        # A bare IPv6 address carries no port.
        return text, None
    if ":" in text:
        host, _, port = text.rpartition(":")
        if port.isdigit():
            return host, int(port)
    return text, None


def host_is_allowed(host_header, server_port, allowed_hosts=()):
    """Whether the Host header names a host the admin page answers to.

    The loopback names on the admin port always answer, exactly as before.
    The configured names answer too, with or without a port.
    """
    hostname, port = split_host_port(host_header)
    if not hostname:
        return False
    if hostname in LOOPBACK_NAMES and port == server_port:
        return True
    for entry in allowed_hosts or ():
        name, entry_port = split_host_port(entry)
        if name != hostname:
            continue
        if entry_port is None or entry_port == port:
            return True
    return False


def origin_is_allowed(origin, host_header, server_port, allowed_hosts=()):
    """Whether the Origin header belongs to the same place as the Host.

    No Origin header at all is allowed, as before: a request that is not a
    browser's cross-site request carries none. The loopback names keep the
    old, strict rule: plain HTTP on the very same address and port. A
    configured name may be reached over HTTP or HTTPS, because a reverse
    proxy terminates TLS and the browser then sends an https Origin.
    """
    if not origin:
        return True
    hostname, port = split_host_port(host_header)
    if hostname in LOOPBACK_NAMES and port == server_port:
        return origin.strip().lower() == "http://" + (host_header or "").strip().lower()
    scheme, _, rest = origin.strip().lower().partition("://")
    if scheme not in ("http", "https") or not rest:
        return False
    origin_host, origin_port = split_host_port(rest.split("/")[0])
    if origin_host != hostname:
        return False
    # The port the browser reached, which is the scheme's own when none is
    # written, must be the one the Host header names; and a Host with no port
    # means the scheme's own as well. So "dash.lan:8788" does not accept an
    # Origin of "http://dash.lan", nor "proxy.lan" one of "http://proxy.lan:3000".
    default = 443 if scheme == "https" else 80
    reached = origin_port if origin_port is not None else default
    named = port if port is not None else default
    return reached == named


def make_admin_handler(manager, token, allowed_hosts=(), token_generated=False,
                       secure_cookie=False, setup=None):
    if not token:
        raise ValueError("admin API requires a token")
    # A session is bound to the token that made it: change a configured token and
    # the sessions made under the old one end. A generated token is new at every
    # start, so those sessions are bound to "generated" and survive a restart.
    sessions = AdminSessions(manager.storage, binding=token_binding(token, bool(token_generated)))
    allowed = tuple(allowed_hosts or ())

    class AdminHandler(BoundedRequestHandler):
        server_label = "admin"

        def log_message(self, *_):
            pass

        def _send(self, status, body, content_type="application/json", cookie=None,
                  headers=None):
            if content_type == "application/json":
                body = json.dumps(body).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy",
                             "default-src 'none'; script-src 'self'; style-src 'self'; "
                             "connect-src 'self'; frame-ancestors 'none'; "
                             "base-uri 'none'; form-action 'none'")
            for name, value in (headers or {}).items():
                self.send_header(name, value)
            self.send_header("Connection", "close")
            if cookie is not None:
                self.send_header("Set-Cookie", cookie)
            if status == 401:
                self.send_header("WWW-Authenticate", "Bearer")
            self.end_headers()
            self.close_connection = True
            self.wfile.write(body)

        def _allowed_host(self):
            port = self.server.server_address[1]
            host = self.headers.get("Host", "")
            origin = self.headers.get("Origin")
            if not host_is_allowed(host, port, allowed):
                return False
            return origin_is_allowed(origin, host, port, allowed)

        def _cookie_name(self):
            # Cookies have no port isolation; keep separate daemon ports independent.
            # With the Secure flag on, the __Secure- prefix makes the browser refuse
            # the cookie unless it came over HTTPS.
            return "%spresto_admin_session_%d" % ("__Secure-" if secure_cookie else "",
                                                  self.server.server_address[1])

        def _cookie_value(self):
            raw = self.headers.get("Cookie", "")
            if len(raw) > 4096:
                return None
            try:
                cookie = SimpleCookie(raw)
                morsel = cookie.get(self._cookie_name())
                return morsel.value if morsel else None
            except CookieError:
                return None

        def _session_cookie(self, credential=""):
            # The listener itself speaks HTTP, so Secure is off unless the operator
            # says the page is served through HTTPS (admin.secure_cookie): a browser
            # would not send a Secure cookie back over plain HTTP. Host-only,
            # API-only cookies never contain the admin code.
            return ("%s=%s; Path=/api/admin; HttpOnly; SameSite=Strict; Max-Age=%d%s" %
                    (self._cookie_name(), credential, SESSION_SECONDS if credential else 0,
                     "; Secure" if secure_cookie else ""))

        def _authorized(self, bearer_only=False):
            if not self._allowed_host():
                self._send(403, {"error": "origin or host is not allowed"})
                return False
            parts = self.headers.get("Authorization", "").split()
            if parts:
                if (len(parts) == 2 and parts[0].lower() == "bearer" and
                        hmac.compare_digest(parts[1].encode("utf-8"), token.encode("utf-8"))):
                    return True
            elif not bearer_only:
                session = sessions.read(self._cookie_value())
                if session:
                    if self.command != "GET":
                        origin = self.headers.get("Origin")
                        csrf = self.headers.get("X-CSRF-Token", "")
                        host = self.headers.get("Host", "")
                        port = self.server.server_address[1]
                        # A change needs the same place to come from and the
                        # session's own CSRF token, exactly as before.
                        if (not origin or
                                not origin_is_allowed(origin, host, port, allowed) or
                                not hmac.compare_digest(csrf.encode("utf-8"),
                                                        session["csrf_token"].encode("ascii"))):
                            self._send(403, {"error": "reload configuration before making changes"})
                            return False
                    return True
            if parts:
                self.note_failed_auth()
            self._send(401, {"error": "admin token required; browser session absent or expired"})
            return False

        def do_GET(self):
            if not self._allowed_host():
                self._send(403, {"error": "origin or host is not allowed"})
                return
            path = urlsplit(self.path).path
            if path == "/api/admin/session":
                session = sessions.read(self._cookie_value())
                if session:
                    self._send(200, session)
                else:
                    self._send(401, {"error": "enter the current startup admin token to unlock"},
                               cookie=self._session_cookie())
                return
            if path in ASSETS:
                name, content_type = ASSETS[path]
                self._send(200, files("presto_ai_dash").joinpath("web", name).read_bytes(),
                           content_type)
                return
            login_status = re.fullmatch(r"/api/admin/codex-logins/([A-Za-z0-9_-]{1,80})", path)
            if login_status:
                if self._authorized():
                    try:
                        self._send(200, manager.codex_login_status(login_status[1]))
                    except KeyError:
                        self._send(404, {"error": "login not found"})
                    except Exception:
                        log.error("codex login status failed")
                        self._send(500, {"error": "login status could not be read"})
                return
            if path == "/api/admin/collector":
                if self._authorized():
                    self._send(200, collector_bytes(),
                        "text/x-python; charset=utf-8",
                        headers={"Content-Disposition":
                                 'attachment; filename="%s"' % COLLECTOR_FILE})
                return
            if path == SETUP_PATH:
                # Read-only: what the page needs before preparing a download.
                if self._authorized():
                    if setup is None:
                        self._send(200, {"v": 1, "available": False,
                                         "reason": "bundle_unavailable"})
                    else:
                        try:
                            self._send(200, setup.state(self.headers.get("Host", "")))
                        except Exception:
                            log.error("presto setup state failed")
                            self._send(500, {"error": "setup state could not be read"})
                return
            setup_script = re.fullmatch(SETUP_SCRIPT_PATH, path)
            setup_handoff_file = re.fullmatch(SETUP_HANDOFF_FILE_PATH, path)
            if setup_script or setup_handoff_file:
                match = setup_script or setup_handoff_file
                self._setup_download(setup, match[1],
                                     "script" if setup_script else "handoff")
                return
            if path == "/api/admin/ingest":
                if self._authorized():
                    try:
                        self._send(200, manager.ingest_state())
                    except Exception:
                        log.error("admin ingest state failed")
                        self._send(500, {"error": "ingest state could not be read"})
                return
            if path == "/api/admin/demo":
                if self._authorized():
                    try:
                        self._send(200, manager.demo_state())
                    except Exception:
                        log.error("admin demo state failed")
                        self._send(500, {"error": "demo state could not be read"})
                return
            if path == "/api/admin/display":
                if self._authorized():
                    try:
                        self._send(200, manager.display_state())
                    except Exception:
                        log.error("admin display state failed")
                        self._send(500, {"error": "display settings could not be read"})
                return
            if path == "/api/admin/alerts":
                if self._authorized():
                    try:
                        self._send(200, manager.alerts_state())
                    except Exception:
                        log.error("admin alert settings failed")
                        self._send(500, {"error": "alert settings could not be read"})
                return
            service_alerts = re.fullmatch(SERVICE_ALERTS_PATH, path)
            if service_alerts:
                if self._authorized():
                    try:
                        self._send(200, manager.service_alert_state(service_alerts[1]))
                    except KeyError:
                        self._send(404, {"error": "source not found"})
                    except Exception:
                        log.error("admin service alert settings failed")
                        self._send(500, {"error": "service alert settings could not be read"})
                return
            if path == "/api/admin/status":
                # Read-only: the Status tab's daemon, client, reader and
                # problem information. It changes nothing.
                if self._authorized():
                    try:
                        self._send(200, manager.status_view())
                    except Exception:
                        log.error("admin status view failed")
                        self._send(500, {"error": "status could not be read"})
                return
            if path == "/api/admin/devices":
                # Read-only: the durable devices and the attention count.
                if self._authorized():
                    try:
                        self._send(200, manager.devices_view())
                    except Exception:
                        log.error("admin device listing failed")
                        self._send(500, {"error": "device records could not be read"})
                return
            device_detail = re.fullmatch(DEVICE_PATH, path)
            if device_detail:
                # Read-only: one device, with its newest attempt records.
                if self._authorized():
                    try:
                        view = manager.device_view(device_detail[1])
                    except Exception:
                        log.error("admin device detail failed")
                        self._send(500, {"error": "device record could not be read"})
                        return
                    if view is None:
                        self._send(404, {"error": "device not found"})
                    else:
                        self._send(200, view)
                return
            if path == RELEASES_PATH:
                # Read-only: the cached release result and this install's own
                # update commands. No registry is contacted here.
                if self._authorized():
                    try:
                        self._send(200, manager.release_view())
                    except Exception:
                        log.error("admin release view failed")
                        self._send(500, {"error": "release state could not be read"})
                return
            if path == "/api/admin/logs":
                # Read-only: the daemon's recent log lines, newest first, from memory.
                if self._authorized():
                    query = parse_qs(urlsplit(self.path).query)
                    try:
                        self._send(200, manager.log_view(
                            (query.get("limit") or [None])[0],
                            (query.get("level") or ["DEBUG"])[0],
                            (query.get("q") or [""])[0]))
                    except Exception:
                        log.error("admin log view failed")
                        self._send(500, {"error": "the log could not be read"})
                return
            if path == "/api/admin/config":
                # Read-only: the effective configuration and where it came from.
                if self._authorized():
                    try:
                        self._send(200, manager.effective_config())
                    except Exception:
                        log.error("admin effective configuration failed")
                        self._send(500, {"error": "configuration could not be read"})
                return
            if path != "/api/admin/sources":
                self._send(404, {"error": "not found"})
                return
            if self._authorized():
                try:
                    self._send(200, manager.list_sources())
                except Exception:
                    log.error("admin source listing failed")
                    self._send(500, {"error": "configuration could not be read"})

        def _mutation(self, method):
            if not self._authorized():
                return
            path = urlsplit(self.path).path
            match = re.fullmatch(r"/api/admin/sources/([A-Za-z0-9_-]{1,80})", path)
            reorder = method == "PUT" and path == "/api/admin/source-order"
            export = re.fullmatch(r"/api/admin/sources/([A-Za-z0-9_-]{1,80})/claude-export", path)
            ingest_setting = method == "PUT" and path == "/api/admin/ingest"
            demo_setting = method == "PUT" and path == "/api/admin/demo"
            display_setting = method == "PUT" and path == "/api/admin/display"
            alerts_setting = method == "PUT" and path == "/api/admin/alerts"
            service_alerts = method == "PUT" and re.fullmatch(SERVICE_ALERTS_PATH, path)
            if ((method == "POST" and path != "/api/admin/sources" and export is None) or
                    (method != "POST" and match is None and not reorder and
                     not ingest_setting and not demo_setting and not display_setting
                     and not alerts_setting and not service_alerts)):
                self._send(404, {"error": "not found"})
                return
            if (self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json"
                    or self.headers.get("Transfer-Encoding")):
                self._send(415, {"error": "send an application/json body"})
                return
            try:
                length = parse_length(self.headers.get("Content-Length"), "0")
            except ValueError:
                self._send(400, {"error": "invalid body length"})
                return
            if not 2 <= length <= MAX_BODY:
                self._send(413, {"error": "invalid or oversized request body"})
                return
            try:
                data = json.loads(self.rfile.read(length))
            except (ValueError, UnicodeError, RecursionError):
                self._send(400, {"error": "invalid JSON"})
                return
            try:
                if reorder:
                    result = manager.reorder(data)
                elif ingest_setting:
                    result = manager.set_ingest_enabled(data)
                elif demo_setting:
                    result = manager.set_demo_enabled(data)
                elif display_setting:
                    result = manager.set_display(data)
                elif alerts_setting:
                    result = manager.set_alerts(data)
                elif service_alerts:
                    result = manager.set_service_alerts(service_alerts[1], data)
                elif export:
                    result = manager.configure_claude_export(export[1], data)
                elif method == "DELETE":
                    if not isinstance(data, dict) or set(data) != {"revision"}:
                        raise ValueError("delete requires the current revision")
                    manager.delete(match[1], data["revision"])
                    result = {"deleted": True}
                else:
                    result = manager.save(data, match[1] if match else None)
                self._send(201 if method == "POST" else 200, result)
            except ConfigurationConflict as exc:
                self._send(409, {"error": str(exc)})
            except KeyError:
                self._send(404, {"error": "source not found"})
            except ValueError as exc:
                self._send(400, {"error": str(exc)})
            except Exception:
                log.error("admin source update failed")
                self._send(500, {"error": "configuration could not be saved"})

        def _ingest_token(self, method, instance_id):
            """Create/rotate (POST) or revoke (DELETE) one account's push token."""
            if not self._authorized():
                return
            try:
                if method == "POST":
                    self._send(201, manager.create_ingest_token(instance_id))
                else:
                    self._send(200, manager.revoke_ingest_token(instance_id))
            except KeyError:
                self._send(404, {"error": "source not found"})
            except ValueError as exc:
                self._send(400, {"error": str(exc)})
            except Exception:
                log.error("admin ingest token operation failed")
                self._send(500, {"error": "ingest token could not be saved"})

        def _setup_download(self, setup, handoff_id, kind):
            """One of the two files a prepared handoff offers.

            Both are authenticated and uncached. The unlock code is in neither:
            it is shown once in the page and typed into the script by hand.
            """
            if not self._authorized():
                return
            if setup is None:
                self._send(409, {"error": "Presto setup is not available in this build"})
                return
            try:
                if kind == "script":
                    body = setup.script(handoff_id)
                    name, content_type = SCRIPT_NAME, "text/x-python; charset=utf-8"
                else:
                    body = setup.handoff(handoff_id)
                    name, content_type = HANDOFF_NAME, "application/octet-stream"
            except KeyError:
                self._send(404, {"error": "setup download not found; prepare it again"})
                return
            except SetupError as exc:
                self._send(409, {"error": str(exc)})
                return
            except Exception:
                log.error("presto setup download failed")
                self._send(500, {"error": "setup download could not be prepared"})
                return
            self._send(200, body, content_type,
                       headers={"Content-Disposition": 'attachment; filename="%s"' % name})

        def create_setup(self, method):
            """Prepare a handoff (POST) or forget one (DELETE)."""
            if not self._authorized():
                return
            path = urlsplit(self.path).path
            if method == "DELETE":
                handoff = re.fullmatch(SETUP_HANDOFF_PATH, path)
                if not handoff:
                    self._send(404, {"error": "not found"})
                    return
                try:
                    removed = setup.discard(handoff[1]) if setup is not None else False
                except Exception:
                    log.error("presto setup discard failed")
                    self._send(500, {"error": "setup download could not be discarded"})
                    return
                if not removed:
                    self._send(404, {"error": "setup download not found"})
                    return
                self._send(200, {"discarded": True})
                return
            if path != SETUP_PATH:
                self._send(404, {"error": "not found"})
                return
            if (self.headers.get("Content-Type", "").split(";")[0].strip()
                    != "application/json" or self.headers.get("Transfer-Encoding")):
                self._send(415, {"error": "send an application/json body"})
                return
            try:
                length = parse_length(self.headers.get("Content-Length"), "0")
            except ValueError:
                self._send(400, {"error": "invalid body length"})
                return
            if not 2 <= length <= SETUP_BODY_LIMIT:
                self._send(413, {"error": "invalid or oversized request body"})
                return
            try:
                data = json.loads(self.rfile.read(length))
            except (ValueError, UnicodeError, RecursionError):
                self._send(400, {"error": "invalid JSON"})
                return
            if (not isinstance(data, dict) or set(data) - {"host", "port", "name", "wifi"} or
                    not {"host", "port"} <= set(data)):
                self._send(400, {"error": "send the daemon host and port"})
                return
            if setup is None:
                self._send(409, {"error": "Presto setup is not available in this build"})
                return
            try:
                result = setup.create(data["host"], data["port"],
                                      device_name=data.get("name", "presto"),
                                      wifi=data.get("wifi", True))
            except SetupUnavailable as exc:
                self._send(409, {"error": str(exc)})
                return
            except SetupError as exc:
                self._send(400, {"error": str(exc)})
                return
            except Exception:
                log.error("presto setup download could not be prepared")
                self._send(500, {"error": "setup download could not be prepared"})
                return
            self._send(201, result)

        def _device_mutation(self, device_id, kind):
            """A retry or recovery request, refused while the updater is absent.

            Both routes need the same body, CSRF and idempotency treatment as
            any other admin mutation, so the request is validated in full
            first: a malformed request is still 400/413/415. The answer to a
            well-formed one is ``409 ota_not_enabled``, because nothing here
            may authorize a transfer, spend a retry or clear a warning.
            """
            if not self._authorized():
                return
            if (self.headers.get("Content-Type", "").split(";")[0].strip()
                    != "application/json" or self.headers.get("Transfer-Encoding")):
                self._send(415, {"error": "send an application/json body"})
                return
            try:
                length = parse_length(self.headers.get("Content-Length"), "0")
            except ValueError:
                self._send(400, {"error": "invalid body length"})
                return
            if not 2 <= length <= DEVICE_BODY_LIMIT:
                self._send(413, {"error": "invalid or oversized request body"})
                return
            try:
                data = json.loads(self.rfile.read(length))
            except (ValueError, UnicodeError, RecursionError):
                self._send(400, {"error": "invalid JSON"})
                return
            key = self.headers.get("Idempotency-Key", "")
            if (not isinstance(data, dict) or len(key) != IDEMPOTENCY_CHARS or
                    any(char not in HEX_DIGITS for char in key)):
                self._send(400, {"error": "invalid request"})
                return
            try:
                # With the updater disabled this raises 409 ota_not_enabled;
                # with it enabled the coordinator validates the body shape,
                # the idempotency key, the optimistic concurrency value and
                # every gate before it queues anything.
                result = manager.device_mutation(device_id, kind, data, key)
            except AdminRequestError as exc:
                self._send(exc.status, {"error": exc.code})
                return
            except Exception:
                log.error("admin device mutation failed")
                self._send(500, {"error": "device request could not be processed"})
                return
            self._send(202, result)

        def _release_check(self):
            """Queue a background release check. Never contacts a registry here.

            It goes through the same Host, Origin, session or bearer and CSRF
            checks as every admin change. 202 means a check is queued or one
            is already running; 429 means one started within the last minute.
            """
            if not self._authorized():
                return
            try:
                length = parse_length(self.headers.get("Content-Length"), "0")
            except ValueError:
                self._send(400, {"error": "invalid body length"})
                return
            if self.headers.get("Transfer-Encoding") or length > RELEASE_CHECK_BODY_LIMIT:
                self._send(413, {"error": "invalid or oversized request body"})
                return
            if length:
                self.rfile.read(length)
            try:
                outcome, retry_after = manager.request_release_check()
            except Exception:
                log.error("admin release check request failed")
                self._send(500, {"error": "the check could not be queued"})
                return
            if outcome in ("throttled", "rate_limited"):
                # "throttled": a check started within the last minute.
                # "rate_limited": the registry itself asked to be left alone.
                code = "check_throttled" if outcome == "throttled" else "rate_limited"
                self._send(429, {"error": code, "retry_after": retry_after},
                           headers={"Retry-After": str(retry_after)})
            elif outcome == "disabled":
                self._send(409, {"error": "release_checks_disabled"})
            else:
                self._send(202, {"queued": True, "status": outcome})

        def do_POST(self):
            if urlsplit(self.path).path == "/api/admin/session":
                if self._authorized(bearer_only=True):
                    try:
                        credential, session = sessions.create()
                        sessions.revoke(self._cookie_value())
                        self._send(201, session, cookie=self._session_cookie(credential))
                    except Exception:
                        log.error("admin session creation failed")
                        self._send(500, {"error": "browser login could not be saved"})
                return
            path = urlsplit(self.path).path
            login_cancel = re.fullmatch(r"/api/admin/codex-logins/([A-Za-z0-9_-]{1,80})/cancel", path)
            codex_login = re.fullmatch(r"/api/admin/sources/([A-Za-z0-9_-]{1,80})/codex-login", path)
            codex_logout = re.fullmatch(r"/api/admin/sources/([A-Za-z0-9_-]{1,80})/codex-logout", path)
            token_create = re.fullmatch(INGEST_TOKEN_PATH, path)
            device_retry = re.fullmatch(DEVICE_RETRY_PATH, path)
            device_recovery = re.fullmatch(DEVICE_RECOVERY_PATH, path)
            if path == RELEASES_CHECK_PATH:
                self._release_check()
                return
            if device_retry or device_recovery:
                self._device_mutation((device_retry or device_recovery)[1],
                                      "retry" if device_retry else "confirm-recovery")
                return
            if path == SETUP_PATH:
                # Prepare the two setup downloads and the one-time unlock code.
                self.create_setup("POST")
                return
            if token_create:
                self._ingest_token("POST", token_create[1])
                return
            if login_cancel or codex_login or codex_logout:
                if not self._authorized():
                    return
                # read body
                if (self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json"
                        or self.headers.get("Transfer-Encoding")):
                    self._send(415, {"error": "send an application/json body"})
                    return
                try:
                    length = parse_length(self.headers.get("Content-Length"), "0")
                except ValueError:
                    self._send(400, {"error": "invalid body length"})
                    return
                if not 2 <= length <= MAX_BODY:
                    self._send(413, {"error": "invalid or oversized request body"})
                    return
                try:
                    data = json.loads(self.rfile.read(length))
                except (ValueError, UnicodeError, RecursionError):
                    self._send(400, {"error": "invalid JSON"})
                    return
                try:
                    if login_cancel:
                        result = manager.cancel_codex_login(login_cancel[1])
                    elif codex_login:
                        result = manager.start_codex_login(codex_login[1], data)
                    else:
                        result = manager.logout_codex(codex_logout[1])
                    self._send(200, result)
                except ConfigurationConflict as exc:
                    self._send(409, {"error": str(exc)})
                except KeyError:
                    self._send(404, {"error": "source or login not found"})
                except ValueError as exc:
                    self._send(400, {"error": str(exc)})
                except (CodexLoginError, CodexReportingError) as exc:
                    # Fixed, secret-free messages such as "Install Codex or set its
                    # executable path in source configuration": the page shows them.
                    log.warning("codex login operation refused: %s", exc)
                    self._send(400, {"error": str(exc)})
                except Exception:
                    log.error("codex login operation failed")
                    self._send(500, {"error": "codex login operation failed"})
                return
            self._mutation("POST")

        def do_PUT(self):
            self._mutation("PUT")

        def do_DELETE(self):
            if urlsplit(self.path).path == "/api/admin/session":
                if self._authorized():
                    sessions.revoke(self._cookie_value())
                    self._send(200, {"logged_out": True}, cookie=self._session_cookie())
                return
            if urlsplit(self.path).path == "/api/admin/sessions":
                # Every browser, this one included. The way to end a session an
                # attacker made, when the token that made it was a generated one.
                if self._authorized():
                    count = sessions.revoke_all()
                    log.warning("every admin browser session was ended (%d)", count)
                    self._send(200, {"signed_out": count}, cookie=self._session_cookie())
                return
            if re.fullmatch(SETUP_HANDOFF_PATH, urlsplit(self.path).path):
                # Forget a prepared handoff; the files already downloaded stay
                # valid until they are used.
                self.create_setup("DELETE")
                return
            token_revoke = re.fullmatch(INGEST_TOKEN_PATH, urlsplit(self.path).path)
            if token_revoke:
                self._ingest_token("DELETE", token_revoke[1])
                return
            self._mutation("DELETE")

    return AdminHandler


class AdminServer(ApiServer):
    def __init__(self, port, manager, token, host="127.0.0.1", allowed_hosts=(),
                 token_generated=False, secure_cookie=False, setup=None):
        super().__init__(host, port, manager.storage, token,
                         handler_class=make_admin_handler(manager, token, allowed_hosts,
                                                          token_generated, secure_cookie,
                                                          setup))
