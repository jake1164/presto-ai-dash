"""Tests for presto/net.py - URL building, headers and payload parsing."""

import unittest

import presto_path  # noqa: F401

from net import (
    EMBEDDED_SAMPLE,
    NetworkUnavailable,
    StatusClient,
    StatusError,
    Unauthorised,
    build_headers,
    build_url,
    embedded_payload,
    load_static_payload,
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
        self.assertEqual(
            build_headers("secret"),
            {"Authorization": "Bearer secret"})

    def test_empty_token_still_sends_a_header(self):
        headers = build_headers("")
        self.assertIn("Authorization", headers)
        self.assertEqual(headers["Authorization"], "Bearer ")


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


class TestEmbeddedSample(unittest.TestCase):

    def test_parses(self):
        payload = embedded_payload()
        self.assertEqual(payload["app"], "presto-ai-dash")

    def test_has_three_cards_in_plan_order(self):
        payload = embedded_payload()
        self.assertEqual(
            [c["id"] for c in payload["cards"]],
            ["dash", "ollama", "litellm"])

    def test_matches_the_committed_sample_file(self):
        import json
        import os
        path = os.path.join(
            os.path.dirname(presto_path.__file__), "..", "..",
            "examples", "sample-status.json")
        with open(os.path.abspath(path)) as handle:
            on_disk = json.load(handle)
        self.assertEqual(embedded_payload(), on_disk)


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
        self.assertEqual(calls[0][1], {"Authorization": "Bearer tok"})
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


class TestLoadStaticPayload(unittest.TestCase):

    def test_missing_file(self):
        with self.assertRaises(StatusError):
            load_static_payload("/definitely/not/here.json")

    def test_reads_a_real_file(self):
        import json
        import os
        import tempfile
        path = os.path.join(tempfile.gettempdir(), "pulse-static-test.json")
        with open(path, "w") as handle:
            json.dump({"cards": []}, handle)
        try:
            self.assertEqual(load_static_payload(path), {"cards": []})
        finally:
            os.remove(path)


if __name__ == "__main__":
    unittest.main()
