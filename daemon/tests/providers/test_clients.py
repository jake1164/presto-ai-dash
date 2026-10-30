"""The client registry: who fetches the status endpoint, and what it says.

The daemon has one shared token, so authentication cannot tell clients apart.
Each client declares a name in a request header; the daemon remembers what it
saw, in memory only. Hostile header values must never reach the page as
anything but "unknown", and the diagnostics header must never carry anything
that was not checked against a whitelist.
"""

import json
import logging
from pathlib import Path
import sys
import unittest
from urllib.error import HTTPError
from urllib.request import Request, build_opener, ProxyHandler

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from presto_ai_dash.api import ApiServer
from presto_ai_dash.clients import (MAX_AGE_SECONDS, MAX_CLIENTS, MAX_EVENTS, ClientRegistry, parse_diag,
                                    sanitize_header)
from admin.test_management import ManagementBase


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
        # Fixed, so entries recorded at made-up small times are not a real day old.
        self.registry = ClientRegistry(clock=lambda: 1000.0)
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


class ParseDiagTests(unittest.TestCase):
    """The diagnostics header is untrusted input: a whitelist, not a hint."""

    def test_the_documented_example_is_accepted(self):
        parsed = parse_diag("err=OSError 110;st=conn;n=3;free=41;up=312;rssi=-61;drops=1")
        self.assertEqual(parsed, {"err": "OSError 110", "st": "conn", "n": 3,
                                  "free": 41, "up": 312, "rssi": -61, "drops": 1})

    def test_a_missing_header_is_nothing(self):
        self.assertIsNone(parse_diag(None))
        self.assertIsNone(parse_diag(""))

    def test_a_header_over_the_limit_is_refused_whole(self):
        self.assertIsNone(parse_diag("err=OSError 110;" + "n=1" * 60))

    def test_a_header_with_a_control_character_is_refused_whole(self):
        for value in ("err=OSError\x00", "err=OSError\n110", "err=OSError\r"):
            with self.subTest(value=value):
                self.assertIsNone(parse_diag(value))

    def test_a_header_that_is_not_ascii_is_refused_whole(self):
        self.assertIsNone(parse_diag("err=OSErroré"))

    def test_an_unknown_key_is_ignored(self):
        parsed = parse_diag("err=OSError;secret=token;n=2")
        self.assertEqual(parsed, {"err": "OSError", "n": 2})

    def test_a_repeated_key_is_ignored(self):
        parsed = parse_diag("n=1;n=2;err=OSError;err=Other")
        self.assertEqual(parsed, {"n": 1, "err": "OSError"})

    def test_a_malformed_number_is_dropped(self):
        for value in ("n=abc", "n=", "n=1.5", "n=+3", "n=1_0", "n= 3"):
            with self.subTest(value=value):
                self.assertNotIn("n", parse_diag(value) or {})

    def test_an_out_of_range_number_is_dropped_and_not_clamped(self):
        for value, key in (("n=1000000", "n"), ("free=65536", "free"),
                           ("up=100000000", "up"), ("rssi=1", "rssi"),
                           ("rssi=-128", "rssi"), ("drops=10000", "drops")):
            with self.subTest(value=value):
                self.assertNotIn(key, parse_diag(value) or {})

    def test_the_edge_values_are_accepted(self):
        parsed = parse_diag("n=999999;free=65535;up=99999999;rssi=-127;drops=9999")
        self.assertEqual(parsed["n"], 999999)
        self.assertEqual(parsed["free"], 65535)
        self.assertEqual(parsed["up"], 99999999)
        self.assertEqual(parsed["rssi"], -127)
        self.assertEqual(parsed["drops"], 9999)
        self.assertEqual(parse_diag("n=0;free=0;up=0;rssi=0;drops=0")["rssi"], 0)

    def test_the_stage_must_be_one_the_daemon_knows(self):
        self.assertEqual(parse_diag("st=parse")["st"], "parse")
        self.assertNotIn("st", parse_diag("st=elsewhere") or {})

    def test_a_hostile_error_label_is_dropped(self):
        # A part that is not a pair at all is refused whole, so nothing of it
        # can reach the page or the log.
        self.assertIsNone(parse_diag("err=x;rm -rf"))
        self.assertIsNone(parse_diag("err=x;rm -rf /"))
        # A pair with a value that is not a label is dropped on its own.
        self.assertNotIn("err", parse_diag("err=<script>alert(1)</script>") or {})

    def test_a_very_long_error_label_is_dropped(self):
        self.assertNotIn("err", parse_diag("err=" + "a" * 5000) or {})

    def test_an_error_label_with_markup_is_dropped(self):
        for value in ("err=<script>alert(1)</script>", "err=OS Error",
                      "err=OSError;rm"):
            with self.subTest(value=value):
                self.assertNotIn("err", parse_diag(value) or {})

    def test_an_error_label_with_an_errno_is_accepted(self):
        self.assertEqual(parse_diag("err=OSError 110")["err"], "OSError 110")
        self.assertEqual(parse_diag("err=OSError -2")["err"], "OSError -2")

    def test_a_pair_without_a_value_is_ignored(self):
        self.assertEqual(parse_diag("=5;n=1"), {"n": 1})


class ClientRegistryDiagTests(unittest.TestCase):
    """The diagnostics of a client, kept in memory and never logged twice."""

    def setUp(self):
        # Fixed, so entries recorded at made-up small times are not a real day old.
        self.registry = ClientRegistry(clock=lambda: 1000.0)
        self.now = 1000.0
        self.logger = logging.getLogger("presto_ai_dash.clients")
        self.level = self.logger.level
        self.logger.setLevel(logging.INFO)
        self.lines = []
        self.handler = logging.Handler()
        self.handler.emit = lambda record: self.lines.append(record.getMessage())
        self.logger.addHandler(self.handler)
        self.addCleanup(self.logger.removeHandler, self.handler)
        self.addCleanup(self.logger.setLevel, self.level)

    def record(self, diag=None, name="presto", address="10.0.0.5"):
        self.now += 1
        return self.registry.record(name, address, "abc123", now=self.now, diag=diag)

    def test_a_poll_stores_the_diagnostics(self):
        record = self.record("err=OSError 110;st=conn;n=3;free=41;up=312")
        self.assertEqual(record["diag"]["err"], "OSError 110")
        self.assertEqual(record["diag"]["n"], 3)
        self.assertEqual(self.registry.clients()[0]["diag"]["st"], "conn")

    def test_a_healthy_poll_keeps_the_last_error_and_stage(self):
        self.record("err=OSError 110;st=conn;n=3")
        record = self.record("n=4;free=41")
        self.assertEqual(record["diag"]["err"], "OSError 110")
        self.assertEqual(record["diag"]["st"], "conn")
        self.assertEqual(record["diag"]["n"], 4)

    def test_a_restarted_device_does_not_keep_the_old_error(self):
        self.record("err=OSError 110;st=conn;n=3;up=312")
        record = self.record("n=0;free=60;up=5")
        self.assertNotIn("err", record["diag"])
        self.assertNotIn("st", record["diag"])
        # A restart that reports only its uptime is recognised too.
        self.record("err=OSError 110;st=conn;up=900")
        record = self.record("up=4")
        self.assertNotIn("err", record["diag"])

    def test_a_poll_replaces_the_other_diagnostics(self):
        self.record("n=3;free=41;up=312")
        record = self.record("n=4")
        self.assertNotIn("free", record["diag"])
        self.assertNotIn("up", record["diag"])

    def test_a_hostile_header_leaves_nothing_behind(self):
        record = self.record("err=<script>alert(1)</script>")
        self.assertEqual(record["diag"], {})

    def test_one_log_line_per_change(self):
        self.record("err=OSError 110;st=conn;n=1")
        self.record("err=OSError 110;st=conn;n=1")
        self.record("err=OSError 110;st=conn;n=2")
        problems = [line for line in self.lines if "fetch problem" in line]
        self.assertEqual(len(problems), 2)
        self.assertIn("err=OSError 110", problems[0])
        self.assertIn("st=conn", problems[0])

    def test_a_rejected_value_is_never_logged(self):
        self.record("err=" + "a" * 5000)
        self.record("err=<script>alert(1)</script>")
        for line in self.lines:
            self.assertNotIn("script", line)
            self.assertNotIn("a" * 100, line)

    def test_a_healthy_client_says_nothing(self):
        self.record("n=0;free=41;up=312")
        self.assertEqual([line for line in self.lines if "fetch problem" in line], [])

    def test_the_diagnostics_survive_a_restart_only_in_memory(self):
        self.record("err=OSError 110")
        self.assertEqual(len(self.registry.clients()), 1)
        self.assertEqual(ClientRegistry().clients(), [])


class DeviceHeaderMatchesTheParserTests(unittest.TestCase):
    """What the device builds is exactly what the daemon accepts."""

    def setUp(self):
        import presto_path  # noqa: F401  (puts the device modules on the path)
        import net

        self.net = net

    def test_a_built_header_is_accepted_whole(self):
        state = {"err": "OSError 110", "st": "conn", "n": 3, "free": 41,
                 "up": 312, "rssi": -61, "drops": 1}
        value = self.net.build_diag(state)
        self.assertEqual(parse_diag(value), state)

    def test_a_built_header_is_never_too_long(self):
        state = {"err": "A" * 24, "st": "parse", "n": 999999, "free": 65535,
                 "up": 99999999, "rssi": -127, "drops": 9999}
        value = self.net.build_diag(state)
        self.assertLessEqual(len(value), 120)
        self.assertEqual(parse_diag(value), state)

    def test_the_daemon_drops_nothing_the_device_sent(self):
        for state in ({"n": 0, "free": 0, "up": 0, "rssi": 0, "drops": 0},
                      {"err": "MemoryError", "st": "parse"},
                      {"err": "TimeoutError", "st": "head", "n": 1},
                      {"rssi": -127, "drops": 9999, "up": 99999999}):
            with self.subTest(state=state):
                value = self.net.build_diag(state)
                self.assertIsNotNone(value)
                self.assertEqual(parse_diag(value), state)


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

    def test_the_diagnostics_header_is_kept(self):
        status, _ = self.status({"X-Presto-Client": "presto",
                                  "X-Presto-Diag": "err=OSError 110;st=conn;n=3;free=41"})
        self.assertEqual(status, 200)
        diag = self.registry.clients()[0]["diag"]
        self.assertEqual(diag["err"], "OSError 110")
        self.assertEqual(diag["st"], "conn")
        self.assertEqual(diag["n"], 3)
        self.assertEqual(diag["free"], 41)

    def test_the_response_is_unchanged_by_the_diagnostics_header(self):
        _, first = self.status()
        _, second = self.status({"X-Presto-Diag": "err=OSError 110;st=conn;n=3"})
        self.assertEqual(first, second)

    def test_a_hostile_diagnostics_header_never_reaches_the_registry(self):
        self.status({"X-Presto-Diag": "err=<script>alert(1)</script>;secret=token"})
        diag = self.registry.clients()[0]["diag"]
        self.assertEqual(diag, {})

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


class ClientFailureEventsTests(unittest.TestCase):
    """When the daemon heard that a device had failed more fetches."""

    def setUp(self):
        self.registry = ClientRegistry(clock=lambda: 5000.0)

    def poll(self, now, **diag):
        header = ";".join("%s=%s" % pair for pair in diag.items())
        self.registry.record("presto", "10.0.0.9", now=now, diag=header)
        return self.registry.clients(now=now)[0]["events"]

    def test_a_device_with_no_failures_has_no_events(self):
        self.assertEqual(self.poll(100, n=0, free=7000), [])

    def test_a_rising_count_is_one_event_with_the_new_failures_and_the_time_it_was_heard(self):
        self.poll(100, n=0)
        events = self.poll(160, n=3, err="TimeoutError", st="conn")
        self.assertEqual(events, [{"at": 160, "count": 3, "err": "TimeoutError", "st": "conn", "first": False}])

    def test_a_steady_count_adds_nothing_and_a_further_rise_adds_the_difference(self):
        self.poll(100, n=2, err="OSError 110", st="conn")
        self.poll(130, n=2)
        events = self.poll(160, n=7, err="TimeoutError", st="head")
        self.assertEqual([(event["count"], event["err"], event["first"]) for event in events],
                         [(2, "OSError 110", True), (5, "TimeoutError", False)])

    def test_a_device_first_seen_with_failures_says_they_came_before_the_daemon_saw_it(self):
        events = self.poll(100, n=18, err="TimeoutError", st="conn")
        self.assertEqual(len(events), 1)
        self.assertEqual((events[0]["count"], events[0]["first"]), (18, True))

    def test_a_restarted_device_starts_counting_again(self):
        self.poll(100, n=5, err="TimeoutError", st="conn")
        self.assertEqual(len(self.poll(130, n=0, up=1)), 1, "a restart is not a failure")
        events = self.poll(160, n=2, err="OSError 113", st="conn")
        self.assertEqual([event["count"] for event in events], [5, 2])

    def test_a_poll_without_a_count_changes_nothing(self):
        self.poll(100, n=1, err="TimeoutError", st="conn")
        self.assertEqual(len(self.poll(130, free=6000)), 1)

    def test_only_the_newest_events_are_kept(self):
        for step in range(1, MAX_EVENTS + 11):
            events = self.poll(100 + step, n=step, err="TimeoutError", st="conn")
        self.assertEqual(len(events), MAX_EVENTS)
        self.assertEqual(events[-1]["count"], 1)
        self.assertEqual(events[0]["at"], 100 + 11)

    def test_the_callers_copy_cannot_change_the_registry_and_no_bookkeeping_leaks(self):
        self.poll(100, n=1, err="TimeoutError", st="conn")
        record = self.registry.clients(now=100)[0]
        record["events"].clear()
        self.assertEqual(len(self.registry.clients(now=100)[0]["events"]), 1)
        self.assertNotIn("failures_seen", record)

    def test_events_go_with_the_client_when_it_is_forgotten(self):
        self.poll(100, n=1, err="TimeoutError", st="conn")
        self.assertEqual(self.registry.clients(now=100 + MAX_AGE_SECONDS + 1), [])


class ClientExpiryTests(unittest.TestCase):
    """A row that nobody refreshes goes away by itself."""

    def test_a_client_is_forgotten_after_a_day_without_a_fetch(self):
        registry = ClientRegistry()
        registry.record("old", "10.0.0.1", now=1000)
        registry.record("recent", "10.0.0.2", now=1000 + 3600)
        later = 1000 + MAX_AGE_SECONDS + 1
        self.assertEqual([client["name"] for client in registry.clients(now=later)], ["recent"])

    def test_one_inside_the_limit_stays_and_one_at_it_stays_too(self):
        registry = ClientRegistry()
        registry.record("edge", "10.0.0.1", now=1000)
        self.assertEqual(len(registry.clients(now=1000 + MAX_AGE_SECONDS)), 1)
        self.assertEqual(len(registry.clients(now=1000 + MAX_AGE_SECONDS + 1)), 0)

    def test_a_fetch_renews_it(self):
        registry = ClientRegistry()
        registry.record("presto", "10.0.0.1", now=1000)
        registry.record("presto", "10.0.0.1", now=1000 + MAX_AGE_SECONDS)
        self.assertEqual(len(registry.clients(now=1000 + MAX_AGE_SECONDS + 5)), 1)

    def test_recording_also_clears_the_old_ones_and_their_problem_memory(self):
        registry = ClientRegistry()
        registry.record("old", "10.0.0.1", now=1000, diag="err=OSError 113;st=conn;n=3")
        registry.record("new", "10.0.0.2", now=1000 + MAX_AGE_SECONDS + 10)
        self.assertEqual(list(registry._records), [("new", "10.0.0.2")])
        self.assertNotIn(("old", "10.0.0.1"), registry._reported)

    def test_the_limit_can_be_changed(self):
        registry = ClientRegistry(max_age=60)
        registry.record("presto", "10.0.0.1", now=1000)
        self.assertEqual(len(registry.clients(now=1060)), 1)
        self.assertEqual(len(registry.clients(now=1061)), 0)


if __name__ == "__main__":
    unittest.main()
