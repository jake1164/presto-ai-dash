"""Tests for presto/net.py - URL building, headers and payload parsing."""

import unittest

import presto_path  # noqa: F401

import net
from net import (
    NetworkUnavailable,
    StatusClient,
    StatusError,
    Unauthorised,
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


if __name__ == "__main__":
    unittest.main()
