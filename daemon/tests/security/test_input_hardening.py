"""Hostile input at the daemon's edges: a provider, a request, a log, a status line.

Each test names the input that used to do harm and checks it no longer does.
Everything runs against temporary files and loopback servers.
"""

import base64
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.request import ProxyHandler, Request, build_opener

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from presto_ai_dash import http_client
from presto_ai_dash.admin import AdminServer
from presto_ai_dash.api import ApiServer, BoundedRequestHandler
from presto_ai_dash.collectors import presto_collector as collector
from presto_ai_dash.collectors.claude_activity import ActivityReader
from presto_ai_dash.http_client import HttpError, HttpUnreachable, get_json, get_text
from providers.test_claude_activity import NOW, iso, line
from admin.test_management import ManagementBase


# -- SEC-013: a provider's answer may not fill the daemon's memory --------------

class BodyServer(unittest.TestCase):
    def serve(self, body, status=200, content_type="application/json"):
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return "http://127.0.0.1:%d/" % server.server_port


class ResponseSizeTests(BodyServer):
    def test_a_body_over_the_limit_is_refused_by_every_reader(self):
        url = self.serve(b'{"pad": "' + b"x" * 5000 + b'"}')
        with patch.object(http_client, "MAX_RESPONSE_BYTES", 1000):
            with self.assertRaisesRegex(HttpUnreachable, "larger than"):
                get_json(url)
            with self.assertRaisesRegex(HttpUnreachable, "larger than"):
                get_text(url)

    def test_a_body_under_the_limit_is_read_in_full(self):
        url = self.serve(b'{"ok": true}')
        with patch.object(http_client, "MAX_RESPONSE_BYTES", 1000):
            self.assertEqual(get_json(url), {"ok": True})
            self.assertEqual(get_text(url), '{"ok": true}')

    def test_a_body_of_exactly_the_limit_is_accepted(self):
        body = b'{"a": "' + b"y" * 100 + b'"}'
        url = self.serve(body)
        with patch.object(http_client, "MAX_RESPONSE_BYTES", len(body)):
            self.assertEqual(len(get_json(url)["a"]), 100)

    def test_an_error_body_is_cut_short(self):
        url = self.serve(b"e" * 50000, status=500, content_type="text/plain")
        with patch.object(http_client, "MAX_ERROR_BODY_BYTES", 100):
            with self.assertRaises(HttpError) as caught:
                get_json(url)
        self.assertEqual(caught.exception.status, 500)
        self.assertEqual(len(caught.exception.body), 100)

    def test_the_default_limit_is_a_few_megabytes(self):
        self.assertGreaterEqual(http_client.MAX_RESPONSE_BYTES, 1024 * 1024)
        self.assertLessEqual(http_client.MAX_RESPONSE_BYTES, 16 * 1024 * 1024)


# -- SEC-014: nesting that exhausts the parser's stack ---------------------------

class NestedJsonTests(BodyServer):
    NESTED = b"[" * 8000 + b"]" * 8000  # 16 000 bytes: under the ingest limit

    def test_a_deeply_nested_answer_is_never_an_uncaught_recursion_error(self):
        # How deep the parser goes before it gives up depends on the platform,
        # so a real body may parse or be refused. It must never escape as a
        # RecursionError.
        url = self.serve(self.NESTED)
        try:
            get_json(url)
        except HttpUnreachable:
            pass

    def test_a_parser_that_runs_out_of_stack_is_reported_as_not_json(self):
        url = self.serve(b"{}")
        with patch.object(http_client.json, "loads", side_effect=RecursionError):
            with self.assertRaisesRegex(HttpUnreachable, "nested too deeply"):
                get_json(url)


# -- the servers, built on the shared management fixture -------------------------

class ServerBase(ManagementBase):
    def setUp(self):
        super().setUp()
        self.row = self.manager.save({"type": "claude", "name": "Claude", "enabled": True,
                                      "config": {"settings_path": str(Path(self.temp.name) / "settings.json")}})
        self.api = ApiServer("127.0.0.1", 0, self.storage, "device-token-0123456789",
                             status_provider=self.manager.status_payload,
                             ingest_enabled=lambda: True,
                             alert_writer=lambda instance_id, enabled: {"enabled": enabled})
        self.api._server.RequestHandlerClass.auth_failure_delay = 0
        self.api.start()
        self.addCleanup(self.api.stop)
        self.admin = AdminServer(0, self.manager, "admin-token-0123456789")
        self.admin._server.RequestHandlerClass.auth_failure_delay = 0
        self.admin.start()
        self.addCleanup(self.admin.stop)
        self.opener = build_opener(ProxyHandler({}))

    def request(self, port, method, path, body, headers):
        request = Request("http://127.0.0.1:%d%s" % (port, path), data=body,
                          headers=headers, method=method)
        try:
            with self.opener.open(request, timeout=10) as response:
                return response.status, response.read()
        except Exception as exc:  # HTTPError carries the status and the body
            if hasattr(exc, "code"):
                return exc.code, exc.read()
            raise

    def ingest(self, body, token="auto"):
        if token == "auto":
            token = self.storage.create_ingest_token(self.row["id"])
        return self.request(self.api.actual_port, "POST", "/api/v1/ingest", body,
                            {"Authorization": "Bearer " + token,
                             "Content-Type": "application/json"})

    def admin_request(self, method, path, body):
        return self.request(self.admin.actual_port, method, path, body,
                            {"Authorization": "Bearer admin-token-0123456789",
                             "Content-Type": "application/json"})


class NestedBodyOnTheServersTests(ServerBase):
    NESTED = b"[" * 8000 + b"]" * 8000

    ADMIN_ROUTES = (("PUT", "/api/admin/display"), ("PUT", "/api/admin/source-order"),
                    ("POST", "/api/admin/sources"))

    def test_ingest_answers_400_for_a_body_nested_too_deeply(self):
        # Whether the parser or the envelope check refuses it depends on the
        # platform's recursion limit; either way the answer is a clean 400.
        status, body = self.ingest(self.NESTED)
        self.assertEqual(status, 400)
        self.assertIn("error", json.loads(body))

    def test_ingest_reports_a_parser_out_of_stack_as_invalid_json(self):
        with patch("presto_ai_dash.api.json.loads", side_effect=RecursionError):
            status, body = self.ingest(b"{}")
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body), {"error": "invalid_json"})

    def test_ingest_still_answers_afterwards(self):
        self.ingest(self.NESTED)
        self.assertEqual(self.ingest(b"{}")[0], 400)

    def test_admin_answers_400_for_a_body_nested_too_deeply(self):
        for method, path in self.ADMIN_ROUTES:
            with self.subTest(path=path):
                status, body = self.admin_request(method, path, self.NESTED)
                self.assertEqual(status, 400, body)
                self.assertIn("error", json.loads(body))

    def test_admin_reports_a_parser_out_of_stack_as_invalid_json(self):
        for method, path in self.ADMIN_ROUTES:
            with self.subTest(path=path):
                with patch("presto_ai_dash.admin.json.loads", side_effect=RecursionError):
                    status, body = self.admin_request(method, path, b"{}")
                self.assertEqual(status, 400, body)
                self.assertEqual(json.loads(body), {"error": "invalid JSON"})

    def test_the_alerts_route_survives_the_deepest_body_it_accepts(self):
        body = b"[" * 120 + b"]" * 120  # the alert body limit is 256 bytes
        status, _ = self.request(self.api.actual_port, "POST", "/api/v1/alerts/" + self.row["id"],
                                 body, {"Authorization": "Bearer device-token-0123456789",
                                        "Content-Type": "application/json"})
        self.assertEqual(status, 400)


# -- SEC-031: an early rejection is answered, not reset ---------------------------

def read_to_end(sock):
    chunks = []
    while True:
        try:
            data = sock.recv(4096)
        except OSError:
            break
        if not data:
            break
        chunks.append(data)
    return b"".join(chunks)


class EarlyRejectionTests(ServerBase):
    """The client sends its headers, waits, then sends a body the server never read."""

    def late_body(self, headers, body, pause=0.25):
        sock = socket.create_connection(("127.0.0.1", self.api.actual_port), timeout=5)
        try:
            sock.sendall(headers)
            time.sleep(pause)  # the server has answered and is closing by now
            try:
                sock.sendall(body)
            except OSError:
                pass
            return read_to_end(sock)
        finally:
            sock.close()

    def head(self, extra=b"", length=None, token=None):
        lines = [b"POST /api/v1/ingest HTTP/1.1", b"Host: 127.0.0.1",
                 b"Content-Type: application/json"]
        if length is not None:
            lines.append(b"Content-Length: " + str(length).encode())
        if token:
            lines.append(b"Authorization: Bearer " + token.encode())
        lines.append(extra)
        return b"\r\n".join(line for line in lines if line is not None) + b"\r\n\r\n"

    def test_every_early_rejection_reaches_the_client(self):
        body = b'{"padding": "' + b"z" * 3000 + b'"}'
        cases = {
            "no token": (self.head(length=len(body)), body, b"401"),
            "wrong token": (self.head(length=len(body), token="x" * 24), body, b"401"),
            "too large": (self.head(length=20000), b"q" * 20000, b"413"),
            "chunked": (self.head(extra=b"Transfer-Encoding: chunked"), b"5\r\nhello\r\n0\r\n\r\n", b"400"),
        }
        for label, (headers, payload, code) in cases.items():
            for attempt in range(8):
                with self.subTest(label=label, attempt=attempt):
                    reply = self.late_body(headers, payload)
                    self.assertTrue(reply.startswith(b"HTTP/1.0 " + code), reply[:60])

    def test_a_chunked_body_is_named_as_chunked(self):
        reply = self.late_body(self.head(extra=b"Transfer-Encoding: chunked"),
                               b"5\r\nhello\r\n0\r\n\r\n")
        self.assertIn(b'"unsupported_transfer_encoding"', reply)

    def test_a_chunked_body_is_not_mistaken_for_a_bad_length(self):
        # No Content-Length at all: the answer must not be "invalid_length".
        reply = self.late_body(self.head(extra=b"Transfer-Encoding: chunked"), b"0\r\n\r\n", pause=0)
        self.assertNotIn(b"invalid_length", reply)

    def test_a_slow_client_is_not_held_for_long_after_the_answer(self):
        sock = socket.create_connection(("127.0.0.1", self.api.actual_port), timeout=5)
        self.addCleanup(sock.close)
        sock.sendall(self.head(length=20000))
        started = time.monotonic()
        reply = read_to_end(sock)  # the server closes its side after the answer
        self.assertTrue(reply.startswith(b"HTTP/1.0 413"))
        self.assertLess(time.monotonic() - started, 3)

    def test_what_is_drained_is_bounded(self):
        self.assertLessEqual(BoundedRequestHandler.drain_bytes, 256 * 1024)
        self.assertLessEqual(BoundedRequestHandler.drain_seconds, 2.0)

    def test_a_request_with_a_proper_client_is_not_slowed(self):
        started = time.monotonic()
        self.assertEqual(self.ingest(b"{}")[0], 400)
        self.assertLess(time.monotonic() - started, 1.0)


# -- SEC-015: a session log with a date that is no date ---------------------------

class ActivityTimestampTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.projects = Path(self.temp.name) / "projects"
        (self.projects / "proj").mkdir(parents=True)

    def write(self, *lines):
        (self.projects / "proj" / "a.jsonl").write_bytes("".join(lines).encode("utf-8"))

    def refresh(self):
        return ActivityReader(self.projects).refresh(NOW)

    def test_a_record_in_the_year_9999_does_not_take_the_source_offline(self):
        self.write(line(request_id="ok", seconds_ago=60),
                   line(request_id="future", raw_timestamp="9999-12-31T23:59:59Z"))
        result = self.refresh()
        self.assertTrue(result["available"], result)
        self.assertEqual(result["requests_5h"], 1)

    def test_a_future_record_is_not_counted_as_recent_for_ever(self):
        self.write(line(request_id="future", raw_timestamp="2099-01-01T00:00:00Z"))
        result = self.refresh()
        self.assertTrue(result["available"])
        self.assertEqual(result["requests_5h"], 0)
        self.assertEqual(result["tokens_5h"], 0)
        self.assertIsNone(result["last_request_at"])

    def test_a_record_a_little_ahead_of_the_clock_is_kept(self):
        ahead = iso(NOW + timedelta(hours=2))
        self.write(line(request_id="skew", raw_timestamp=ahead))
        self.assertEqual(self.refresh()["requests_5h"], 1)

    def test_a_record_far_in_the_past_is_ignored(self):
        self.write(line(request_id="old", raw_timestamp="0001-01-01T00:00:00Z"),
                   line(request_id="ok", seconds_ago=30))
        result = self.refresh()
        self.assertTrue(result["available"])
        self.assertEqual(result["requests_5h"], 1)

    def test_a_summary_that_raises_becomes_unavailable_not_an_exception(self):
        self.write(line(request_id="ok", seconds_ago=30))
        reader = ActivityReader(self.projects)
        with patch.object(reader, "_summary", side_effect=OSError(22, "Invalid argument")):
            result = reader.refresh(NOW)
        self.assertFalse(result["available"])
        self.assertNotIn("Invalid argument", json.dumps(result))


# -- SEC-016: a reset time that is a number but no date ---------------------------

class StatusLineResetTimeTests(unittest.TestCase):
    def data(self, resets_at):
        return {"version": "2.1.296",
                "rate_limits": {"five_hour": {"used_percentage": 10.0, "resets_at": resets_at},
                                "seven_day": {"used_percentage": 20.0, "resets_at": 1900000000}}}

    def test_numbers_that_are_no_date_are_an_invalid_window(self):
        # 4e10 is the year 3170: Windows cannot make a date of it, Linux can.
        values = [1e20, 1e300, -1e20, 253402300800] + ([4e10] if os.name == "nt" else [])
        for value in values:
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    collector.build_envelope(self.data(value), "laptop")

    def test_a_real_reset_time_still_builds(self):
        envelope = collector.build_envelope(self.data(1900000000), "laptop")
        self.assertEqual(len(envelope["windows"]), 2)

    def run_main(self, raw):
        with tempfile.TemporaryDirectory() as temp:
            token = Path(temp) / "token"
            token.write_text("t" * 30, encoding="utf-8")
            args = ["claude-statusline", "--url", "http://127.0.0.1:1", "--token-file", str(token), "--host", "laptop"]
            output = io.StringIO()
            stdin = io.TextIOWrapper(io.BytesIO(raw.encode("utf-8")))
            with patch("sys.stdin", stdin), patch("sys.stdout", output):
                code = collector.main(args, state_path=str(Path(temp) / "state.json"))
        return code, output.getvalue()

    def test_the_status_line_still_answers_when_the_reset_time_is_no_date(self):
        for value in (1e20, 4e10):
            with self.subTest(value=value):
                code, text = self.run_main(json.dumps(self.data(value)))
                self.assertEqual(code, 0)
                self.assertTrue(text)

    def test_the_status_line_survives_a_document_nested_too_deeply(self):
        code, text = self.run_main("[" * 20000)
        self.assertEqual(code, 0)


# -- SEC-011: the typographic quotes that PowerShell reads as quotes ---------------

def parse_powershell_literal(text, start=0):
    """The value of the single-quoted PowerShell string at ``start`` and where it ends.

    PowerShell ends the string at any of the single-quote characters unless it
    is doubled, which is how this reads it.
    """
    quotes = collector._POWERSHELL_QUOTES
    assert text[start] in quotes
    opener = text[start]
    out, index = [], start + 1
    while index < len(text):
        char = text[index]
        if char in quotes:
            if index + 1 < len(text) and text[index + 1] == char:
                out.append(char)
                index += 2
                continue
            return "".join(out), index + 1
        out.append(char)
        index += 1
    raise AssertionError("the string was never closed")


class PowerShellQuotingTests(unittest.TestCase):
    NASTY = ["plain", "it's", "\u2018quoted\u2019", "a\u2019; New-Item -Path x; #b",
             "\u201a low", "\u201b high", "'\u2018\u2019\u201a\u201b'", "''", "C:/Users/O\u2019Brien/dash.db",
             "end'", "'start", "\u2019", ""]

    def test_every_quote_character_is_doubled(self):
        for value in self.NASTY:
            with self.subTest(value=value):
                quoted = "'" + collector.powershell_quote(value) + "'"
                parsed, end = parse_powershell_literal(quoted)
                self.assertEqual(parsed, value)
                self.assertEqual(end, len(quoted), "the literal ended early: %r" % quoted)

    def test_the_encoded_command_keeps_each_argument_in_one_piece(self):
        command = collector.powershell_snippet(
            "C:/x/script.py", "http://127.0.0.1:8787/api/v1/ingest", "C:/t/token",
            "host\u2019; calc; #", previous=("C:/Program Files/node.exe", "it's"))
        encoded = command.rsplit(" ", 1)[1]
        script = base64.b64decode(encoded).decode("utf-16-le")
        tail = script.split("& ", 1)[1]
        values, index = [], 0
        while index < len(tail):
            if tail[index] == " ":
                index += 1
                continue
            value, index = parse_powershell_literal(tail, index)
            values.append(value)
        self.assertIn("host\u2019; calc; #", values)
        self.assertIn("it's", values)
        # Every argument, and none made out of an injection: python, the script,
        # the mode, three options and their values, "--" and two chained words.
        self.assertEqual(len(values), 12)

    @unittest.skipUnless(os.name == "nt" and shutil.which("powershell"), "needs Windows PowerShell")
    def test_a_real_powershell_runs_no_injected_command(self):
        with tempfile.TemporaryDirectory() as temp:
            marker = Path(temp) / "pwned.txt"
            argv_file = Path(temp) / "argv.json"
            stub = Path(temp) / "stub.py"
            stub.write_text("import json, sys\n"
                            "json.dump(sys.argv[1:], open(%r, 'w'))\n"
                            % str(argv_file).replace("\\", "/"), encoding="utf-8")
            hostile = "x\u2019; New-Item -ItemType File -Path '%s' | Out-Null; #y" % str(marker).replace("\\", "/")
            command = collector.powershell_snippet(
                str(stub).replace("\\", "/"), "http://127.0.0.1:1/", "C:/t/token", hostile,
                python=sys.executable)
            result = subprocess.run(command.split(" "), input=b"", capture_output=True, timeout=60)
            self.assertFalse(marker.exists(), "an injected command ran: %r" % result.stderr[:200])
            received = json.loads(argv_file.read_text(encoding="utf-8"))
            self.assertEqual(received[received.index("--host") + 1], hostile)

    @unittest.skipUnless(os.name == "nt", "the Windows helper command")
    def test_the_helper_command_for_the_database_quotes_the_same_way(self):
        from presto_ai_dash import claude_export
        with tempfile.TemporaryDirectory() as temp:
            database = Path(temp) / "O\u2019Brien" / "dash.db"
            command = claude_export.helper_command(database, "abc123")
        script = base64.b64decode(command.rsplit(" ", 1)[1]).decode("utf-16-le")
        tail = script.split("& ", 1)[1]
        values, index = [], 0
        while index < len(tail):
            if tail[index] == " ":
                index += 1
                continue
            value, index = parse_powershell_literal(tail, index)
            values.append(value)
        self.assertTrue(any("O\u2019Brien" in value for value in values), values)


if __name__ == "__main__":
    unittest.main()
