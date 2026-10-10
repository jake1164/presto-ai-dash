"""A minimal HTTP client for talking to sources.

Uses ``urllib`` from the standard library. It exists so every source shares
one timeout policy, one error taxonomy and one JSON path, and so tests can
substitute a transport without a network.
"""

import json
import urllib.error
import urllib.request


class HttpError(Exception):
    """The server answered with a non-2xx status."""

    def __init__(self, status, body=""):
        self.status = status
        self.body = body
        super().__init__("HTTP %s: %s" % (status, body[:200]))


class HttpUnreachable(Exception):
    """The server could not be reached at all."""


def get_json(url, timeout=5, headers=None, transport=None):
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
        with urllib.request.urlopen(request, timeout=timeout) as response:
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
        with urllib.request.urlopen(request, timeout=timeout) as response:
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
