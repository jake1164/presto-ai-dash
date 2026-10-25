"""Tests for presto/net.py - URL building, headers and payload parsing."""

import asyncio
import unittest

import presto_path  # noqa: F401

import net
from net import (
    NetworkUnavailable,
    StatusClient,
    StatusError,
    Unauthorised,
    build_diag,
    build_headers,
    build_url,
    parse_payload,
)


class FakeResponse(object):
    def __init__(self, status_code=200, text="{}"):
        self.status_code = status_code
        self.text = text
        self.closed = False

    def close(self):
        self.closed = True


class TestBuildUrl(unittest.TestCase):

    def test_basic(self):
        self.assertEqual(
            build_url("host", 8787), "http://host:8787/api/v1/status")

    def test_custom_path(self):
        self.assertEqual(
            build_url("host", 80, "/other"), "http://host:80/other")

    def test_path_without_leading_slash(self):
        self.assertEqual(
            build_url("host", 80, "other"), "http://host:80/other")


class TestBuildHeaders(unittest.TestCase):

    def test_bearer_token(self):
        headers = build_headers("secret")
        self.assertEqual(headers["Authorization"], "Bearer secret")

    def test_empty_token_still_sends_a_header(self):
        headers = build_headers("")
        self.assertIn("Authorization", headers)
        self.assertEqual(headers["Authorization"], "Bearer ")

    def test_every_request_names_the_client_and_the_revision(self):
        headers = build_headers("secret")
        self.assertEqual(headers["X-Presto-Client"], net.current_client_name())
        self.assertEqual(net.DEFAULT_CLIENT_NAME, "presto")
        self.assertIn("X-Presto-Revision", headers)

    def test_the_client_name_comes_from_the_device_settings(self):
        original = net.CLIENT_NAME_OVERRIDE
        net.CLIENT_NAME_OVERRIDE = "kitchen-presto"
        try:
            self.assertEqual(build_headers("t")["X-Presto-Client"], "kitchen-presto")
        finally:
            net.CLIENT_NAME_OVERRIDE = original

    def test_a_hostile_client_name_is_not_sent(self):
        self.assertEqual(net.safe_label("bad\x00name"), "unknown")
        self.assertEqual(net.safe_label("a" * 33), "unknown")
        self.assertEqual(net.safe_label(""), "unknown")
        self.assertEqual(net.safe_label(None), "unknown")
        self.assertEqual(net.safe_label("presto"), "presto")

    def test_the_revision_override_is_used(self):
        original = net.REVISION_OVERRIDE
        net.REVISION_OVERRIDE = "abc123456789"
        try:
            self.assertEqual(build_headers("t")["X-Presto-Revision"], "abc123456789")
        finally:
            net.REVISION_OVERRIDE = original

    def test_a_missing_revision_file_is_unknown(self):
        self.assertEqual(net.read_revision("no-such-revision-file.txt"), "unknown")

    def test_the_installed_revision_file_is_read(self):
        import os
        import tempfile
        handle, path = tempfile.mkstemp(suffix=".txt")
        try:
            with os.fdopen(handle, "w") as stream:
                stream.write("abc123456789\n")
            self.assertEqual(net.read_revision(path), "abc123456789")
        finally:
            os.remove(path)


class TestParsePayload(unittest.TestCase):

    def test_object(self):
        self.assertEqual(parse_payload('{"a": 1}'), {"a": 1})

    def test_invalid_json(self):
        with self.assertRaises(StatusError):
            parse_payload("not json")

    def test_list_is_rejected(self):
        with self.assertRaises(StatusError):
            parse_payload("[1, 2, 3]")

    def test_bare_string_is_rejected(self):
        with self.assertRaises(StatusError):
            parse_payload('"hello"')


class TestStatusClient(unittest.TestCase):

    def _client(self, response=None, exc=None):
        calls = []

        def transport(url, headers, timeout):
            calls.append((url, headers, timeout))
            if exc is not None:
                raise exc
            return response

        client = StatusClient("host", 8787, "tok", timeout=5,
                              transport=transport)
        return client, calls

    def test_success(self):
        client, calls = self._client(FakeResponse(200, '{"cards": []}'))
        payload = client.fetch()
        self.assertEqual(payload, {"cards": []})
        self.assertEqual(calls[0][0], "http://host:8787/api/v1/status")
        self.assertEqual(calls[0][1]["Authorization"], "Bearer tok")
        self.assertEqual(calls[0][1]["X-Presto-Client"], "presto")
        self.assertIn("X-Presto-Revision", calls[0][1])
        self.assertEqual(calls[0][2], 5)

    def test_response_is_closed(self):
        response = FakeResponse(200, '{"cards": []}')
        client, _ = self._client(response)
        client.fetch()
        self.assertTrue(response.closed)

    def test_401_raises_unauthorised(self):
        response = FakeResponse(401, "{}")
        client, _ = self._client(response)
        with self.assertRaises(Unauthorised):
            client.fetch()
        self.assertTrue(response.closed)

    def test_500_raises_status_error(self):
        client, _ = self._client(FakeResponse(500, "oops"))
        with self.assertRaises(StatusError):
            client.fetch()

    def test_404_raises_status_error(self):
        client, _ = self._client(FakeResponse(404, "nope"))
        with self.assertRaises(StatusError):
            client.fetch()

    def test_oserror_is_network_unavailable(self):
        client, _ = self._client(exc=OSError("no route"))
        with self.assertRaises(NetworkUnavailable):
            client.fetch()

    def test_generic_exception_is_network_unavailable(self):
        client, _ = self._client(exc=ValueError("weird"))
        with self.assertRaises(NetworkUnavailable):
            client.fetch()

    def test_missing_status_code(self):
        client, _ = self._client(FakeResponse(None, "{}"))
        with self.assertRaises(StatusError):
            client.fetch()

    def test_bad_body_raises_status_error(self):
        response = FakeResponse(200, "garbage")
        client, _ = self._client(response)
        with self.assertRaises(StatusError):
            client.fetch()
        self.assertTrue(response.closed)


class TestBuildDiag(unittest.TestCase):
    """The diagnostics header the device builds: short, ASCII and bounded."""

    def test_the_documented_example(self):
        self.assertEqual(
            build_diag({"err": "OSError 110", "st": "conn", "n": 3,
                        "free": 41, "up": 312, "rssi": -61, "drops": 1}),
            "err=OSError 110;st=conn;n=3;free=41;up=312;rssi=-61;drops=1")

    def test_the_pairs_are_written_in_a_fixed_order(self):
        value = build_diag({"up": 312, "n": 3, "st": "conn"})
        self.assertEqual(value, "st=conn;n=3;up=312")

    def test_a_missing_pair_is_left_out(self):
        self.assertEqual(build_diag({"n": 3}), "n=3")

    def test_an_empty_state_is_no_header(self):
        self.assertIsNone(build_diag({}))
        self.assertIsNone(build_diag(None))
        self.assertIsNone(build_diag("not a dict"))

    def test_every_pair_at_its_maximum_still_fits(self):
        value = build_diag({"err": "A" * 24, "st": "parse", "n": 999999,
                            "free": 65535, "up": 99999999, "rssi": -127,
                            "drops": 9999})
        self.assertLessEqual(len(value), net.DIAG_MAX_CHARS)
        self.assertIn("err=" + "A" * 24, value)
        self.assertIn("n=999999", value)

    def test_the_value_is_never_longer_than_the_daemon_accepts(self):
        for state in ({"err": "OSError 110", "st": "conn", "n": 999999,
                       "free": 65535, "up": 99999999, "rssi": -127,
                       "drops": 9999},
                      {"err": "A" * 24, "up": 99999999},
                      {"n": 1, "free": 65535, "up": 99999999}):
            with self.subTest(state=state):
                self.assertLessEqual(len(build_diag(state)), net.DIAG_MAX_CHARS)

    def test_the_lowest_priority_pairs_are_left_out_first(self):
        state = {"err": "OSError 110", "st": "conn", "n": 3, "free": 41,
                 "up": 312, "rssi": -61, "drops": 1}
        # The whole thing is 59 characters long.
        self.assertEqual(len(build_diag(state)), 59)
        # drops goes first, then the signal, then the uptime.
        self.assertNotIn("drops", build_diag(state, limit=57))
        self.assertIn("rssi", build_diag(state, limit=57))
        self.assertNotIn("rssi", build_diag(state, limit=50))
        self.assertIn("up", build_diag(state, limit=50))
        self.assertNotIn("up", build_diag(state, limit=40))
        # The important pairs are never the ones dropped.
        self.assertIn("err=OSError 110", build_diag(state, limit=40))
        self.assertIn("st=conn", build_diag(state, limit=40))

    def test_a_label_the_daemon_would_not_accept_is_left_out(self):
        for label in ("<script>alert(1)</script>", "OS Error", "OSError;rm",
                      "OSError 12345", "", "OSError 110 220", "OSError 1-23",
                      "OSError 1 2"):
            with self.subTest(label=label):
                value = build_diag({"err": label, "n": 1})
                self.assertNotIn("err", value)

    def test_an_unknown_stage_is_left_out(self):
        self.assertEqual(build_diag({"st": "elsewhere", "n": 1}), "n=1")

    def test_a_number_that_is_not_a_number_is_left_out(self):
        self.assertEqual(build_diag({"n": "3", "free": 41}), "free=41")

    def test_a_boolean_is_not_a_number(self):
        self.assertEqual(build_diag({"n": True}), None)

    def test_the_value_is_printable_ascii(self):
        value = build_diag({"err": "OSError 110", "st": "conn", "n": 3,
                            "free": 41, "up": 312, "rssi": -61, "drops": 1})
        for char in value:
            self.assertTrue(32 <= ord(char) < 127)


class TestErrorLabel(unittest.TestCase):
    """The label says what went wrong, never what the text says."""

    def test_the_type_name_is_used(self):
        self.assertEqual(net.error_label(ValueError("x")), "ValueError")
        self.assertEqual(net.error_label(MemoryError("x")), "MemoryError")

    def test_an_errno_is_used_too(self):
        label = net.error_label(OSError(110, "timed out"))
        self.assertTrue(label.endswith(" 110"), label)

    def test_the_exception_text_is_never_used(self):
        label = net.error_label(ValueError("Bearer abc123 http://10.0.0.5"))
        self.assertEqual(label, "ValueError")
        self.assertNotIn("abc123", label)
        self.assertNotIn("10.0.0.5", label)

    def test_the_label_is_short_enough_for_the_daemon(self):
        for exc in (ValueError("x"), OSError(110, "x"), MemoryError("x")):
            with self.subTest(exc=exc):
                self.assertLessEqual(len(net.error_label(exc)), net.DIAG_MAX_ERR)


class TestDiagHeader(unittest.IsolatedAsyncioTestCase):
    """Every request the device makes carries the diagnostics."""

    async def _server(self, reply):
        self.requests = []

        async def handle(reader, writer):
            try:
                self.requests.append(await reader.readuntil(b"\r\n\r\n"))
            except OSError:
                pass
            try:
                writer.write(reply)
                await writer.drain()
            except OSError:
                pass
            writer.close()

        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        self.addAsyncCleanup(server.wait_closed)
        self.addCleanup(server.close)
        return server.sockets[0].getsockname()[1]

    async def test_the_get_carries_the_diagnostics(self):
        port = await self._server(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}")
        client = StatusClient("127.0.0.1", port, "tok", timeout=2,
                              diag_provider=lambda: {"n": 3, "free": 41,
                                                     "up": 312})
        await client.fetch_async()
        self.assertIn(b"X-Presto-Diag: n=3;free=41;up=312\r\n", self.requests[0])

    async def test_the_post_carries_the_diagnostics(self):
        body = b'{"enabled": true}'
        reply = (b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n" % len(body)) + body
        port = await self._server(reply)
        client = StatusClient("127.0.0.1", port, "tok", timeout=2,
                              diag_provider=lambda: {"err": "OSError 110",
                                                     "st": "conn", "n": 1})
        await client.post_alert("claude", True)
        self.assertIn(b"X-Presto-Diag: err=OSError 110;st=conn;n=1\r\n",
                      self.requests[0])

    async def test_a_request_without_diagnostics_looks_as_it_did(self):
        port = await self._server(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}")
        client = StatusClient("127.0.0.1", port, "tok", timeout=2)
        await client.fetch_async()
        self.assertNotIn(b"X-Presto-Diag", self.requests[0])
        self.assertIn(b"X-Presto-Client: presto\r\n", self.requests[0])

    async def test_a_probe_that_raises_does_not_break_the_fetch(self):
        port = await self._server(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}")

        def broken():
            raise RuntimeError("no probe here")

        client = StatusClient("127.0.0.1", port, "tok", timeout=2,
                              diag_provider=broken)
        self.assertEqual(await client.fetch_async(), {})
        self.assertNotIn(b"X-Presto-Diag", self.requests[0])

    async def test_a_failure_is_reported_by_the_next_request(self):
        # The first request cannot reach the daemon, so it carries nothing
        # useful. The second one works, and it carries what the first one
        # left behind.
        client = None

        def provider():
            return {"err": client.last_error, "st": client.last_stage,
                    "n": 1}

        client = StatusClient("127.0.0.1", 1, "tok", timeout=.2,
                              diag_provider=provider)
        with self.assertRaises(NetworkUnavailable):
            await client.fetch_async()
        self.assertEqual(client.last_stage, "conn")
        self.assertTrue(client.last_error)

        port = await self._server(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}")
        client.host = "127.0.0.1"
        client.port = port
        client.timeout = 2
        await client.fetch_async()
        self.assertIn(b"X-Presto-Diag: err=", self.requests[0])
        self.assertIn(b"st=conn", self.requests[0])


if __name__ == "__main__":
    unittest.main()
