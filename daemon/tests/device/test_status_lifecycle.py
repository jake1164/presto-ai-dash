"""The status response's two optional extensions.

The provider's payload is shared and must never grow one device's state, the
extension describes only the requesting device, and demo mode never creates a
durable record or acknowledges anything.
"""

import json
from pathlib import Path
import sys
import unittest
from urllib.error import HTTPError
from urllib.request import Request, ProxyHandler, build_opener

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from admin.test_management import ManagementBase
from presto_ai_dash import device_protocol as protocol
from presto_ai_dash.api import ApiServer
from presto_ai_dash.device_lifecycle import DeviceLifecycle

LEGACY = "1234567890abcdef" * 4
A = "1" * 32
B = "2" * 32


def report(boot=1, seq=1, revision=LEGACY[:12]):
    return protocol.lifecycle_text({
        "v": 1, "boot": boot, "seq": seq, "scheme": "legacy-v1", "revision": revision,
        "bootstrap": None, "slot": None, "boot_confirmed": True, "caps": [],
        "update": protocol.idle_update()})


class StatusExtensionBase(ManagementBase):
    demo = False

    def setUp(self):
        super().setUp()
        self.shared = {"cards": [], "sources": [], "updated_at": "2026-10-29T09:00:00Z"}
        self.lifecycle = DeviceLifecycle(
            self.storage,
            expected_provider=lambda: {"scheme": "legacy-v1", "revision": LEGACY,
                                       "min_bootstrap": None},
            running_version="0.50.0", channel="pypi")
        self.api = ApiServer("127.0.0.1", 0, self.storage, "device-token",
                             status_provider=self.payload,
                             demo_enabled=(lambda: True) if self.demo else None,
                             device_lifecycle=self.lifecycle)
        self.api.start()
        self.addCleanup(self.api.stop)
        self.base_url = "http://127.0.0.1:%d" % self.api.actual_port
        self.opener = build_opener(ProxyHandler({}))

    def payload(self, lazy=False):
        # A shared dict, exactly as a cached provider would return one.
        self.shared["calls"] = self.shared.get("calls", 0) + 1
        return self.shared

    def fetch(self, device_id=None, lifecycle=None, revision=LEGACY[:12], query="?cards=lazy"):
        headers = {"Authorization": "Bearer device-token", "X-Presto-Client": "presto"}
        if revision is not None:
            headers["X-Presto-Revision"] = revision
        if device_id is not None:
            headers["X-Presto-Device-ID"] = device_id
        if lifecycle is not None:
            headers["X-Presto-Lifecycle"] = lifecycle
        request = Request(self.base_url + "/api/v1/status" + query, headers=headers)
        with self.opener.open(request, timeout=5) as response:
            return json.loads(response.read())


class DeviceExtensionTests(StatusExtensionBase):
    def test_a_known_device_gets_its_own_extension(self):
        body = self.fetch(device_id=A, lifecycle=report())
        extension = body["device_lifecycle"]
        self.assertEqual(extension["device_id"], A)
        self.assertEqual(extension["report_status"], "accepted")
        self.assertEqual(extension["update"]["state"], "current")
        self.assertEqual(body["daemon_release"]["state"], "disabled")

    def test_the_shared_provider_payload_is_never_mutated(self):
        self.fetch(device_id=A, lifecycle=report())
        self.assertEqual(set(self.shared), {"cards", "sources", "updated_at", "calls"})

    def test_two_devices_never_see_each_other(self):
        self.fetch(device_id=A, lifecycle=report(revision="0123456789ab"),
                   revision="0123456789ab")
        first = self.fetch(device_id=A, lifecycle=report(seq=2, revision="0123456789ab"),
                           revision="0123456789ab")
        second = self.fetch(device_id=B, lifecycle=report())
        self.assertEqual(first["device_lifecycle"]["device_id"], A)
        self.assertEqual(second["device_lifecycle"]["device_id"], B)
        self.assertEqual(first["device_lifecycle"]["update"]["state"], "manual_action")
        self.assertEqual(second["device_lifecycle"]["update"]["state"], "current")
        self.assertNotIn(B, json.dumps(first))

    def test_a_legacy_client_still_gets_its_quota_data(self):
        body = self.fetch(device_id=None)
        self.assertEqual(body["sources"], [])
        self.assertEqual(body["device_lifecycle"]["report_status"], "legacy")
        self.assertIsNone(body["device_lifecycle"]["device_id"])

    def test_a_malformed_report_does_not_break_the_refresh(self):
        body = self.fetch(device_id=A, lifecycle="not json")
        self.assertEqual(body["device_lifecycle"]["report_status"], "invalid")
        self.assertEqual(body["sources"], [])
        self.assertEqual(self.storage.device_count(), 0)

    def test_a_full_payload_without_room_drops_the_extensions(self):
        self.shared["padding"] = "x" * (protocol.MAX_STATUS_BYTES - 200)
        body = self.fetch(device_id=A, lifecycle=report())
        self.assertNotIn("device_lifecycle", body)
        self.assertNotIn("daemon_release", body)
        # The report itself is still recorded: a device that cannot be told
        # anything new is still one the admin page should know about. What the
        # plan forbids is issuing a command or an acknowledgement, and there
        # is neither to issue.
        self.assertEqual(self.storage.device_count(), 1)

    def test_the_status_route_still_needs_the_shared_token(self):
        request = Request(self.base_url + "/api/v1/status")
        with self.assertRaises(HTTPError) as caught:
            self.opener.open(request, timeout=5)
        self.assertEqual(caught.exception.code, 401)


class DemoExtensionTests(StatusExtensionBase):
    demo = True

    def test_demo_mode_creates_no_device_record(self):
        body = self.fetch(device_id=A, lifecycle=report())
        self.assertNotIn("device_lifecycle", body)
        self.assertEqual(self.storage.device_count(), 0)


if __name__ == "__main__":
    unittest.main()
