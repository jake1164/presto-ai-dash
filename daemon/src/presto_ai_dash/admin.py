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
ASSETS = {"/": ("index.html", "text/html; charset=utf-8"),
          "/admin.js": ("admin.js", "text/javascript; charset=utf-8"),
          "/admin.css": ("admin.css", "text/css; charset=utf-8")}


def make_admin_handler(manager, token):
    if not token:
        raise ValueError("admin API requires a token")
    sessions = AdminSessions(manager.storage)

    class AdminHandler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(3.0)

        def log_message(self, *_):
            pass

        def _send(self, status, body, content_type="application/json", cookie=None):
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
            hosts = {"127.0.0.1:%d" % port, "localhost:%d" % port}
            host = self.headers.get("Host", "").lower()
            origin = self.headers.get("Origin")
            return host in hosts and (origin is None or origin == "http://" + host)

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
                        if (origin != "http://" + self.headers.get("Host", "").lower() or
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
            if ((method == "POST" and path != "/api/admin/sources" and export is None) or
                    (method != "POST" and match is None and not reorder)):
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
            self._mutation("DELETE")

    return AdminHandler


class AdminServer(ApiServer):
    def __init__(self, port, manager, token):
        super().__init__("127.0.0.1", port, manager.storage, token,
                         handler_class=make_admin_handler(manager, token))
