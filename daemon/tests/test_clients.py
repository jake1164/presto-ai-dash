"""The client registry: who fetches the status endpoint, and what it says.

The daemon has one shared token, so authentication cannot tell clients apart.
Each client declares a name in a request header; the daemon remembers what it
saw, in memory only. Hostile header values must never reach the page as
anything but "unknown".
"""

import json
from pathlib import Path
import sys
import unittest
from urllib.error import HTTPError
from urllib.request import Request, build_opener, ProxyHandler

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from presto_ai_dash.api import ApiServer
from presto_ai_dash.clients import MAX_CLIENTS, ClientRegistry, sanitize_header
from test_management import ManagementBase


class SanitizeHeaderTests(unittest.TestCase):
    """A header value is kept only when it is short printable ASCII."""

    def test_a_plain_name_is_kept(self):
        self.assertEqual(sanitize_header("presto"), "presto")
        self.assertEqual(sanitize_header("psl"), "psl")

    def test_a_missing_or_empty_value_is_unknown(self):
        for value in (None, "", "   "):
            with self.subTest(value=value):
                self.assertEqual(sanitize_header(value), "unknown")

    def test_a_value_that_is_not_a_string_is_unknown(self):
        for value in (12, [], {}, b"presto", True):
            with self.subTest(value=value):
                self.assertEqual(sanitize_header(value), "unknown")

    def test_a_long_value_is_unknown(self):
        self.assertEqual(sanitize_header("a" * 32), "a" * 32)
        self.assertEqual(sanitize_header("a" * 33), "unknown")

    def test_control_characters_make_the_value_unknown(self):
        for value in ("presto\n", "pre\x00sto", "pre\tsto", "pre\rsto", "pr\xc3\xa9sto"):
            with self.subTest(value=value):
                self.assertEqual(sanitize_header(value), "unknown")

    def test_markup_is_kept_as_text_but_never_filtered(self):
        # The page writes every value with textContent, so markup stays inert.
        self.assertEqual(sanitize_header("<b>x</b>"), "<b>x</b>")
        self.assertEqual(sanitize_header("<script>" + "a" * 40), "unknown")


class ClientRegistryTests(unittest.TestCase):
    """One record per (name, address), at most sixteen, newest first."""

    def setUp(self):
        self.registry = ClientRegistry()
        self.now = 1000.0

    def record(self, name="presto", address="10.0.0.5", revision="abc123"):
        self.now += 1
        return self.registry.record(name, address, revision, now=self.now)

    def test_a_first_fetch_creates_the_record(self):
        record = self.record()
        self.assertEqual(record["name"], "presto")
        self.assertEqual(record["address"], "10.0.0.5")
        self.assertEqual(record["fetches"], 1)
        self.assertEqual(record["revision"], "abc123")
        self.assertEqual(record["first_seen"], record["last_seen"])

    def test_a_second_fetch_counts_and_keeps_the_first_seen(self):
        first = self.record()
        second = self.record()
        self.assertEqual(second["fetches"], 2)
        self.assertEqual(second["first_seen"], first["first_seen"])
        self.assertGreater(second["last_seen"], first["last_seen"])

    def test_the_same_name_from_another_address_is_another_record(self):
        self.record(address="10.0.0.5")
        self.record(address="10.0.0.6")
        self.assertEqual(len(self.registry.clients()), 2)

    def test_a_hostile_name_is_recorded_as_unknown(self):
        self.record(name="bad\x00name")
        clients = self.registry.clients()
        self.assertEqual(clients[0]["name"], "unknown")

    def test_at_most_sixteen_clients_are_kept(self):
        for index in range(MAX_CLIENTS + 4):
            self.record(name="client-%d" % index)
        clients = self.registry.clients()
        self.assertEqual(len(clients), MAX_CLIENTS)
        # The least recently seen clients are the ones that were dropped.
        names = [client["name"] for client in clients]
        self.assertIn("client-19", names)
        self.assertNotIn("client-0", names)
        self.assertNotIn("client-3", names)

    def test_a_client_is_connected_until_three_poll_intervals_pass(self):
        self.record()
        moment = self.now
        connected = self.registry.clients(now=moment, poll_interval=15)
        self.assertTrue(connected[0]["connected"])
        connected = self.registry.clients(now=moment + 45, poll_interval=15)
        self.assertTrue(connected[0]["connected"])
        disconnected = self.registry.clients(now=moment + 46, poll_interval=15)
        self.assertFalse(disconnected[0]["connected"])

    def test_the_clients_are_listed_newest_first(self):
        self.record(name="older")
        self.record(name="newer")
        self.assertEqual([client["name"] for client in self.registry.clients()],
                         ["newer", "older"])

    def test_nothing_is_kept_after_a_restart(self):
        self.record()
        self.assertEqual(ClientRegistry().clients(), [])


class ApiClientRecordingTests(ManagementBase):
    """The status route remembers its clients without changing its answer."""

    def setUp(self):
        super().setUp()
        self.registry = ClientRegistry()
        self.api = ApiServer("127.0.0.1", 0, self.storage, "device-token",
                             status_provider=self.manager.status_payload,
                             client_registry=self.registry)
        self.api.start()
        self.addCleanup(self.api.stop)
        self.base_url = "http://127.0.0.1:%d" % self.api.actual_port
        self.opener = build_opener(ProxyHandler({}))

    def status(self, headers=None):
        extra = {"Authorization": "Bearer device-token"}
        extra.update(headers or {})
        request = Request(self.base_url + "/api/v1/status", headers=extra)
        try:
            response = self.opener.open(request, timeout=3)
        except HTTPError as exc:
            response = exc
        with response:
            return response.status, json.loads(response.read())

    def test_a_fetch_is_recorded_with_its_name_and_revision(self):
        status, payload = self.status({"X-Presto-Client": "presto",
                                        "X-Presto-Revision": "abc123"})
        self.assertEqual(status, 200)
        self.assertIn("cards", payload)
        clients = self.registry.clients()
        self.assertEqual(len(clients), 1)
        self.assertEqual(clients[0]["name"], "presto")
        self.assertEqual(clients[0]["revision"], "abc123")
        self.assertEqual(clients[0]["fetches"], 1)

    def test_the_response_is_unchanged_by_the_client_header(self):
        _, first = self.status()
        _, second = self.status({"X-Presto-Client": "presto"})
        self.assertEqual(first, second)

    def test_a_refused_request_is_not_counted_as_a_client(self):
        request = Request(self.base_url + "/api/v1/status",
                          headers={"Authorization": "Bearer wrong"})
        try:
            self.opener.open(request, timeout=3)
        except HTTPError as exc:
            with exc:
                pass
        self.assertEqual(self.registry.clients(), [])

    def test_a_hostile_client_name_never_reaches_the_registry(self):
        self.status({"X-Presto-Client": "evil\x00name"})
        self.assertEqual(self.registry.clients()[0]["name"], "unknown")


if __name__ == "__main__":
    unittest.main()
