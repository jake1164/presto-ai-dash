"""HTTP client for the Presto app.

The live app uses bounded asyncio streams so touch/rendering can continue
during network waits. The synchronous requests wrapper is retained for smoke
checks and existing transport tests. Both send the same shared bearer token.

Every request also says which client it is: ``X-Presto-Client`` carries the
device's name (``DEVICE_NAME`` in the device's own ``config.py``, "presto" by
default) and ``X-Presto-Revision`` the app revision it runs. The daemon keeps
both in memory to show who is connected; it never stores them.

MicroPython's ``requests`` supports custom headers (verified in
micropython-lib ``python-ecosys/requests``), which is what lets the Presto send
the shared bearer token the daemon requires.
"""

import gc
import json
import sys

# The name and the revision this client reports. The preview is not a device,
# so it overrides them.
DEFAULT_CLIENT_NAME = "presto"
DEFAULT_REVISION = "unknown"
CLIENT_NAME_OVERRIDE = None
REVISION_OVERRIDE = None

# The deployment writes the app revision next to the app files, so the device
# reports the build it actually runs.
REVISION_FILE = "revision.txt"
REVISION_FILES = (REVISION_FILE, "/ai_dash_files/revision.txt")
MAX_LABEL = 32


def _collect():
    gc.collect()


class StatusError(Exception):
    """The daemon answered, but not with something we can use."""


class NetworkUnavailable(Exception):
    """The daemon could not be reached at all."""


def describe_error(exc, stage=None):
    """A short ASCII reason for a failed fetch, safe to show on the screen.

    Only the exception's type, an integer errno and the stage are used, never
    its text, which can carry addresses or response contents.
    """
    if isinstance(exc, NetworkUnavailable):
        text = str(exc.args[0]) if exc.args else "network"
    else:
        text = type(exc).__name__
        args = getattr(exc, "args", None)
        if isinstance(exc, OSError) and args and type(args[0]) is int:
            text += " %d" % args[0]
    if stage:
        text += " @" + stage
    return "".join(ch if " " <= ch <= "~" else "?" for ch in text)[:30]


class Unauthorised(Exception):
    """The daemon rejected our token (401)."""


def build_url(host, port, path="/api/v1/status"):
    """Build the status URL. ``path`` must start with a slash."""
    if not path.startswith("/"):
        path = "/" + path
    return "http://{}:{}{}".format(host, port, path)


# The device asks for the small summary: the card's lines and tiles are
# fetched when the card is opened.
LAZY_QUERY = "?cards=lazy"
# The answer to a status fetch: the whole payload.
STATUS_RESPONSE_LIMIT = 65536
# The answer to a card fetch: one card's tiles and lines.
CARD_RESPONSE_LIMIT = 8192


# The device's only write: one service's alerts on or off. The service id
# goes into the path, so it must be the plain identifier the daemon uses.
SAFE_SERVICE_CHARS = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
MAX_SERVICE_CHARS = 80
# The answer is a small JSON object; a bigger one is not what we asked for.
ALERT_RESPONSE_LIMIT = 1024


def safe_service_id(service):
    """The service id as it may appear in a path, or None when unusable."""
    if not isinstance(service, str) or not service or len(service) > MAX_SERVICE_CHARS:
        return None
    for char in service:
        if char not in SAFE_SERVICE_CHARS:
            return None
    return service


def safe_label(value, limit=MAX_LABEL):
    """A printable ASCII label of at most ``limit`` characters.

    Header values are untrusted, so anything else becomes "unknown" rather
    than a partly-trusted string.
    """
    if not isinstance(value, str) or not value:
        return "unknown"
    if len(value) > limit:
        return "unknown"
    for char in value:
        if not (" " <= char <= "~"):
            return "unknown"
    text = value.strip()
    return text or "unknown"


def device_setting(name, default):
    """One setting from the device's own ``config.py``, when it is loaded.

    The app's config module holds the daemon address and the shared token;
    only the display name and the revision are read here.
    """
    module = sys.modules.get("config")
    if module is None:
        return default
    value = getattr(module, name, None)
    return default if value is None else value


def read_revision(path=None):
    """The app revision installed with this build, or "unknown".

    The deployment writes the revision next to the app files. A missing file
    is not an error: the device then says "unknown" instead of a revision it
    cannot prove.
    """
    for candidate in ((path,) if path else REVISION_FILES):
        try:
            with open(candidate, "r") as handle:
                text = handle.read().strip()
        except OSError:
            continue
        except Exception:
            continue
        return safe_label(text)
    return DEFAULT_REVISION


def current_client_name():
    """The name this client reports to the daemon."""
    if CLIENT_NAME_OVERRIDE:
        return safe_label(CLIENT_NAME_OVERRIDE)
    return safe_label(device_setting("DEVICE_NAME", DEFAULT_CLIENT_NAME))


def current_revision():
    """The app revision this client reports to the daemon."""
    if REVISION_OVERRIDE:
        return safe_label(REVISION_OVERRIDE)
    configured = device_setting("DEVICE_REVISION", None)
    if configured:
        return safe_label(configured)
    return read_revision()


def build_headers(token, client=None, revision=None):
    """The request headers, including the shared bearer token.

    An empty token is still sent as an empty bearer so the daemon's error is a
    clean 401 rather than a confusing connection failure. Every request also
    names the client and the app revision it runs.
    """
    return {"Authorization": "Bearer {}".format(token or ""),
            "X-Presto-Client": safe_label(client) if client else current_client_name(),
            "X-Presto-Revision": safe_label(revision) if revision else current_revision()}


def parse_payload(text):
    """Parse a JSON payload into a dict.

    Raises ``StatusError`` if the body is not a JSON object. A list or a bare
    string is rejected: the app expects an object with a ``cards`` key.
    """
    try:
        data = json.loads(text)
    except ValueError:
        # Parser exceptions may include response contents. Keep diagnostics safe.
        raise StatusError("response was not valid JSON")
    if not isinstance(data, dict):
        raise StatusError("response was not a JSON object")
    return data


class StatusClient(object):
    """Fetches the status payload from the daemon.

    ``transport`` is a callable taking ``(url, headers, timeout)`` and
    returning an object with ``status_code`` and ``text``. It defaults to
    MicroPython's ``requests.get``. Tests pass a fake.
    """

    def __init__(self, host, port, token, timeout=8,
                 path="/api/v1/status", transport=None):
        self.host = host
        self.port = port
        self.token = token
        self.timeout = timeout
        self.path = path
        self._transport = transport
        # Where the last async fetch got to and why it ended; the screen and
        # the serial log report these when the daemon looks unreachable.
        self.stage = "start"
        self.received = 0

    @property
    def url(self):
        return build_url(self.host, self.port, self.path)

    @property
    def headers(self):
        return build_headers(self.token)

    def _default_transport(self, url, headers, timeout):
        import requests
        return requests.get(url, headers=headers, timeout=timeout)

    def fetch(self):
        """Return the parsed payload dict.

        Raises ``Unauthorised`` on 401, ``StatusError`` on any other non-200
        or unparseable body, and ``NetworkUnavailable`` when the daemon cannot
        be reached at all.
        """
        transport = self._transport or self._default_transport
        url = self.url
        headers = self.headers

        try:
            response = transport(url, headers, self.timeout)
        except OSError as exc:
            raise NetworkUnavailable("cannot reach daemon: {}".format(exc))
        except Exception as exc:  # MicroPython raises bare Exception sometimes
            raise NetworkUnavailable("request failed: {}".format(exc))

        try:
            status_code = getattr(response, "status_code", None)
            if status_code is None:
                raise StatusError("response had no status code")
            if status_code == 401:
                raise Unauthorised("daemon rejected the shared token")
            if status_code != 200:
                raise StatusError("daemon returned HTTP {}".format(status_code))
            try:
                text = response.text
            except Exception as exc:
                raise StatusError("could not read response body: {}".format(exc))
            return parse_payload(text)
        finally:
            close = getattr(response, "close", None)
            if close is not None:
                try:
                    close()
                except Exception:
                    pass

    async def fetch_async(self):
        """Bounded HTTP GET using asyncio streams, keeping touch input responsive.

        This speaks the daemon's Content-Length JSON contract. Redirects and
        chunked responses are rejected; credentials are never forwarded.
        """
        return await self._fetch_path(self.path, STATUS_RESPONSE_LIMIT)

    async def fetch_card(self, card_id):
        """Fetch one card's lazy detail: its tiles and its lines.

        The same contract as ``fetch_async``: bounded asyncio streams, the
        same stages and reason strings, no redirect, no chunked body and a
        capped answer. It is a read: nothing is changed on the daemon.
        """
        identifier = safe_service_id(card_id)
        if identifier is None:
            raise StatusError("invalid card id")
        return await self._fetch_path("/api/v1/cards/" + identifier,
                                      CARD_RESPONSE_LIMIT)

    async def _fetch_path(self, path, limit):
        import asyncio
        try:
            return await asyncio.wait_for(self._fetch_stream(path, limit),
                                          self.timeout)
        except (Unauthorised, StatusError):
            raise
        except Exception as exc:
            error = NetworkUnavailable("cannot reach daemon")
            error.reason = describe_error(exc, self.stage)
            raise error

    async def _fetch_stream(self, path=None, limit=STATUS_RESPONSE_LIMIT):
        import asyncio
        writer = None
        self.stage = "conn"
        self.received = 0
        try:
            path = path if path is not None else self.path
            if not path.startswith("/"):
                path = "/" + path
            headers = self.headers
            if any(char in str(value) for value in (self.host, self.token, path)
                   for char in ("\r", "\n")):
                raise StatusError("invalid connection settings")
            reader, writer = await asyncio.open_connection(self.host, self.port)
            request = ("GET %s HTTP/1.1\r\nHost: %s:%s\r\n"
                       "Authorization: %s\r\nX-Presto-Client: %s\r\n"
                       "X-Presto-Revision: %s\r\nConnection: close\r\n\r\n" %
                       (path, self.host, self.port, headers["Authorization"],
                        headers["X-Presto-Client"], headers["X-Presto-Revision"]))
            self.stage = "send"
            writer.write(request.encode("utf-8"))
            await writer.drain()
            self.stage = "head"

            header = b""
            while b"\r\n\r\n" not in header:
                chunk = await reader.read(256)
                if not chunk:
                    raise NetworkUnavailable("short head")
                header += chunk
                if len(header) > 4096:
                    raise StatusError("response headers too large")
            header, body = header.split(b"\r\n\r\n", 1)
            lines = header.split(b"\r\n")
            try:
                status = int(lines[0].split()[1])
                headers = {}
                for line in lines[1:]:
                    key, value = line.split(b":", 1)
                    key = key.strip().lower()
                    if key in headers:
                        raise ValueError
                    headers[key] = value.strip()
                length = int(headers.get(b"content-length", b"-1"))
            except (ValueError, IndexError):
                raise StatusError("invalid HTTP response")
            if status == 401:
                raise Unauthorised("daemon rejected the shared token")
            if status != 200:
                raise StatusError("daemon returned HTTP %s" % status)
            if not 0 <= length <= limit or b"transfer-encoding" in headers:
                raise StatusError("unsupported response size or framing")
            self.stage = "body"
            self.received = len(body)
            while len(body) < length:
                chunk = await reader.read(min(1024, length - len(body)))
                if not chunk:
                    raise NetworkUnavailable("short body")
                body += chunk
                self.received = len(body)
            if len(body) != length:
                raise StatusError("invalid response length")
            try:
                text = body.decode("utf-8")
            except UnicodeError:
                raise StatusError("response was not UTF-8")
            self.stage = "parse"
            body = header = None
            _collect()
            return parse_payload(text)
        finally:
            if writer is not None:
                try:
                    writer.close()
                    await writer.wait_closed()
                except OSError:
                    pass


    async def post_alert(self, service, enabled):
        """Set one service's alerts on or off; return the daemon's answer.

        The same contract as ``fetch_async``: bounded asyncio streams, no
        redirect, no chunked body, a capped answer and the same exceptions.
        The body is exactly ``{"enabled": true|false}``: it sets a state and
        never toggles, so a double tap or a retry is harmless.

        Raises ``Unauthorised`` on 401, ``StatusError`` on any other status
        or an unusable body, and ``NetworkUnavailable`` when the daemon
        cannot be reached at all.
        """
        import asyncio
        try:
            return await asyncio.wait_for(self._post_alert_stream(service, enabled),
                                          self.timeout)
        except (Unauthorised, StatusError):
            raise
        except Exception as exc:
            error = NetworkUnavailable("cannot reach daemon")
            error.reason = describe_error(exc, self.stage)
            raise error

    async def _post_alert_stream(self, service, enabled):
        import asyncio
        writer = None
        self.stage = "conn"
        self.received = 0
        identifier = safe_service_id(service)
        if identifier is None:
            raise StatusError("invalid service id")
        if type(enabled) is not bool:
            raise StatusError("enabled must be true or false")
        path = "/api/v1/alerts/" + identifier
        try:
            headers = self.headers
            if any(char in str(value) for value in (self.host, self.token, path)
                   for char in ("\r", "\n")):
                raise StatusError("invalid connection settings")
            body = json.dumps({"enabled": enabled})
            reader, writer = await asyncio.open_connection(self.host, self.port)
            request = ("POST %s HTTP/1.1\r\nHost: %s:%s\r\n"
                       "Authorization: %s\r\nX-Presto-Client: %s\r\n"
                       "X-Presto-Revision: %s\r\nContent-Type: application/json\r\n"
                       "Content-Length: %d\r\nConnection: close\r\n\r\n%s" %
                       (path, self.host, self.port, headers["Authorization"],
                        headers["X-Presto-Client"], headers["X-Presto-Revision"],
                        len(body), body))
            self.stage = "send"
            writer.write(request.encode("utf-8"))
            await writer.drain()
            self.stage = "head"

            header = b""
            while b"\r\n\r\n" not in header:
                chunk = await reader.read(256)
                if not chunk:
                    raise NetworkUnavailable("short head")
                header += chunk
                if len(header) > 4096:
                    raise StatusError("response headers too large")
            header, body_bytes = header.split(b"\r\n\r\n", 1)
            lines = header.split(b"\r\n")
            try:
                status = int(lines[0].split()[1])
                response_headers = {}
                for line in lines[1:]:
                    key, value = line.split(b":", 1)
                    key = key.strip().lower()
                    if key in response_headers:
                        raise ValueError
                    response_headers[key] = value.strip()
                length = int(response_headers.get(b"content-length", b"-1"))
            except (ValueError, IndexError):
                raise StatusError("invalid HTTP response")
            if status == 401:
                raise Unauthorised("daemon rejected the shared token")
            if status != 200:
                raise StatusError("daemon returned HTTP %s" % status)
            if not 0 <= length <= ALERT_RESPONSE_LIMIT or b"transfer-encoding" in response_headers:
                raise StatusError("unsupported response size or framing")
            self.stage = "body"
            self.received = len(body_bytes)
            while len(body_bytes) < length:
                chunk = await reader.read(min(512, length - len(body_bytes)))
                if not chunk:
                    raise NetworkUnavailable("short body")
                body_bytes += chunk
                self.received = len(body_bytes)
            if len(body_bytes) != length:
                raise StatusError("invalid response length")
            try:
                text = body_bytes.decode("utf-8")
            except UnicodeError:
                raise StatusError("response was not UTF-8")
            self.stage = "parse"
            body_bytes = header = None
            _collect()
            return parse_payload(text)
        finally:
            if writer is not None:
                try:
                    writer.close()
                    await writer.wait_closed()
                except OSError:
                    pass


def load_static_payload(path):
    """Load a payload from a file on the Presto's filesystem.

    Used by the static demo mode, where the device shows sample data without a
    daemon. Raises ``StatusError`` if the file is missing or unparseable.
    """
    try:
        with open(path, "r") as handle:
            text = handle.read()
    except OSError as exc:
        raise StatusError("cannot read {}: {}".format(path, exc))
    return parse_payload(text)


EMBEDDED_SAMPLE = """{
  "app": "presto-ai-dash",
  "version": "0.1.0",
  "updated_at": "2026-10-09T11:00:00-04:00",
  "overall_status": "ok",
  "cards": [
    {
      "id": "dash",
      "title": "AI Dash",
      "status": "ok",
      "primary": "128k tokens today",
      "secondary": "Ollama active - LiteLLM OK",
      "detail": [
        "LiteLLM: 42 requests today",
        "Ollama: 38 tok/s last run",
        "Alerts: none"
      ],
      "accent": "green",
      "alert": null
    },
    {
      "id": "ollama",
      "title": "Ollama",
      "status": "active",
      "primary": "38 tok/s",
      "secondary": "qwen model - last run 2m ago",
      "detail": [
        "Reachable: yes",
        "Prompt tokens: 812",
        "Output tokens: 1944",
        "Duration: 51s"
      ],
      "accent": "blue",
      "alert": null
    },
    {
      "id": "litellm",
      "title": "LiteLLM",
      "status": "ok",
      "primary": "42 requests",
      "secondary": "128k tokens today",
      "detail": [
        "Spend today: unavailable",
        "Top model: unavailable",
        "Errors: 0"
      ],
      "accent": "green",
      "alert": null
    }
  ]
}"""


def embedded_payload():
    """The built-in sample payload, for a first run with no files at all."""
    return parse_payload(EMBEDDED_SAMPLE)
