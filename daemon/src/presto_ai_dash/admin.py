"""Loopback-only admin HTTP surface; the device token cannot edit credentials."""

import hmac
from http.server import BaseHTTPRequestHandler
from importlib.resources import files
import json
import logging
import re
from urllib.parse import urlsplit

from .api import ApiServer
from .storage import ConfigurationConflict

log = logging.getLogger(__name__)
MAX_BODY = 65536
ASSETS = {"/": ("index.html", "text/html; charset=utf-8"),
          "/admin.js": ("admin.js", "text/javascript; charset=utf-8"),
          "/admin.css": ("admin.css", "text/css; charset=utf-8")}


def make_admin_handler(manager, token):
    if not token:
        raise ValueError("admin API requires a token")

    class AdminHandler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(3.0)

        def log_message(self, *_):
            pass

        def _send(self, status, body, content_type="application/json"):
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
            return host in hosts and (origin is None or
                                      origin in {"http://" + name for name in hosts})

        def _authorized(self):
            if not self._allowed_host():
                self._send(403, {"error": "origin or host is not allowed"})
                return False
            parts = self.headers.get("Authorization", "").split()
            if (len(parts) != 2 or parts[0].lower() != "bearer" or
                    not hmac.compare_digest(parts[1].encode("utf-8"), token.encode("utf-8"))):
                self._send(401, {"error": "admin token required"})
                return False
            return True

        def do_GET(self):
            if not self._allowed_host():
                self._send(403, {"error": "origin or host is not allowed"})
                return
            path = urlsplit(self.path).path
            if path in ASSETS:
                name, content_type = ASSETS[path]
                self._send(200, files("presto_ai_dash").joinpath("web", name).read_bytes(),
                           content_type)
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
            if ((method == "POST" and path != "/api/admin/sources") or
                    (method != "POST" and match is None)):
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
                if method == "DELETE":
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
            self._mutation("POST")

        def do_PUT(self):
            self._mutation("PUT")

        def do_DELETE(self):
            self._mutation("DELETE")

    return AdminHandler


class AdminServer(ApiServer):
    def __init__(self, port, manager, token):
        super().__init__("127.0.0.1", port, manager.storage, token,
                         handler_class=make_admin_handler(manager, token))
