"""The Presto's authenticated clock: the daemon proves the time, the device checks it.

The device builds HMAC from hashlib (MicroPython has no hmac module here), so
it is pinned against the stdlib's, and against the daemon's own signing.
"""

import hashlib
import hmac as stdlib_hmac
import json
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from urllib.request import ProxyHandler, Request, build_opener

import presto_path  # noqa: F401
import clockproof
import net
import updater
from presto_ai_dash import clock_proof
from presto_ai_dash.api import ApiServer
from presto_ai_dash.storage import Storage

from device import test_slot_app as slot_app

TOKEN = "shared-token-for-tests"
NONCE = "0123456789abcdef" * 2
NOW = 1800000000
FLOOR = updater.MIN_USABLE_EPOCH


def header_set(**values):
    from email.message import Message
    message = Message()
    for name, value in values.items():
        message[name.replace("_", "-")] = value
    return message


class HmacTests(unittest.TestCase):
    def test_matches_the_standard_library_including_long_keys(self):
        for key in (b"k", b"key" * 10, b"x" * 64, b"y" * 65, b"z" * 200):
            for text in (b"", b"abc", b"presto" * 40):
                with self.subTest(key=len(key), text=len(text)):
                    self.assertEqual(clockproof.hmac_sha256(key, text),
                                     stdlib_hmac.new(key, text, hashlib.sha256).digest())

    def test_rfc_4231_case_two(self):
        self.assertEqual(
            clockproof.hmac_sha256(b"Jefe", b"what do ya want for nothing?").hex(),
            "5bdcc146bf60754e6a042426089575c75a003f089d2739839dec58b964ec3843")

    def test_the_device_verifies_exactly_what_the_daemon_signs(self):
        proof = clock_proof.proof(TOKEN, NONCE, NOW)
        self.assertEqual(clockproof.verify(TOKEN, NONCE, proof, FLOOR), NOW)

    def test_nonces_are_fresh_lowercase_hex(self):
        first, second = clockproof.new_nonce(), clockproof.new_nonce()
        self.assertNotEqual(first, second)
        self.assertTrue(clock_proof.valid_nonce(first))


class VerifyTests(unittest.TestCase):
    def good(self):
        return clock_proof.proof(TOKEN, NONCE, NOW)

    def test_a_proof_for_another_nonce_is_refused(self):
        self.assertIsNone(clockproof.verify(TOKEN, "f" * 32, self.good(), FLOOR))

    def test_a_proof_made_without_the_token_is_refused(self):
        forged = clock_proof.proof("another-token", NONCE, NOW)
        self.assertIsNone(clockproof.verify(TOKEN, NONCE, forged, FLOOR))

    def test_moving_the_time_invalidates_the_proof(self):
        for moved in (NOW - 86400 * 365, NOW + 1, FLOOR - 1):
            with self.subTest(moved=moved):
                self.assertIsNone(clockproof.verify(TOKEN, NONCE, dict(self.good(), t=moved), FLOOR))

    def test_times_outside_the_sane_range_are_refused_even_when_signed(self):
        for epoch in (FLOOR - 1, 0, clockproof.CEILING, clockproof.CEILING + 1):
            with self.subTest(epoch=epoch):
                signed = clock_proof.proof(TOKEN, NONCE, epoch)
                self.assertIsNone(clockproof.verify(TOKEN, NONCE, signed, FLOOR))

    def test_malformed_replies_are_none_never_exceptions(self):
        good = self.good()
        for proof in (None, [], "x", {}, {"t": NOW}, {"mac": good["mac"]},
                      {"t": True, "mac": good["mac"]}, {"t": float(NOW), "mac": good["mac"]},
                      {"t": str(NOW), "mac": good["mac"]}, {"t": NOW, "mac": good["mac"][:-1]},
                      {"t": NOW, "mac": 5}):
            with self.subTest(proof=proof):
                self.assertIsNone(clockproof.verify(TOKEN, NONCE, proof, FLOOR))
        self.assertIsNone(clockproof.verify("", NONCE, good, FLOOR))
        self.assertIsNone(clockproof.verify(TOKEN, "", good, FLOOR))
        self.assertIsNone(clockproof.verify(None, NONCE, good, FLOOR))


class DaemonReplyTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.storage = Storage(os.path.join(folder.name, "clock.sqlite"))
        self.addCleanup(self.storage.close)
        self.api = ApiServer("127.0.0.1", 0, self.storage, TOKEN)
        self.api.start()
        self.addCleanup(self.api.stop)
        self.opener = build_opener(ProxyHandler({}))

    def status(self, extra=None, path="/api/v1/status"):
        headers = {"Authorization": "Bearer " + TOKEN}
        headers.update(extra or {})
        request = Request("http://127.0.0.1:%d%s" % (self.api.actual_port, path), headers=headers)
        with self.opener.open(request, timeout=3) as response:
            return json.load(response)

    def test_a_valid_nonce_is_answered_with_a_proof_the_device_accepts(self):
        payload = self.status({clockproof.NONCE_HEADER: NONCE})
        epoch = clockproof.verify(TOKEN, NONCE, payload["clock"], FLOOR)
        self.assertIsNotNone(epoch)
        self.assertAlmostEqual(epoch, __import__("time").time(), delta=10)

    def test_the_devices_own_client_puts_the_nonce_on_the_wire_and_gets_a_proof(self):
        # The header dictionary is not enough: request_headers() writes only
        # the names it knows, which hid a missing header from the first tests.
        import asyncio
        client = net.StatusClient("127.0.0.1", self.api.actual_port, TOKEN, timeout=3,
                                  path="/api/v1/status")
        nonce = clockproof.new_nonce()
        client.time_nonce_provider = lambda: nonce
        self.assertIn("X-Presto-Time-Nonce: " + nonce,
                      net.request_headers(client.headers_for("/api/v1/status")))
        payload = asyncio.run(client.fetch_async())
        self.assertIsNotNone(clockproof.verify(TOKEN, nonce, payload.get("clock"), FLOOR))
        # And no nonce, no header: a request from a device with a real clock is unchanged.
        client.time_nonce_provider = lambda: None
        self.assertNotIn("Time-Nonce", net.request_headers(client.headers_for("/api/v1/status")))
        self.assertNotIn("clock", asyncio.run(client.fetch_async()))

    def test_no_nonce_means_no_clock_and_the_rest_is_unchanged(self):
        plain = self.status()
        self.assertNotIn("clock", plain)
        with_clock = self.status({clockproof.NONCE_HEADER: NONCE})
        with_clock.pop("clock")
        self.assertEqual(set(with_clock), set(plain))

    def test_a_bad_nonce_is_ignored(self):
        for bad in ("short", "G" * 32, "A" * 32, NONCE + "0", " "):
            with self.subTest(nonce=bad):
                self.assertNotIn("clock", self.status({clockproof.NONCE_HEADER: bad}))

    def test_two_nonces_are_ambiguous_and_get_nothing(self):
        from http.client import HTTPConnection
        connection = HTTPConnection("127.0.0.1", self.api.actual_port, timeout=3)
        self.addCleanup(connection.close)
        connection.putrequest("GET", "/api/v1/status")
        connection.putheader("Authorization", "Bearer " + TOKEN)
        connection.putheader(clockproof.NONCE_HEADER, NONCE)
        connection.putheader(clockproof.NONCE_HEADER, "f" * 32)
        connection.endheaders()
        self.assertNotIn("clock", json.load(connection.getresponse()))

    def test_an_unauthorised_request_gets_no_time(self):
        from urllib.error import HTTPError
        request = Request("http://127.0.0.1:%d/api/v1/status" % self.api.actual_port,
                          headers={clockproof.NONCE_HEADER: NONCE})
        with self.assertRaises(HTTPError) as caught:
            self.opener.open(request, timeout=3)
        self.assertEqual(caught.exception.code, 401)
        self.assertNotIn(b"clock", caught.exception.read())

    def test_a_payload_with_no_room_is_left_alone(self):
        payload = {"cards": []}
        headers = header_set(**{"X_Presto_Time_Nonce": NONCE})
        self.assertIs(clock_proof.attach(payload, headers, TOKEN, 10), payload)
        self.assertIn("clock", clock_proof.attach(payload, headers, TOKEN, 10000))


class AppClockTests(slot_app.SlotAppBase):
    """The running app: ask only while the clock is unusable; accept only a proof."""

    def app(self):
        journal = slot_app.activation.blank_journal()
        app = self.make_app(journal, self.slot_context(journal, boot_confirmed=True))
        app.client.token = TOKEN
        return app

    def unusable(self):
        return patch.object(self.module.time, "time", return_value=1609459230)

    def test_the_status_request_carries_a_nonce_only_while_the_clock_is_unusable(self):
        app = self.app()
        with self.unusable():
            headers = app.client.headers_for(net.STATUS_PATH)
            self.assertEqual(headers["X-Presto-Time-Nonce"], app._time_nonce)
            self.assertNotIn("X-Presto-Time-Nonce", app.client.headers_for("/api/v1/cards/x"))
        with patch.object(self.module.time, "time", return_value=NOW):
            self.assertNotIn("X-Presto-Time-Nonce", app.client.headers_for(net.STATUS_PATH))
            self.assertIsNone(app._time_nonce)

    def test_a_valid_proof_sets_the_clock_once_and_builds_the_updater(self):
        app = self.app()
        self.assertIsNone(app.ota)
        with self.unusable():
            nonce = app._clock_nonce()
            with patch.object(self.module, "_set_clock") as set_clock, \
                    patch("ota_transport.configure") as configure:
                app._apply_clock({"clock": clock_proof.proof(TOKEN, nonce, NOW)})
                set_clock.assert_called_once_with(NOW)
                configure.assert_called_once_with(app)
                # The nonce is spent: the same reply cannot be applied twice.
                app._apply_clock({"clock": clock_proof.proof(TOKEN, nonce, NOW)})
                set_clock.assert_called_once()

    def test_forged_stale_and_unasked_replies_change_nothing(self):
        app = self.app()
        with self.unusable(), patch.object(self.module, "_set_clock") as set_clock:
            nonce = app._clock_nonce()
            app._apply_clock({"clock": clock_proof.proof("another-token", nonce, NOW)})
            app._apply_clock({"clock": clock_proof.proof(TOKEN, nonce, NOW)})  # nonce already spent
            app._apply_clock({"clock": clock_proof.proof(TOKEN, "f" * 32, NOW)})
            app._apply_clock({})
            app._apply_clock(None)
            set_clock.assert_not_called()

    def test_a_real_clock_is_never_moved(self):
        app = self.app()
        app._time_nonce = NONCE
        with patch.object(self.module.time, "time", return_value=NOW), \
                patch.object(self.module, "_set_clock") as set_clock:
            app._apply_clock({"clock": clock_proof.proof(TOKEN, NONCE, NOW - 86400 * 400)})
            set_clock.assert_not_called()

    def test_the_whole_poll_path_uses_it_before_the_update_offer(self):
        from payloads import sample_payload
        app = self.app()
        order = []
        with self.unusable():
            nonce = app._clock_nonce()
            payload = sample_payload()
            payload["clock"] = clock_proof.proof(TOKEN, nonce, NOW)

            async def fetch():
                return payload

            app.client.fetch_async = fetch
            with patch.object(self.module, "_set_clock", side_effect=lambda e: order.append("clock")), \
                    patch.object(app, "_set_payload", side_effect=lambda p: order.append("payload")):
                import asyncio
                self.assertTrue(asyncio.run(app.poll_once()))
        self.assertEqual(order, ["clock", "payload"])


if __name__ == "__main__":
    unittest.main()
