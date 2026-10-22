"""A minimal HTTP client for talking to sources.

Uses ``urllib`` from the standard library. It exists so every source shares
one timeout policy, one error taxonomy and one JSON path, and so tests can
substitute a transport without a network.
"""

import json
import urllib.error
import urllib.request
from urllib.parse import urlsplit


class HttpError(Exception):
    """The server answered with a non-2xx status."""

    def __init__(self, status, body=""):
        self.status = status
        self.body = body
        super().__init__("HTTP %s: %s" % (status, body[:200]))


class HttpUnreachable(Exception):
    """The server could not be reached at all."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        return None


_DEFAULT_PORTS = {"http": 80, "https": 443}


def origin_of(url):
    """The scheme, host and port of a URL, so two spellings compare equal."""
    parts = urlsplit(url or "")
    scheme = parts.scheme.lower()
    try:
        port = parts.port or _DEFAULT_PORTS.get(scheme)
    except ValueError:
        port = None
    return (scheme, (parts.hostname or "").lower(), port)


def redirect_allowed(old_url, new_url):
    """Whether a redirect stays on the host that was configured.

    urllib copies every request header, an ``Authorization`` key included, onto
    the redirected request, whatever host it points at. So a redirect is only
    followed to the same host and port, or from http to https on the same
    host. Never to another host, never from https back to http, and never to
    a scheme other than http and https.
    """
    old, new = urlsplit(old_url), urlsplit(new_url)
    if old.scheme not in _DEFAULT_PORTS or new.scheme not in _DEFAULT_PORTS:
        return False
    if not old.hostname or old.hostname.lower() != (new.hostname or "").lower():
        return False
    try:
        old_port = old.port or _DEFAULT_PORTS[old.scheme]
        new_port = new.port or _DEFAULT_PORTS[new.scheme]
    except ValueError:
        return False
    if old.scheme == new.scheme:
        return old_port == new_port
    return old.scheme == "http" and new.scheme == "https" and new_port in (443, old_port)


class _SameHostRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        if not redirect_allowed(request.full_url, new_url):
            return None
        return super().redirect_request(request, fp, code, message, headers, new_url)


_FOLLOWING = urllib.request.build_opener(_SameHostRedirect())
_REFUSING = urllib.request.build_opener(_NoRedirect())


def _open(request, timeout, allow_redirects=True):
    """Open a request; a redirect is followed only as ``redirect_allowed`` says."""
    return (_FOLLOWING if allow_redirects else _REFUSING).open(request, timeout=timeout)


def get_json(url, timeout=5, headers=None, transport=None, allow_redirects=True):
    """GET a URL and parse the JSON body.

    Raises ``HttpError`` on a non-2xx status and ``HttpUnreachable`` when the
    host cannot be reached or the body is not JSON.
    """
    if transport is not None:
        return transport(url, timeout, headers or {})

    request = urllib.request.Request(url, method="GET")
    for key, value in (headers or {}).items():
        request.add_header(key, value)

    try:
        with _open(request, timeout, allow_redirects) as response:
            status = getattr(response, "status", None) or response.getcode()
            raw = response.read()
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read().decode("utf-8", "replace")
        except Exception:  # pragma: no cover - defensive
            body = ""
        raise HttpError(exc.code, body)
    except urllib.error.URLError as exc:
        raise HttpUnreachable(str(exc.reason))
    except OSError as exc:
        raise HttpUnreachable(str(exc))

    if not 200 <= status < 300:
        raise HttpError(status, raw.decode("utf-8", "replace"))

    return _decode(raw)


def get_text(url, timeout=5, headers=None, transport=None):
    """GET a successful response as UTF-8 text without requiring JSON."""
    if transport is not None:
        return transport(url, timeout, headers or {})

    request = urllib.request.Request(url, method="GET")
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    try:
        with _open(request, timeout) as response:
            status = getattr(response, "status", None) or response.getcode()
            raw = response.read()
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read().decode("utf-8", "replace")
        except Exception:  # pragma: no cover - defensive
            body = ""
        raise HttpError(exc.code, body)
    except urllib.error.URLError as exc:
        raise HttpUnreachable(str(exc.reason))
    except OSError as exc:
        raise HttpUnreachable(str(exc))
    if not 200 <= status < 300:
        raise HttpError(status, raw.decode("utf-8", "replace"))
    return raw.decode("utf-8", "replace")


def post_json(url, payload, timeout=5, headers=None, transport=None):
    """POST a JSON body and parse the JSON response."""
    if transport is not None:
        return transport(url, timeout, headers or {}, payload)

    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=body, method="POST")
    request.add_header("Content-Type", "application/json")
    for key, value in (headers or {}).items():
        request.add_header(key, value)

    try:
        with _open(request, timeout) as response:
            status = getattr(response, "status", None) or response.getcode()
            raw = response.read()
    except urllib.error.HTTPError as exc:
        try:
            text = exc.read().decode("utf-8", "replace")
        except Exception:  # pragma: no cover - defensive
            text = ""
        raise HttpError(exc.code, text)
    except urllib.error.URLError as exc:
        raise HttpUnreachable(str(exc.reason))
    except OSError as exc:
        raise HttpUnreachable(str(exc))

    if not 200 <= status < 300:
        raise HttpError(status, raw.decode("utf-8", "replace"))

    return _decode(raw)


def _decode(raw):
    """Decode a JSON body, raising ``HttpUnreachable`` if it is not JSON."""
    if isinstance(raw, bytes):
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise HttpUnreachable("response was not UTF-8: %s" % exc)
    try:
        return json.loads(raw)
    except ValueError as exc:
        raise HttpUnreachable("response was not valid JSON: %s" % exc)


def join_url(base, path):
    """Join a base URL and a path without doubling or dropping slashes."""
    if not path.startswith("/"):
        path = "/" + path
    return base.rstrip("/") + path
