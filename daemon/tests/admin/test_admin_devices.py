"""The device and release routes behind the admin page.

Every route here is authenticated, uncached and read-only except the two
mutation routes, which answer ``409 ota_not_enabled``: there is no updater yet,
and nothing may authorize a transfer, spend a retry or clear a warning.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from admin.test_management import AdminServerBase
from presto_ai_dash import device_protocol as protocol

DEVICE = "0123456789abcdef" * 2
KEY = "f" * 32
LEGACY = "1234567890abcdef" * 4


def headers_for(device_id=DEVICE, revision=LEGACY[:12], seq=1):
    from email.message import Message
    message = Message()
    message["X-Presto-Client"] = "Kitchen Presto"
    message["X-Presto-Revision"] = revision
    message["X-Presto-Device-ID"] = device_id
    message["X-Presto-Lifecycle"] = protocol.lifecycle_text({
        "v": 1, "boot": 1, "seq": seq, "scheme": "legacy-v1", "revision": revision,
        "bootstrap": None, "slot": None, "boot_confirmed": True, "caps": [],
        "update": protocol.idle_update()})
    return message


class DeviceRouteBase(AdminServerBase):
    """An admin server with one durable device that does not match."""

    def setUp(self):
        super().setUp()
        self.manager.lifecycle._expected_provider = lambda: {
            "scheme": "legacy-v1", "revision": LEGACY, "min_bootstrap": None}
        self.manager.lifecycle._expected_cache = None
        self.manager.lifecycle._expected_failed = False
        self.manager.lifecycle.observe(headers_for(revision="0123456789ab"))

    def device_path(self, device_id=DEVICE):
        return "/api/admin/devices/" + device_id

    def mutation(self, path, body=None, key=KEY, **kwargs):
        payload = body if body is not None else {"state_revision": 2,
                                                 "target_scheme": "legacy-v1",
                                                 "target_revision": LEGACY[:12]}
        return self.request(path, "POST", payload, headers={"Idempotency-Key": key},
                            **kwargs)


class DeviceListTests(DeviceRouteBase):
    def test_it_needs_authorization(self):
        for token in (None, "wrong", "device-token"):
            self.assertEqual(self.request("/api/admin/devices", token=token)[0], 401)

    def test_a_browser_session_may_read_it(self):
        cookie, _, _ = self.login()
        status, _, body = self.request("/api/admin/devices", token=None,
                                       headers={"Cookie": cookie})
        self.assertEqual(status, 200)
        self.assertEqual(body["v"], 1)

    def test_it_is_uncached_and_read_only(self):
        status, headers, _ = self.request("/api/admin/devices")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Cache-Control"], "no-store")
        for method in ("POST", "PUT", "DELETE"):
            with self.subTest(method=method):
                self.assertIn(self.request("/api/admin/devices", method, {})[0],
                              (404, 409))

    def test_a_mismatch_is_listed_and_counted(self):
        status, _, body = self.request("/api/admin/devices")
        self.assertEqual(status, 200)
        self.assertEqual(body["needs_attention_count"], 1)
        device = body["devices"][0]
        self.assertEqual(device["device_id"], DEVICE)
        self.assertEqual(device["comparison"], "different")
        self.assertEqual(device["update"]["state"], "manual_action")
        self.assertEqual(device["update"]["reason"], "usb_update_required")
        self.assertTrue(device["update"]["needs_attention"])

    def test_a_device_projection_carries_no_secrets_or_raw_report(self):
        _, _, body = self.request("/api/admin/devices")
        text = json.dumps(body)
        self.assertNotIn("unit-secret-admin-token", text)
        self.assertNotIn("report_json", text)
        self.assertNotIn("shared_token", text)
        device = body["devices"][0]
        self.assertIsNone(device["usb_observation"])
        self.assertFalse(device["update"]["can_retry"])
        self.assertEqual(device["update"]["command_status"], "none")


class DeviceDetailTests(DeviceRouteBase):
    def test_it_needs_authorization(self):
        self.assertEqual(self.request(self.device_path(), token=None)[0], 401)

    def test_one_device_is_returned_with_its_attempts(self):
        status, _, body = self.request(self.device_path())
        self.assertEqual(status, 200)
        self.assertEqual(body["device_id"], DEVICE)
        self.assertEqual(body["attempts"], [])
        self.assertIn("state_revision", body)

    def test_an_unknown_device_is_not_found(self):
        self.assertEqual(self.request(self.device_path("a" * 32))[0], 404)

    def test_an_id_that_is_not_hexadecimal_is_not_found(self):
        self.assertEqual(self.request(self.device_path("abc"))[0], 404)


class RetryRouteTests(DeviceRouteBase):
    def test_it_needs_authorization(self):
        self.assertEqual(self.mutation(self.device_path() + "/retry", token=None)[0], 401)

    def test_a_cookie_mutation_needs_csrf(self):
        cookie, session, _ = self.login()
        path = self.device_path() + "/retry"
        body = {"state_revision": 2}
        headers = {"Cookie": cookie, "Origin": self.base_url, "Idempotency-Key": KEY}
        self.assertEqual(self.request(path, "POST", body, token=None, headers=headers)[0], 403)
        headers["X-CSRF-Token"] = session["csrf_token"]
        status, _, answer = self.request(path, "POST", body, token=None, headers=headers)
        self.assertEqual(status, 409)
        self.assertEqual(answer, {"error": "ota_not_enabled"})

    def test_a_known_device_is_refused_as_not_enabled(self):
        status, _, body = self.mutation(self.device_path() + "/retry")
        self.assertEqual(status, 409)
        self.assertEqual(body, {"error": "ota_not_enabled"})

    def test_an_unknown_device_is_not_found(self):
        self.assertEqual(self.mutation(self.device_path("a" * 32) + "/retry")[0], 404)

    def test_a_malformed_request_is_refused_before_the_feature_check(self):
        path = self.device_path() + "/retry"
        self.assertEqual(self.request(path, "POST", raw=b"[}", headers={
            "Content-Type": "application/json", "Idempotency-Key": KEY})[0], 400)
        self.assertEqual(self.request(path, "POST", raw=b"{}", headers={
            "Content-Type": "text/plain", "Idempotency-Key": KEY})[0], 415)
        self.assertEqual(self.request(path, "POST", raw=b"{}", headers={})[0], 415)
        oversized = json.dumps({"pad": "x" * 600}).encode("utf-8")
        self.assertEqual(self.request(path, "POST", raw=oversized, headers={
            "Content-Type": "application/json", "Idempotency-Key": KEY})[0], 413)

    def test_the_idempotency_key_is_required(self):
        path = self.device_path() + "/retry"
        self.assertEqual(self.request(path, "POST", {"state_revision": 2})[0], 400)
        self.assertEqual(self.request(path, "POST", {"state_revision": 2},
                                      headers={"Idempotency-Key": "short"})[0], 400)

    def test_it_never_changes_the_device_record(self):
        before = self.request(self.device_path())[2]
        self.mutation(self.device_path() + "/retry")
        after = self.request(self.device_path())[2]
        self.assertEqual(before["state_revision"], after["state_revision"])
        self.assertEqual(before["update"], after["update"])


class RecoveryRouteTests(DeviceRouteBase):
    def test_a_known_device_is_refused_as_not_enabled(self):
        status, _, body = self.mutation(self.device_path() + "/confirm-recovery")
        self.assertEqual(status, 409)
        self.assertEqual(body, {"error": "ota_not_enabled"})

    def test_it_needs_authorization(self):
        self.assertEqual(self.mutation(self.device_path() + "/confirm-recovery",
                                       token=None)[0], 401)

    def test_an_unknown_device_is_not_found(self):
        self.assertEqual(self.mutation(self.device_path("a" * 32) + "/confirm-recovery")[0],
                         404)


class ReleaseRouteTests(DeviceRouteBase):
    def test_it_needs_authorization(self):
        self.assertEqual(self.request("/api/admin/releases", token=None)[0], 401)

    def test_without_a_checker_it_says_disabled_and_offers_commands(self):
        status, headers, body = self.request("/api/admin/releases")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(body["state"], "disabled")
        self.assertIsNone(body["latest"])
        self.assertIn("instructions", body)
        self.assertIn("update", body["instructions"])
        self.assertTrue(body["instructions"]["note"])
        self.assertNotIn("unit-secret-admin-token", json.dumps(body))

    def test_the_instructions_match_the_channel(self):
        _, _, body = self.request("/api/admin/releases")
        commands = " ".join(body["instructions"]["update"])
        if body["channel"] == "ghcr":
            self.assertIn("docker compose", commands)
        elif body["channel"] == "pypi":
            self.assertIn("pipx", commands)
        else:
            # A development checkout is never offered a published release.
            self.assertEqual(body["channel"], "development")
            self.assertEqual(commands, "")


class StatusViewTests(DeviceRouteBase):
    def test_the_status_view_carries_the_device_projections(self):
        status, _, body = self.request("/api/admin/status")
        self.assertEqual(status, 200)
        self.assertEqual(body["device_attention_count"], 1)
        self.assertEqual([device["device_id"] for device in body["devices"]], [DEVICE])
        self.assertEqual(body["release"]["state"], "disabled")
        # The in-memory client list is still there and still separate.
        self.assertIn("clients", body)
