"""HTTP client for the Presto app.

The live app uses bounded asyncio streams so touch/rendering can continue
during network waits. The synchronous requests wrapper is retained for smoke
checks and existing transport tests. Both send the same shared bearer token.

MicroPython's ``requests`` supports custom headers (verified in
micropython-lib ``python-ecosys/requests``), which is what lets the Presto send
the shared bearer token the daemon requires.
"""

import json


class StatusError(Exception):
    """The daemon answered, but not with something we can use."""


class NetworkUnavailable(Exception):
    """The daemon could not be reached at all."""


class Unauthorised(Exception):
    """The daemon rejected our token (401)."""


def build_url(host, port, path="/api/v1/status"):
    """Build the status URL. ``path`` must start with a slash."""
    if not path.startswith("/"):
        path = "/" + path
    return "http://{}:{}{}".format(host, port, path)


def build_headers(token):
    """The request headers, including the shared bearer token.

    An empty token is still sent as an empty bearer so the daemon's error is a
    clean 401 rather than a confusing connection failure.
    """
    return {"Authorization": "Bearer {}".format(token or "")}


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
        import asyncio
        try:
            return await asyncio.wait_for(self._fetch_stream(), self.timeout)
        except (Unauthorised, StatusError):
            raise
        except Exception:
            raise NetworkUnavailable("cannot reach daemon")

    async def _fetch_stream(self):
        import asyncio
        writer = None
        try:
            path = self.path if self.path.startswith("/") else "/" + self.path
            if any(char in str(value) for value in (self.host, self.token, path)
                   for char in ("\r", "\n")):
                raise StatusError("invalid connection settings")
            reader, writer = await asyncio.open_connection(self.host, self.port)
            request = ("GET %s HTTP/1.1\r\nHost: %s:%s\r\n"
                       "Authorization: Bearer %s\r\nConnection: close\r\n\r\n" %
                       (path, self.host, self.port, self.token or ""))
            writer.write(request.encode("utf-8"))
            await writer.drain()

            header = b""
            while b"\r\n\r\n" not in header:
                chunk = await reader.read(256)
                if not chunk:
                    raise NetworkUnavailable("incomplete HTTP response")
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
            if not 0 <= length <= 65536 or b"transfer-encoding" in headers:
                raise StatusError("unsupported response size or framing")
            while len(body) < length:
                chunk = await reader.read(min(1024, length - len(body)))
                if not chunk:
                    raise NetworkUnavailable("incomplete response body")
                body += chunk
            if len(body) != length:
                raise StatusError("invalid response length")
            try:
                text = body.decode("utf-8")
            except UnicodeError:
                raise StatusError("response was not UTF-8")
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
