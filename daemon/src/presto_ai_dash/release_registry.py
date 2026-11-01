"""Read-only registry adapters that list the numbered releases of one project.

Two adapters, one question each: "which numbered releases exist?"

* ``PypiAdapter`` asks ``https://pypi.org/pypi/<project>/json`` and nothing else.
* ``GhcrAdapter`` lists the tags of one public image on ``ghcr.io`` with the
  registry's anonymous pull token and nothing else.

Neither adapter knows about the other, about Presto, or about the cache. They
return a list of ``Release`` or raise ``RegistryError`` with one of a fixed set
of codes. There is no fallback between registries and none to TestPyPI.

Every request is bounded: https only, a fixed host list, no redirect to
anywhere not listed, a response size limit, a per-request timeout inside one
overall deadline, and a page limit. A listing that could not be read to the end
raises ``incomplete_listing`` rather than returning what was seen, because a
partial list could miss the newest release and be reported as "up to date".

Dependency decision: ``oras-py`` was tried
first and works anonymously, but it sets no request timeout, follows the token
``realm`` from a registry challenge to any host, applies no response size limit
and brings ``requests`` and ``jsonschema`` with it. The checks here need a
bounded, validated client, so this module uses the standard library only.
"""

import http.client
import json
import re
import socket
import ssl
import threading
import time
from email.utils import parsedate_to_datetime
import urllib.error
import urllib.request
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit

from .release_policy import Release, parse_docker, parse_python

# The fixed error codes shared with the status contract.
ERROR_CODES = ("timeout", "network_error", "http_error", "rate_limited",
               "auth_challenge", "invalid_metadata", "incomplete_listing",
               "no_eligible_release", "running_version_unknown")

USER_AGENT = "presto-ai-dash-release-check"
REQUEST_TIMEOUT_S = 10.0
OVERALL_TIMEOUT_S = 30.0
PYPI_MAX_BYTES = 2 * 1024 * 1024
TAGS_PAGE_MAX_BYTES = 256 * 1024
TOKEN_MAX_BYTES = 16 * 1024
MAX_PAGES = 20
PAGE_SIZE = 100
MAX_TAGS = 5000
MAX_PYPI_RELEASES = 2000
MAX_REDIRECTS = 2
# Retry-After is honored within these bounds, in seconds.
RETRY_AFTER_MIN_S = 60
# One socket read may stall this long, so a stalled body is cut off promptly.
READ_STALL_S = 5.0
CHUNK_BYTES = 8192
RETRY_AFTER_MAX_S = 86400


def _plain_port(parts):
    """Whether a URL names the default https port (or none)."""
    try:
        return parts.port in (None, 443)
    except ValueError:
        return False


class RegistryError(Exception):
    """A check could not finish. ``code`` is one of ``ERROR_CODES``."""

    def __init__(self, code, retry_after=None):
        super().__init__(code)
        self.code = code
        self.retry_after = retry_after


class Cancelled(Exception):
    """The daemon is shutting down; the check was abandoned, not failed."""


class Response(object):
    def __init__(self, status, headers, body):
        self.status = status
        self.headers = {str(k).lower(): v for k, v in headers.items()}
        self.body = body


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        return None


class _Deadline(object):
    """A hard stop for one request: at the deadline, its sockets are shut down.

    A socket timeout applies to each read, so a server that sends its status
    line, headers or TLS handshake a byte at a time could hold a request far
    past its budget. Shutting the socket down from a timer unblocks the read
    whatever phase it is in. Only DNS resolution and the TCP connect itself
    run before a socket exists; those are bounded by the socket timeout.
    """

    def __init__(self, seconds):
        self.fired = False
        self._connections = []
        self._lock = threading.Lock()
        self._timer = threading.Timer(max(0.0, seconds), self._expire)
        self._timer.daemon = True

    def track(self, connection):
        with self._lock:
            self._connections.append(connection)
            fired = self.fired
        if fired:
            raise socket.timeout("request deadline passed")

    def _expire(self):
        with self._lock:
            self.fired = True
            connections = list(self._connections)
        for connection in connections:
            sock = getattr(connection, "sock", None)
            if sock is None:
                continue
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def __enter__(self):
        self._timer.start()
        return self

    def __exit__(self, *exc):
        self._timer.cancel()
        return False


def _tracked(base, deadline):
    """A connection class that registers itself with ``deadline`` on connect."""

    class Connection(base):
        def connect(self):
            deadline.track(self)
            super().connect()

    return Connection


class _HttpHandler(urllib.request.HTTPHandler):
    def __init__(self, deadline):
        super().__init__()
        self._deadline = deadline

    def http_open(self, request):
        return self.do_open(_tracked(http.client.HTTPConnection, self._deadline), request)


class _HttpsHandler(urllib.request.HTTPSHandler):
    def __init__(self, deadline):
        super().__init__()
        self._deadline = deadline

    def https_open(self, request):
        return self.do_open(_tracked(http.client.HTTPSConnection, self._deadline), request,
                            context=self._context)


def _opener(deadline):
    # urllib's proxy handling (HTTPS_PROXY, NO_PROXY) still applies.
    return urllib.request.build_opener(_NoRedirects(), _HttpHandler(deadline),
                                       _HttpsHandler(deadline))


def retry_after_seconds(value, now=None):
    """A Retry-After header as clamped whole seconds, or None when unusable.

    Both forms RFC 9110 allows are read: a number of seconds, or an HTTP date
    (taken relative to ``now``, the current time by default). A date in the
    past means "now", which the lower bound turns into the shortest wait.
    """
    text = str(value).strip() if value is not None else ""
    if not text:
        return None
    if text.isdigit() and len(text) <= 10:
        seconds = int(text)
    else:
        try:
            moment = parsedate_to_datetime(text)
        except (TypeError, ValueError, IndexError, OverflowError):
            return None
        if moment is None or moment.tzinfo is None:
            return None
        seconds = int(moment.timestamp() - (time.time() if now is None else now))
    return max(RETRY_AFTER_MIN_S, min(seconds, RETRY_AFTER_MAX_S))


def _read_bounded(stream, max_bytes, started, timeout):
    """Read a body in chunks, stopping at the size limit or the time budget."""
    chunks, total = [], 0
    # read1 returns what one socket read produced; read(n) would wait for n
    # bytes and so could not notice the clock.
    read = getattr(stream, "read1", None) or stream.read
    while True:
        if time.monotonic() - started > timeout:
            raise RegistryError("timeout")
        chunk = read(min(CHUNK_BYTES, max_bytes + 1 - total))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
        if total > max_bytes:
            break
    if time.monotonic() - started > timeout:
        raise RegistryError("timeout")
    return b"".join(chunks)


def fetch(url, headers, timeout, max_bytes):
    """One GET without redirects, returning a ``Response`` of at most ``max_bytes``.

    The real transport. Tests substitute a function with the same signature.
    Any HTTP status comes back as a response; only a transport problem raises.
    """
    request = urllib.request.Request(url, method="GET")
    for key, value in headers.items():
        request.add_header(key, value)
    started = time.monotonic()
    # ``timeout`` bounds the whole request: the TLS handshake, the status line
    # and headers (by the deadline timer) and the body (by the chunked read).
    with _Deadline(timeout) as deadline:
        try:
            with _opener(deadline).open(request,
                                        timeout=min(timeout, READ_STALL_S)) as response:
                raw = _read_bounded(response, max_bytes, started, timeout)
                status = getattr(response, "status", None) or response.getcode()
                received = dict(response.headers.items())
        except urllib.error.HTTPError as exc:
            try:
                raw = _read_bounded(exc, max_bytes, started, timeout)
                received = dict(exc.headers.items()) if exc.headers else {}
            except (OSError, http.client.HTTPException):
                raise RegistryError("timeout" if deadline.fired else "network_error")
            finally:
                exc.close()
            status = exc.code
        except RegistryError:
            raise
        except (socket.timeout, TimeoutError):
            raise RegistryError("timeout")
        except urllib.error.URLError as exc:
            if deadline.fired or isinstance(exc.reason, (socket.timeout, TimeoutError)):
                raise RegistryError("timeout")
            raise RegistryError("network_error")
        except (OSError, ssl.SSLError, ValueError, http.client.HTTPException):
            # A socket shut down by the deadline surfaces as a reset, an
            # incomplete read or a malformed status line: it was the clock.
            raise RegistryError("timeout" if deadline.fired else "network_error")
    if deadline.fired or time.monotonic() - started > timeout:
        raise RegistryError("timeout")
    if len(raw) > max_bytes:
        raise RegistryError("invalid_metadata")
    return Response(status, received, raw)


class _Session(object):
    """The shared limits for one check: deadline, cancellation, host list."""

    def __init__(self, hosts, transport=None, clock=time.monotonic,
                 cancelled=None, overall_timeout=OVERALL_TIMEOUT_S):
        self.hosts = frozenset(hosts)
        self.transport = transport or fetch
        self.clock = clock
        self.cancelled = cancelled or (lambda: False)
        self.deadline = clock() + overall_timeout
        self.requests = 0

    def get(self, url, max_bytes, headers=None, redirects=0):
        """GET ``url`` (https, an allowed host), following allowed redirects."""
        for _ in range(redirects + 1):
            parts = urlsplit(url)
            if (parts.scheme != "https" or (parts.hostname or "").lower() not in self.hosts
                    or parts.username or parts.password or not _plain_port(parts)
                    or len(url) > 1024):
                raise RegistryError("invalid_metadata")
            if self.cancelled():
                raise Cancelled()
            remaining = self.deadline - self.clock()
            if remaining <= 0:
                raise RegistryError("timeout")
            sent = {"User-Agent": USER_AGENT, "Accept": "application/json"}
            sent.update(headers or {})
            self.requests += 1
            response = self.transport(url, sent, min(REQUEST_TIMEOUT_S, remaining), max_bytes)
            if response.status in (301, 302, 303, 307, 308) and redirects:
                url = urljoin(url, response.headers.get("location", ""))
                # Credentials never travel with a redirect.
                headers = {k: v for k, v in (headers or {}).items()
                           if k.lower() != "authorization"}
                redirects -= 1
                continue
            return response
        raise RegistryError("http_error")

    def ok_json(self, response):
        """The JSON object of a 200 response, or the matching error."""
        status = response.status
        if status == 429 or (status == 403 and response.headers.get("x-ratelimit-remaining") == "0"):
            raise RegistryError("rate_limited", retry_after_seconds(
                response.headers.get("retry-after")))
        if status != 200:
            raise RegistryError("http_error")
        try:
            data = json.loads(response.body.decode("utf-8"))
        except (ValueError, UnicodeError, RecursionError):
            raise RegistryError("invalid_metadata")
        if not isinstance(data, dict):
            raise RegistryError("invalid_metadata")
        return data


def _normalized_name(text):
    return re.sub(r"[-_.]+", "-", text).lower() if isinstance(text, str) else None


class PypiAdapter(object):
    """Numbered releases of a project on pypi.org, from its JSON metadata."""

    registry = "pypi"
    HOST = "pypi.org"

    def __init__(self, project, transport=None, clock=time.monotonic,
                 cancelled=None):
        self.project = project
        self._transport = transport
        self._clock = clock
        self._cancelled = cancelled

    def package_url(self, label=None):
        base = "https://pypi.org/project/%s/" % self.project
        return base + label + "/" if label else base

    def releases(self):
        session = _Session((self.HOST,), self._transport, self._clock, self._cancelled)
        url = "https://pypi.org/pypi/%s/json" % self.project
        data = session.ok_json(session.get(url, PYPI_MAX_BYTES, redirects=MAX_REDIRECTS))
        info, table = data.get("info"), data.get("releases")
        if (not isinstance(info, dict) or not isinstance(table, dict)
                or _normalized_name(info.get("name")) != _normalized_name(self.project)):
            raise RegistryError("invalid_metadata")
        if len(table) > MAX_PYPI_RELEASES:
            raise RegistryError("incomplete_listing")
        found = {}
        for label, files in table.items():
            version = parse_python(label)
            if version is None or not self._usable(files):
                continue
            found[version] = Release(version, label)
        if not found:
            raise RegistryError("no_eligible_release")
        return list(found.values())

    @staticmethod
    def _usable(files):
        """A release needs at least one real, unyanked distribution file."""
        if not isinstance(files, list) or not files:
            return False
        for item in files:
            if not isinstance(item, dict) or not isinstance(item.get("filename"), str):
                continue
            if item.get("yanked") is True:
                continue
            if item["filename"].endswith((".whl", ".tar.gz")):
                return True
        return False


_PAIR = re.compile(r'([A-Za-z_]+)="([^"]{0,512})"')
_IMAGE = re.compile(r"^[a-z0-9]+(?:[._-][a-z0-9]+)*(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*){0,3}$")
_TOKEN = re.compile(r"^[A-Za-z0-9._~+/=-]{1,8192}$")


_PARAMETER = re.compile(
    r'\s*([A-Za-z0-9!#$&+.^_`|~-]+)\s*(?:=\s*(?:"((?:[^"\\]|\\.)*)"|([^\s;,"]+)))?')


def _link_values(text):
    """Split a Link header into ``(target, {parameter: value})`` pairs (RFC 8288).

    Raises ``RegistryError("incomplete_listing")`` for a header that cannot be
    read: a listing whose next page cannot be told must not look finished.
    """
    values, position, length = [], 0, len(text)
    while position < length:
        while position < length and text[position] in " \t,":
            position += 1
        if position >= length:
            break
        if text[position] != "<":
            raise RegistryError("incomplete_listing")
        end = text.find(">", position)
        if end < 0 or end - position > 1024:
            raise RegistryError("incomplete_listing")
        target, params, position = text[position + 1:end], {}, end + 1
        while True:
            while position < length and text[position] in " \t":
                position += 1
            if position >= length or text[position] == ",":
                break
            if text[position] != ";":
                raise RegistryError("incomplete_listing")
            match = _PARAMETER.match(text, position + 1)
            if not match:
                raise RegistryError("incomplete_listing")
            value = match.group(2) if match.group(2) is not None else (match.group(3) or "")
            params.setdefault(match.group(1).lower(), value)
            position = match.end()
        values.append((target, params))
    return values


def next_link(text):
    """The target of the first ``rel=next`` link in a Link header, or None."""
    for target, params in _link_values(text):
        if "next" in params.get("rel", "").lower().split():
            return target
    return None


class GhcrAdapter(object):
    """Numbered tags of one public image on ghcr.io, anonymously."""

    registry = "ghcr"
    HOST = "ghcr.io"

    def __init__(self, image, github_repository=None, transport=None,
                 clock=time.monotonic, cancelled=None):
        if len(image) > 128 or not _IMAGE.fullmatch(image):
            raise ValueError("not a registry image name")
        self.image = image
        self.github_repository = github_repository or image
        self._transport = transport
        self._clock = clock
        self._cancelled = cancelled

    def package_url(self, label=None):
        base = "https://github.com/%s" % self.github_repository
        if label:
            return "%s/releases/tag/v%s" % (base, label)
        return "%s/pkgs/container/%s" % (base, self.image.rsplit("/", 1)[-1])

    def _challenge(self, response):
        """Validate a 401 challenge; return the token request URL.

        The challenge is data from the network. It is accepted only when it
        names a Bearer scheme, this registry's own token endpoint over https,
        this registry's service and exactly the pull scope for this image. The
        URL that is then requested is built here, not copied from the header.
        """
        header = response.headers.get("www-authenticate", "")
        scheme, _, rest = header.partition(" ")
        pairs = dict(_PAIR.findall(rest))
        want_scope = "repository:%s:pull" % self.image
        if (scheme.lower() != "bearer" or pairs.get("realm") != "https://ghcr.io/token"
                or pairs.get("service") != "ghcr.io"
                or pairs.get("scope", want_scope) != want_scope):
            raise RegistryError("auth_challenge")
        return "https://ghcr.io/token?" + urlencode(
            {"service": "ghcr.io", "scope": want_scope})

    def _token(self, session, url):
        response = session.get(url, TOKEN_MAX_BYTES)
        if response.status in (401, 403):
            raise RegistryError("auth_challenge")
        data = session.ok_json(response)
        token = data.get("token") or data.get("access_token")
        if not isinstance(token, str) or not _TOKEN.fullmatch(token):
            raise RegistryError("auth_challenge")
        return token

    def _next(self, response, current):
        """The next page URL from a Link header, validated, or None."""
        link = response.headers.get("link")
        if not link:
            return None
        reference = next_link(link)
        if reference is None:
            return None
        target = urljoin(current, reference)
        parts = urlsplit(target)
        path = "/v2/%s/tags/list" % self.image
        try:
            query = parse_qsl(parts.query, keep_blank_values=True, strict_parsing=True)
        except ValueError:
            raise RegistryError("incomplete_listing")
        names = [name for name, _ in query]
        if (parts.scheme != "https" or (parts.hostname or "").lower() != self.HOST
                or parts.username or parts.password or not _plain_port(parts)
                or parts.path != path or parts.fragment or len(target) > 512
                or len(set(names)) != len(names) or not set(names) <= {"n", "last"}):
            raise RegistryError("incomplete_listing")
        return target

    def releases(self):
        session = _Session((self.HOST,), self._transport, self._clock, self._cancelled)
        url = "https://ghcr.io/v2/%s/tags/list?%s" % (
            self.image, urlencode({"n": PAGE_SIZE}))
        seen, tags, token = set(), [], None
        for _ in range(MAX_PAGES):
            if url in seen:
                raise RegistryError("incomplete_listing")
            seen.add(url)
            response = session.get(url, TAGS_PAGE_MAX_BYTES,
                                   {"Authorization": "Bearer " + token} if token else None)
            if response.status == 401:
                if token is not None:
                    raise RegistryError("auth_challenge")
                token = self._token(session, self._challenge(response))
                seen.discard(url)
                continue
            data = session.ok_json(response)
            page = data.get("tags")
            if data.get("name") != self.image or not (page is None or isinstance(page, list)):
                raise RegistryError("invalid_metadata")
            for tag in page or ():
                if not isinstance(tag, str):
                    raise RegistryError("invalid_metadata")
                tags.append(tag)
            if len(tags) > MAX_TAGS:
                raise RegistryError("incomplete_listing")
            url = self._next(response, url)
            if url is None:
                return self._numbered(tags)
        # A next page was still offered after the page limit.
        raise RegistryError("incomplete_listing")

    @staticmethod
    def _numbered(tags):
        found = {}
        for tag in tags:
            version = parse_docker(tag)
            if version is not None:
                found[version] = Release(version, tag)
        if not found:
            raise RegistryError("no_eligible_release")
        return list(found.values())
