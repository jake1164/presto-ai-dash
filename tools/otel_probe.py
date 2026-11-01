"""A throwaway OpenTelemetry listener: what does Claude Code emit, and from where?

    python tools/otel_probe.py                 # listens on 127.0.0.1:4318

It answers one question: does the Claude Code surface you use (the VS Code
panel, the terminal) emit OpenTelemetry at all, and what does it carry? An
OpenTelemetry receiver in the daemon is only worth building if it does. It is a
diagnostic, not part of the daemon.

Privacy first. Claude Code puts the account's email, its ids and, when opted in,
prompts and tool inputs on its telemetry. This probe therefore prints only:

  * metric and event names, counts and numeric totals,
  * attribute NAMES (so you can see what is there),
  * the values of a short allowlist of harmless attributes (the model, the
    surface that emitted it, token type, status code),
  * and, for sensitive attributes, only that they were PRESENT - never the value.

Nothing is stored except what you see on the screen, and it listens on the
loopback address only. Use the HTTP/JSON protocol: it needs no extra library.
"""

import argparse
import json
import sys
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MAX_BODY = 5 * 1024 * 1024
# A compressed body is held to the same size once it is expanded; a few
# kilobytes of gzip can otherwise stand for gigabytes.
MAX_EXPANDED = MAX_BODY
MAX_LINE = 400
PATHS = {"/v1/metrics": "metrics", "/v1/logs": "logs", "/v1/traces": "traces"}

# Values that are safe to show: no identity, no content.
SHOW_VALUES = frozenset((
    "service.name", "model", "type", "app.entrypoint", "terminal.type", "query_source",
    "speed", "effort", "start_type", "success", "status_code", "decision", "tool_name",
    "event.name"))
# Numbers that are safe to show and add up.
NUMERIC = frozenset((
    "duration_ms", "cost_usd", "cost_usd_micros", "input_tokens", "output_tokens",
    "cache_read_tokens", "cache_creation_tokens", "prompt_length", "response_length",
    "attempt", "tool_input_size_bytes", "tool_result_size_bytes"))
# Attributes that can identify the person or carry content: only their presence
# is reported, never the value.
SENSITIVE = frozenset((
    "user.email", "user.id", "user.account_uuid", "user.account_id", "organization.id",
    "session.id", "ccr.session.id", "workspace.host_paths", "prompt", "prompt_text",
    "response", "tool_input", "tool_parameters", "body", "body_ref", "error",
    "message.uuid", "prompt.id", "request_id", "client_request_id", "tool_use_id"))
SENSITIVE_PREFIXES = ("vcs.", "host.", "process.", "os.")

HINT = ("set OTEL_EXPORTER_OTLP_PROTOCOL=http/json: this probe reads JSON only, and "
        "protobuf bodies are counted, not decoded")


def _is_sensitive(key):
    return key in SENSITIVE or key.startswith(SENSITIVE_PREFIXES)


def _plain(value):
    """The Python value of one OTLP attribute value, or None."""
    if not isinstance(value, dict):
        return None
    for kind in ("stringValue", "intValue", "doubleValue", "boolValue"):
        if kind in value:
            raw = value[kind]
            if kind == "intValue":
                try:
                    return int(raw)
                except (TypeError, ValueError):
                    return None
            return raw
    return None


def _attributes(items):
    """A list of (key, value) pairs from an OTLP attribute list; never raises."""
    pairs = []
    if not isinstance(items, list):
        return pairs
    for item in items:
        if isinstance(item, dict) and isinstance(item.get("key"), str):
            pairs.append((item["key"], _plain(item.get("value"))))
    return pairs


def printable(text):
    """``text`` with everything but printable ASCII replaced, and one line long.

    Names and values come from whatever sent the request. A control character
    in one (an escape sequence, say) would otherwise be interpreted by the
    terminal that shows it.
    """
    clean = "".join(ch if " " <= ch <= "~" else "?" for ch in str(text))
    return clean if len(clean) <= MAX_LINE else clean[:MAX_LINE - 3] + "..."


def inflate(raw, limit=MAX_EXPANDED):
    """The gunzipped ``raw``; ValueError when it would expand past ``limit``."""
    expander = zlib.decompressobj(16 + zlib.MAX_WBITS)
    data = expander.decompress(raw, limit + 1)
    if len(data) > limit or expander.unconsumed_tail:
        raise ValueError("expands to more than %d bytes" % limit)
    if not expander.eof:
        raise EOFError("the compressed data ends early")
    return data


class Tally(object):
    """What one metric or event name has carried, without private values."""

    def __init__(self):
        self.count = 0
        self.names = set()
        self.sensitive = set()
        self.shown = {}
        self.numbers = {}

    def add(self, pairs):
        self.count += 1
        for key, value in pairs:
            if _is_sensitive(key):
                self.sensitive.add(key)
                continue
            self.names.add(key)
            if key in SHOW_VALUES and isinstance(value, (str, bool, int)):
                self.shown.setdefault(key, set()).add(str(value))
            elif key in NUMERIC and isinstance(value, (int, float)) and not isinstance(value, bool):
                self.numbers[key] = self.numbers.get(key, 0) + value

    def lines(self, indent="  "):
        out = []
        if self.names:
            out.append("%sattributes: %s" % (indent, ", ".join(sorted(self.names))))
        for key in sorted(self.shown):
            out.append("%s  %s = %s" % (indent, key, " | ".join(sorted(self.shown[key])[:8])))
        for key in sorted(self.numbers):
            out.append("%s  total %s = %s" % (indent, key, _number(self.numbers[key])))
        if self.sensitive:
            out.append("%sPRESENT (values not shown): %s" % (indent, ", ".join(sorted(self.sensitive))))
        return out


def _number(value):
    return ("%.4f" % value).rstrip("0").rstrip(".") if isinstance(value, float) else str(value)


def _points(metric):
    """The data points of any metric kind."""
    for kind in ("sum", "gauge", "histogram", "exponentialHistogram", "summary"):
        body = metric.get(kind)
        if isinstance(body, dict) and isinstance(body.get("dataPoints"), list):
            return kind, body["dataPoints"]
    return None, []


def _point_value(point):
    for key in ("asDouble", "asInt", "sum"):
        if key in point:
            try:
                return float(point[key])
            except (TypeError, ValueError):
                return None
    return None


def _resource(item):
    resource = item.get("resource") if isinstance(item, dict) else None
    tally = Tally()
    if isinstance(resource, dict):
        tally.add(_attributes(resource.get("attributes")))
    return tally


def summarize(kind, payload):
    """Plain lines describing one OTLP/JSON request. Never raises."""
    try:
        return _summarize(kind, payload)
    except Exception as error:  # a diagnostic must never take itself down
        return ["  (could not summarise this %s request: %s)" % (kind, type(error).__name__)]


def _summarize(kind, payload):
    lines = []
    if not isinstance(payload, dict):
        return ["  (the %s body was not a JSON object)" % kind]
    if kind == "metrics":
        groups = payload.get("resourceMetrics")
    elif kind == "logs":
        groups = payload.get("resourceLogs")
    else:
        groups = payload.get("resourceSpans")
    if not isinstance(groups, list):
        return ["  (no %s found in the body)" % kind]
    for group in groups:
        if not isinstance(group, dict):
            continue
        resource = _resource(group)
        lines.append("  resource:")
        lines.extend(resource.lines("    "))
        scopes = group.get("scopeMetrics" if kind == "metrics" else
                           "scopeLogs" if kind == "logs" else "scopeSpans")
        for scope in scopes if isinstance(scopes, list) else []:
            if not isinstance(scope, dict):
                continue
            if kind == "metrics":
                lines.extend(_metrics(scope.get("metrics")))
            elif kind == "logs":
                lines.extend(_logs(scope.get("logRecords")))
            else:
                spans = scope.get("spans")
                lines.append("  spans: %d" % (len(spans) if isinstance(spans, list) else 0))
    return lines


def _metrics(metrics):
    lines = []
    for metric in metrics if isinstance(metrics, list) else []:
        if not isinstance(metric, dict) or not isinstance(metric.get("name"), str):
            continue
        shape, points = _points(metric)
        tally = Tally()
        total = 0.0
        counted = False
        for point in points:
            if not isinstance(point, dict):
                continue
            tally.add(_attributes(point.get("attributes")))
            value = _point_value(point)
            if value is not None:
                total += value
                counted = True
        unit = metric.get("unit") if isinstance(metric.get("unit"), str) else ""
        lines.append("  METRIC %s%s  points=%d%s" % (
            metric["name"], " [%s]" % unit if unit else "", len(points),
            "  total=%s" % _number(round(total, 6)) if counted else ""))
        lines.extend(tally.lines("    "))
    return lines


def _logs(records):
    lines = []
    events = {}
    for record in records if isinstance(records, list) else []:
        if not isinstance(record, dict):
            continue
        pairs = _attributes(record.get("attributes"))
        name = None
        for key, value in pairs:
            if key == "event.name" and isinstance(value, str):
                name = value
        if name is None:
            body = _plain(record.get("body"))
            name = body if isinstance(body, str) and body.startswith("claude_code.") else "(unnamed)"
        events.setdefault(name, Tally()).add(pairs)
    for name in sorted(events):
        lines.append("  EVENT %s  count=%d" % (name, events[name].count))
        lines.extend(events[name].lines("    "))
    return lines


class ProbeHandler(BaseHTTPRequestHandler):
    server_version = "otel-probe"

    def log_message(self, fmt, *args):  # the access log would echo paths only, but stay quiet
        pass

    def _reply(self, status, body=b"{}"):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def _other(self):
        """Any contact that is not a POST to an OTLP path: show it, count it, answer 404.

        Silence here would look exactly like "nothing arrived". The query string is
        never echoed: it could carry anything.
        """
        path = self.path.split("?")[0][:80]
        self.server.counts["unrecognised"] = self.server.counts.get("unrecognised", 0) + 1
        self.server.out("[%s] contact: %s %s -> 404 (not a POST to /v1/metrics, /v1/logs "
                        "or /v1/traces)" % (self.server.clock(), self.command, path))
        self._reply(404, b'{"error":"not found"}')

    do_GET = do_PUT = do_DELETE = do_PATCH = do_HEAD = do_OPTIONS = _other

    def do_POST(self):
        kind = PATHS.get(self.path.split("?")[0])
        if kind is None:
            self._other()
            return
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            self._reply(400, b'{"error":"length required"}')
            return
        if length < 0 or length > MAX_BODY:
            self.server.out("  (a %s body of %d bytes was refused: too large)" % (kind, length))
            self._reply(413, b'{"error":"too large"}')
            return
        raw = self.rfile.read(length)
        self.server.counts[kind] = self.server.counts.get(kind, 0) + 1
        header = "[%s] %s request #%d" % (self.server.clock(), kind, self.server.counts[kind])
        if "protobuf" in (self.headers.get("Content-Type") or "").lower():
            self.server.out("%s: protobuf, %d bytes - not decoded; %s" % (header, length, HINT))
            self._reply(200, b"")
            return
        try:
            if (self.headers.get("Content-Encoding") or "").lower() == "gzip":
                raw = inflate(raw)
            payload = json.loads(raw.decode("utf-8"))
        except (OSError, ValueError, EOFError, UnicodeError, zlib.error, RecursionError):
            self.server.out("%s: the body was not valid JSON (or gzip)" % header)
            self._reply(400, b'{"error":"invalid body"}')
            return
        self.server.out(header)
        for line in summarize(kind, payload):
            self.server.out(line)
        self._reply(200)


def make_server(port, out=print):
    """The listener on the loopback address. ``out`` receives each line shown."""
    import time
    server = ThreadingHTTPServer(("127.0.0.1", port), ProbeHandler)
    # Everything shown passes the filter, whatever a request put in a name.
    server.out = lambda text: out(printable(text))
    server.counts = {}
    server.clock = lambda: time.strftime("%H:%M:%S")
    return server


def listen(port, out=print):
    """A server on ``port``, or on another free loopback port when that one cannot be used.

    Windows reserves ranges of ports (Hyper-V and Docker Desktop do), and binding
    one fails with WinError 10013 even though nothing is listening. Returns the
    server and a note ("" when the asked-for port was used) saying what happened.
    """
    candidates = [port] + [port + 10000] if port + 10000 < 65536 else [port]
    candidates.append(0)
    last = None
    for candidate in candidates:
        try:
            server = make_server(candidate, out)
        except OSError as error:
            last = error
            continue
        actual = server.server_address[1]
        if actual == port:
            return server, ""
        return server, ("port %d could not be used here (%s); listening on %d instead"
                        % (port, type(last).__name__ if last else "busy", actual))
    raise last


BANNER = """OpenTelemetry probe listening on http://127.0.0.1:{port}  (loopback only)

To test the Claude Code surface you use, give it these environment variables
(do NOT use OTEL_LOG_USER_PROMPTS, OTEL_LOG_TOOL_DETAILS or OTEL_LOG_RAW_API_BODIES):

    CLAUDE_CODE_ENABLE_TELEMETRY=1
    OTEL_METRICS_EXPORTER=otlp
    OTEL_LOGS_EXPORTER=otlp
    OTEL_EXPORTER_OTLP_PROTOCOL=http/json
    OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:{port}
    OTEL_METRICS_INCLUDE_ENTRYPOINT=true
    OTEL_METRIC_EXPORT_INTERVAL=10000
    OTEL_LOGS_EXPORT_INTERVAL=2000

Start the surface AFTER setting them (they are read once at startup). Then use
it for a minute. Only names, counts, harmless values and "PRESENT" markers are
printed; the account's email, ids and any prompt text are never shown or stored.
Press Ctrl-C to stop.
"""


def final_summary(counts):
    """The lines printed when the probe stops."""
    total = sum(value for key, value in counts.items() if key != "unrecognised")
    stray = counts.get("unrecognised", 0)
    lines = ["stopped: %d telemetry request(s) received %s" % (total, dict(counts))]
    if stray:
        lines.append("%d unrecognised contact(s): something connected, but not with a POST to "
                     "/v1/metrics, /v1/logs or /v1/traces. See the contact lines above." % stray)
    if not total and not stray:
        lines.append("Nothing arrived: this surface did not emit telemetry to the probe.")
    return chr(10).join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--port", type=int, default=4318)
    args = parser.parse_args(argv)
    server, note = listen(args.port)
    if note:
        print("NOTE: " + note)
        print()
    print(BANNER.format(port=server.server_address[1]), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    print(chr(10) + final_summary(server.counts))
    return 0


if __name__ == "__main__":
    sys.exit(main())
