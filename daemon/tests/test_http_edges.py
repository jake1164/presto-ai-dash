"""Small protocol details of the daemon's two HTTP servers.

The Server header says nothing about the Python behind it, a HEAD answer has
no body, the server's own error answers are plain text with the safety
headers, a Content-Length is plain digits, and the admin page's Origin check
compares ports exactly.
"""

import json
from pathlib import Path
import socket
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from presto_ai_dash.admin import origin_is_allowed
from presto_ai_dash.api import parse_length
from test_input_hardening import ServerBase

CRLF = bytes((13, 10))


def parse(answer):
    head, _, body = answer.partition(CRLF + CRLF)
    lines = head.split(CRLF)
    headers = {}
    for line in lines[1:]:
        name, _, value = line.partition(b":")
        headers[name.strip().lower().decode("ascii")] = value.strip().decode("latin-1")
    return lines[0].decode("ascii"), headers, body


class RawServerBase(ServerBase):
    def raw(self, port, method, path, headers=None, host="127.0.0.1"):
        lines = ["%s %s HTTP/1.1" % (method, path), "Host: " + host]
        lines += ["%s: %s" % item for item in (headers or {}).items()]
        data = CRLF.join(line.encode("latin-1") for line in lines) + CRLF + CRLF
        with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
            sock.sendall(data)
            chunks = []
            try:
                while True:
                    chunk = sock.recv(65536)
                    if not chunk:
                        break
                    chunks.append(chunk)
            except socket.timeout:
                pass
        return parse(b"".join(chunks))

    def servers(self):
        return (("status", self.api.actual_port), ("admin", self.admin.actual_port))


class ServerHeaderTests(RawServerBase):
    def test_the_server_header_names_no_python_version(self):
        for name, port in self.servers():
            for method in ("GET", "OPTIONS"):
                with self.subTest(server=name, method=method):
                    _, headers, _ = self.raw(port, method, "/nothing")
                    self.assertEqual(headers.get("server"), "presto-ai-dash")


class HeadTests(RawServerBase):
    def test_a_head_answer_has_no_body(self):
        status, headers, body = self.raw(self.api.actual_port, "HEAD", "/nothing")
        self.assertIn("404", status)
        self.assertEqual(body, b"")
        # The length is the one a GET would send, as HEAD requires.
        self.assertGreater(int(headers["content-length"]), 0)

    def test_the_same_path_by_get_has_that_body(self):
        _, headers, body = self.raw(self.api.actual_port, "GET", "/nothing")
        self.assertEqual(len(body), int(headers["content-length"]))


class ErrorAnswerTests(RawServerBase):
    def test_the_servers_own_errors_are_plain_text_with_the_safety_headers(self):
        for name, port in self.servers():
            with self.subTest(server=name):
                status, headers, body = self.raw(port, "OPTIONS", "/")
                self.assertIn("501", status)
                self.assertTrue(headers["content-type"].startswith("text/plain"))
                self.assertEqual(headers["x-content-type-options"], "nosniff")
                self.assertIn("default-src 'none'", headers["content-security-policy"])
                self.assertEqual(headers["cache-control"], "no-store")
                self.assertNotIn(b"<", body)
                self.assertEqual(len(body), int(headers["content-length"]))

    def test_an_error_never_repeats_what_the_client_sent(self):
        for name, port in self.servers():
            with self.subTest(server=name):
                _, _, body = self.raw(port, "NOSUCHMETHOD", "/")
                self.assertNotIn(b"NOSUCHMETHOD", body)

    def test_a_head_error_has_no_body(self):
        status, _, body = self.raw(self.admin.actual_port, "HEAD", "/api/admin/status")
        self.assertEqual(body, b"")

    def test_an_overlong_request_line_is_a_plain_414(self):
        for name, port in self.servers():
            with self.subTest(server=name):
                status, headers, body = self.raw(port, "GET", "/" + "a" * 70000)
                self.assertIn("414", status)
                self.assertTrue(headers["content-type"].startswith("text/plain"))


class ContentLengthTests(RawServerBase):
    def test_only_plain_digits_are_a_length(self):
        for value in ("5", "0", "0123"):
            self.assertEqual(parse_length(value), int(value))
        for value in ("+5", "-5", " 5", "5 ", "1_0", "0x10", "5.0", "", "1e3", "٥",
                      "9" * 13):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    parse_length(value)
        self.assertEqual(parse_length(None, "0"), 0)
        with self.assertRaises(ValueError):
            parse_length(None)

    def test_the_ingest_route_refuses_a_signed_or_underscored_length(self):
        token = self.storage.create_ingest_token(self.row["id"])
        for value in ("+2", "1_2", "0x2"):
            with self.subTest(value=value):
                _, _, body = self.raw(self.api.actual_port, "POST", "/api/v1/ingest",
                                      {"Authorization": "Bearer " + token,
                                       "Content-Type": "application/json",
                                       "Content-Length": value})
                self.assertEqual(json.loads(body), {"error": "invalid_length"})

    def test_the_admin_routes_refuse_it_too(self):
        for value in ("+2", "1_2"):
            with self.subTest(value=value):
                status, _, body = self.raw(self.admin.actual_port, "PUT", "/api/admin/display",
                                           {"Authorization": "Bearer admin-token-0123456789",
                                            "Content-Type": "application/json",
                                            "Content-Length": value},
                                           host="127.0.0.1:%d" % self.admin.actual_port)
                self.assertIn("400", status, body)


class OriginPortTests(unittest.TestCase):
    NAMES = ("dash.lan:8788", "proxy.lan")

    def check(self, origin, host, port=8788):
        return origin_is_allowed(origin, host, port, self.NAMES)

    def test_the_same_name_and_port_is_accepted(self):
        self.assertTrue(self.check("http://dash.lan:8788", "dash.lan:8788"))
        self.assertTrue(self.check("http://proxy.lan", "proxy.lan"))
        self.assertTrue(self.check("https://proxy.lan", "proxy.lan"))
        self.assertTrue(self.check("https://proxy.lan:443", "proxy.lan"))
        self.assertTrue(self.check("http://proxy.lan:80", "proxy.lan"))

    def test_a_host_with_a_port_wants_that_port_in_the_origin(self):
        self.assertFalse(self.check("http://dash.lan", "dash.lan:8788"))
        self.assertFalse(self.check("https://dash.lan", "dash.lan:8788"))
        self.assertFalse(self.check("http://dash.lan:9999", "dash.lan:8788"))

    def test_a_host_without_a_port_refuses_an_origin_with_another(self):
        self.assertFalse(self.check("http://proxy.lan:3000", "proxy.lan"))
        self.assertFalse(self.check("https://proxy.lan:8443", "proxy.lan"))

    def test_another_name_scheme_or_nothing_useful_is_refused(self):
        self.assertFalse(self.check("http://evil.example", "dash.lan:8788"))
        self.assertFalse(self.check("ftp://proxy.lan", "proxy.lan"))
        self.assertFalse(self.check("null", "proxy.lan"))
        self.assertFalse(self.check("http://", "proxy.lan"))

    def test_no_origin_is_still_allowed_and_loopback_stays_exact(self):
        self.assertTrue(self.check(None, "dash.lan:8788"))
        self.assertTrue(self.check("http://127.0.0.1:8788", "127.0.0.1:8788"))
        self.assertFalse(self.check("https://127.0.0.1:8788", "127.0.0.1:8788"))
        self.assertFalse(self.check("http://127.0.0.1:9", "127.0.0.1:8788"))


if __name__ == "__main__":
    unittest.main()
