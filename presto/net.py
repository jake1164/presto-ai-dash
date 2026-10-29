"""HTTP client for the Presto app.

The live app uses bounded asyncio streams so touch/rendering can continue
during network waits. The synchronous requests wrapper is retained for smoke
checks and existing transport tests. Both send the same shared bearer token.

Every request also says which client it is: ``X-Presto-Client`` carries the
device's name (``DEVICE_NAME`` in the device's own ``config.py``, "presto" by
default) and ``X-Presto-Revision`` the app revision it runs. The daemon keeps
both in memory to show who is connected; it never stores them.

A request may also carry ``X-Presto-Diag``: a short summary of the device's
own state, so the daemon can say why a device is unhappy without a serial
cable. It is built by ``build_diag`` from values the app already gathered,
it never carries an exception's text, and it is at most 120 characters long.

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
# An empty override disables durable device telemetry in the host preview.
DEVICE_ID_OVERRIDE = None

# The deployment writes the app revision next to the app files, so the device
# reports the build it actually runs.
REVISION_FILE = "revision.txt"
REVISION_FILES = (REVISION_FILE, "/ai_dash_files/revision.txt")
MAX_LABEL = 32

# The path the lifecycle extension belongs to. The two headers go on the
# status GET only: a card fetch and an alert write carry neither.
STATUS_PATH = "/api/v1/status"
DEVICE_ID_HEADER = "X-Presto-Device-ID"
LIFECYCLE_HEADER = "X-Presto-Lifecycle"
DEVICE_ID_CHARS = 32
HEX = "0123456789abcdef"

# The diagnostics header: what the device says about itself.
DIAG_HEADER = "X-Presto-Diag"
# The daemon refuses a longer header, so the device never sends one.
DIAG_MAX_CHARS = 120
# The stages a fetch can stop at. The daemon accepts only these.
DIAG_STAGES = ("conn", "send", "head", "body", "parse")
# The order the pairs are written in.
DIAG_ORDER = ("err", "st", "n", "free", "up", "rssi", "drops")
# The error label is at most this long, and the daemon drops a longer one.
DIAG_MAX_ERR = 24
# The characters an error label may carry: letters, digits and an underscore.
DIAG_NAME_CHARS = ("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
                   "0123456789_")
# The digits an errno may have.
DIAG_DIGITS = "0123456789"


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


def error_label(exc):
    """A short label for a failure: its type name and, maybe, its errno.

    The exception's own text is never used: it can carry an address, a token
    or a response body. Only the name and an integer errno are kept, so the
    label is safe to put in a request header, and it is the shape the daemon
    accepts: the name, then at most one errno.
    """
    name = type(exc).__name__
    for char in name:
        if char not in DIAG_NAME_CHARS:
            return None
    errno = None
    args = getattr(exc, "args", None)
    if isinstance(exc, OSError) and args and type(args[0]) is int:
        errno = args[0]
    if errno is None:
        return name[:DIAG_MAX_ERR]
    tail = " %d" % errno
    if len(name) + len(tail) <= DIAG_MAX_ERR and len(str(abs(errno))) <= 4:
        return name + tail
    return name[:DIAG_MAX_ERR]


def _is_error_label(text):
    """Whether a label is the shape the daemon accepts.

    The daemon's rule is a name, then at most one errno: digits, with an
    optional minus in front. Anything else is not sent.
    """
    if not text or len(text) > DIAG_MAX_ERR:
        return False
    name, sep, tail = text.partition(" ")
    if not name or not all(char in DIAG_NAME_CHARS for char in name):
        return False
    if not sep:
        return True
    digits = tail[1:] if tail.startswith("-") else tail
    if not digits or len(digits) > 4:
        return False
    return all(char in DIAG_DIGITS for char in digits)


def _render_diag(pairs):
    """The header value for a list of (key, value) pairs."""
    return ";".join("%s=%s" % (key, value) for key, value in pairs)


def build_diag(state, limit=DIAG_MAX_CHARS):
    """The diagnostics header value for a state dict, or None.

    Pure, so it can be checked on a development machine. The pairs are
    written in the order the daemon reads them. The whole value stays
    within the length the daemon accepts: the lowest-priority pairs
    (``drops``, then ``rssi``, then ``up``) are left out first, and only
    then is the error label shortened. A label the daemon would not accept
    is left out rather than sent.
    """
    if not isinstance(state, dict):
        return None
    pairs = []
    for key in DIAG_ORDER:
        value = state.get(key)
        if value is None:
            continue
        if key == "err":
            text = value if _is_error_label(value) else ""
        elif key == "st":
            text = value if value in DIAG_STAGES else ""
        elif isinstance(value, bool) or not isinstance(value, int):
            continue
        else:
            text = str(value)
        if text:
            pairs.append((key, text))
    text = _render_diag(pairs)
    for key in ("drops", "rssi", "up"):
        if len(text) <= limit:
            break
        pairs = [(name, value) for name, value in pairs if name != key]
        text = _render_diag(pairs)
    if len(text) > limit:
        # Only a long error label can still be too long: shorten it.
        pairs = [(name, value[:DIAG_MAX_ERR] if name == "err" else value)
                 for name, value in pairs]
        text = _render_diag(pairs)
    return text or None


def request_headers(headers):
    """The request header lines, in the order they are sent.

    Only the headers that are present are written, so a request without
    diagnostics looks exactly as it did before.
    """
    lines = []
    for name in ("Authorization", "X-Presto-Client", "X-Presto-Revision", DIAG_HEADER,
                 DEVICE_ID_HEADER, LIFECYCLE_HEADER):
        value = headers.get(name)
        if value:
            lines.append("%s: %s" % (name, value))
    return "".join(line + "\r\n" for line in lines)


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


def _hex_revision(text):
    """A revision as 12 or 64 lowercase hexadecimal characters, or None."""
    if not isinstance(text, str):
        return None
    text = text.strip().lower()
    if len(text) not in (12, 64):
        return None
    for char in text:
        if char not in HEX:
            return None
    return text


def current_revision_hex():
    """The installed revision for the lifecycle report, or None when unknown.

    The lifecycle header carries only a genuine hexadecimal revision, because
    the daemon compares it against a hash. The plain ``X-Presto-Revision``
    header keeps its looser label, so an unreadable revision still shows as
    "unknown" in the daemon's in-memory client list.
    """
    if REVISION_OVERRIDE:
        return _hex_revision(REVISION_OVERRIDE)
    configured = device_setting("DEVICE_REVISION", None)
    if configured:
        return _hex_revision(configured)
    for candidate in REVISION_FILES:
        try:
            with open(candidate, "r") as handle:
                text = handle.read(65)
        except Exception:
            continue
        found = _hex_revision(text)
        if found:
            return found
    return None


def current_device_id():
    """The persistent device identifier from the device's own config, or None.

    It is generated once at setup and kept outside the replaceable app files,
    so a repair keeps the identity the daemon already knows.
    """
    value = DEVICE_ID_OVERRIDE
    if value is None:
        value = device_setting("DEVICE_ID", None)
    if not isinstance(value, str) or len(value) != DEVICE_ID_CHARS:
        return None
    for char in value:
        if char not in HEX:
            return None
    return value


def build_headers(token, client=None, revision=None, diag=None, device_id=None,
                  lifecycle=None):
    """The request headers, including the shared bearer token.

    An empty token is still sent as an empty bearer so the daemon's error is a
    clean 401 rather than a confusing connection failure. Every request also
    names the client and the app revision it runs, and carries the device's
    own diagnostics when they are known. The two lifecycle headers are added
    by ``StatusClient.headers_for``, on the status GET only.
    """
    headers = {"Authorization": "Bearer {}".format(token or ""),
               "X-Presto-Client": safe_label(client) if client else current_client_name(),
               "X-Presto-Revision": safe_label(revision) if revision else current_revision()}
    if diag:
        headers[DIAG_HEADER] = diag
    if device_id:
        headers[DEVICE_ID_HEADER] = device_id
    if lifecycle:
        headers[LIFECYCLE_HEADER] = lifecycle
    return headers


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

    ``diag_provider`` is a callable returning the device's own state as a
    dict, or None. Its values are sent in the ``X-Presto-Diag`` header, so
    the daemon can say why a device is unhappy without a serial cable. It
    is called at most once per request, and a probe that fails is simply
    not reported.
    """

    def __init__(self, host, port, token, timeout=8,
                 path="/api/v1/status", transport=None, diag_provider=None,
                 lifecycle_provider=None):
        self.host = host
        self.port = port
        self.token = token
        self.timeout = timeout
        self.path = path
        self._transport = transport
        self.diag_provider = diag_provider
        # A callable returning ``(device_id, lifecycle_text)`` or None. It is
        # asked once per status poll and never breaks one.
        self.lifecycle_provider = lifecycle_provider
        # Where the last async fetch got to and why it ended; the screen and
        # the serial log report these when the daemon looks unreachable.
        self.stage = "start"
        self.received = 0
        # The last failure, kept after it clears: a healthy poll then still
        # says what went wrong before it. Both are the header's own values.
        self.last_error = None
        self.last_stage = None

    @property
    def url(self):
        return build_url(self.host, self.port, self.path)

    @property
    def headers(self):
        return build_headers(self.token, diag=self.diag_header())

    def lifecycle_headers(self, path):
        """The two lifecycle headers, for a status GET and nothing else.

        The provider is asked at most once per request, and a provider that
        raises or returns something unusable costs the poll nothing: the
        request goes out without them and the daemon treats it as legacy
        telemetry.
        """
        provider = self.lifecycle_provider
        if provider is None:
            return {}
        if (path or "").split("?")[0] != STATUS_PATH:
            return {}
        try:
            pair = provider()
        except Exception:
            return {}
        if not isinstance(pair, tuple) or len(pair) != 2:
            return {}
        device_id, text = pair
        if not device_id or not text:
            return {}
        return {DEVICE_ID_HEADER: device_id, LIFECYCLE_HEADER: text}

    def headers_for(self, path):
        """The headers of one request: the usual ones, plus the lifecycle pair."""
        headers = self.headers
        for name, value in self.lifecycle_headers(path).items():
            headers[name] = value
        return headers

    def diag_header(self):
        """The diagnostics header value for this request, or None.

        The provider is asked once and never breaks a fetch: a probe that
        raises is reported as nothing.
        """
        provider = self.diag_provider
        if provider is None:
            return None
        try:
            state = provider()
        except Exception:
            return None
        if not state:
            return None
        try:
            return build_diag(state)
        except Exception:
            return None

    def _note_failure(self, exc):
        """Remember the last failure, so the next poll can still report it.

        The label is the exception's type and errno, never its text: the text
        can carry an address or a token. Both stay after the failure clears,
        so a healthy poll still says what went wrong before it.
        """
        label = error_label(exc)
        if label:
            self.last_error = label
        if self.stage in DIAG_STAGES:
            self.last_stage = self.stage

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
        except (Unauthorised, StatusError) as exc:
            self._note_failure(exc)
            raise
        except Exception as exc:
            self._note_failure(exc)
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
            headers = self.headers_for(path)
            if any(char in str(value) for value in (self.host, self.token, path)
                   for char in ("\r", "\n")):
                raise StatusError("invalid connection settings")
            reader, writer = await asyncio.open_connection(self.host, self.port)
            request = ("GET %s HTTP/1.1\r\nHost: %s:%s\r\n%sConnection: close\r\n\r\n" %
                       (path, self.host, self.port, request_headers(headers)))
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
        except (Unauthorised, StatusError) as exc:
            self._note_failure(exc)
            raise
        except Exception as exc:
            self._note_failure(exc)
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
            request = ("POST %s HTTP/1.1\r\nHost: %s:%s\r\n%s"
                       "Content-Type: application/json\r\n"
                       "Content-Length: %d\r\nConnection: close\r\n\r\n%s" %
                       (path, self.host, self.port, request_headers(headers),
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


