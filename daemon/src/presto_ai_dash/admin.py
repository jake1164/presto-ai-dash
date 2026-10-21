"""Loopback-only admin HTTP surface; the device token cannot edit credentials."""

import hmac
from http.server import BaseHTTPRequestHandler
from http.cookies import CookieError, SimpleCookie
from importlib.resources import files
import json
import logging
import re
from urllib.parse import urlsplit

from .api import ApiServer
from .admin_sessions import AdminSessions, SESSION_SECONDS
from .storage import ConfigurationConflict

log = logging.getLogger(__name__)
MAX_BODY = 65536
COLLECTOR_FILE = "claude_statusline.py"
INGEST_TOKEN_PATH = r"/api/admin/sources/([A-Za-z0-9_-]{1,80})/ingest-token"
SERVICE_ALERTS_PATH = r"/api/admin/sources/([A-Za-z0-9_-]{1,80})/alerts"
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
    if origin_port is not None and port is not None and origin_port != port:
        return False
    return True


def make_admin_handler(manager, token, allowed_hosts=()):
    if not token:
        raise ValueError("admin API requires a token")
    sessions = AdminSessions(manager.storage)
    allowed = tuple(allowed_hosts or ())

    class AdminHandler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(3.0)

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
            return "presto_admin_session_%d" % self.server.server_address[1]

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
            # This listener is HTTP on loopback only, so Secure is intentionally
            # omitted. Host-only, API-only cookies never contain the admin code.
            return ("%s=%s; Path=/api/admin; HttpOnly; SameSite=Strict; Max-Age=%d" %
                    (self._cookie_name(), credential, SESSION_SECONDS if credential else 0))

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
                    self._send(200, files("presto_ai_dash").joinpath(
                        "collectors", COLLECTOR_FILE).read_bytes(),
                        "text/x-python; charset=utf-8",
                        headers={"Content-Disposition":
                                 'attachment; filename="%s"' % COLLECTOR_FILE})
                return
            if path == "/api/admin/ingest":
                if self._authorized():
                    try:
                        self._send(200, manager.ingest_state())
                    except Exception:
                        log.error("admin ingest state failed")
                        self._send(500, {"error": "ingest state could not be read"})
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
            display_setting = method == "PUT" and path == "/api/admin/display"
            alerts_setting = method == "PUT" and path == "/api/admin/alerts"
            service_alerts = method == "PUT" and re.fullmatch(SERVICE_ALERTS_PATH, path)
            if ((method == "POST" and path != "/api/admin/sources" and export is None) or
                    (method != "POST" and match is None and not reorder and
                     not ingest_setting and not display_setting and not alerts_setting
                     and not service_alerts)):
                self._send(404, {"error": "not found"})
                return
            if (self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json"
                    or self.headers.get("Transfer-Encoding")):
                self._send(415, {"error": "send an application/json body"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                self._send(400, {"error": "invalid body length"})
                return
            if not 2 <= length <= MAX_BODY:
                self._send(413, {"error": "invalid or oversized request body"})
                return
            try:
                data = json.loads(self.rfile.read(length))
            except (ValueError, UnicodeError):
                self._send(400, {"error": "invalid JSON"})
                return
            try:
                if reorder:
                    result = manager.reorder(data)
                elif ingest_setting:
                    result = manager.set_ingest_enabled(data)
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
                    length = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    self._send(400, {"error": "invalid body length"})
                    return
                if not 2 <= length <= MAX_BODY:
                    self._send(413, {"error": "invalid or oversized request body"})
                    return
                try:
                    data = json.loads(self.rfile.read(length))
                except (ValueError, UnicodeError):
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
            token_revoke = re.fullmatch(INGEST_TOKEN_PATH, urlsplit(self.path).path)
            if token_revoke:
                self._ingest_token("DELETE", token_revoke[1])
                return
            self._mutation("DELETE")

    return AdminHandler


class AdminServer(ApiServer):
    def __init__(self, port, manager, token, host="127.0.0.1", allowed_hosts=()):
        super().__init__(host, port, manager.storage, token,
                         handler_class=make_admin_handler(manager, token, allowed_hosts))
