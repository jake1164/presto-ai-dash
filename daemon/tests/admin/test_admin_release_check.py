"""The release-check route, and proof that every read path stays off the network.

A counting adapter records every registry call. GET status, GET release and the
device poll must leave it at zero however often they are called; only the
background checker, started by the authenticated POST, may use it.
"""

import json
from pathlib import Path
import sys
import threading

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from admin.test_admin_devices import DEVICE, DeviceRouteBase
from presto_ai_dash import device_protocol as protocol
from presto_ai_dash.api import ApiServer
from presto_ai_dash.release import Installation
from presto_ai_dash.release_checker import ReleaseChecker
from presto_ai_dash.release_policy import Release, parse_python
from presto_ai_dash.release_registry import RegistryError

from urllib.request import ProxyHandler, Request, build_opener


class Counting(object):
    registry = "pypi"

    def __init__(self, labels=("0.50.0", "0.51.0"), error=None):
        self.labels = labels
        self.error = error
        self.calls = 0
        self.lock = threading.Lock()
        self.hold = None

    def package_url(self, label=None):
        return "https://pypi.org/project/presto-ai-dash/" + (label + "/" if label else "")

    def releases(self):
        with self.lock:
            self.calls += 1
        if self.hold is not None:
            self.hold.wait(5)
        if self.error:
            raise self.error
        return [Release(parse_python(label), label) for label in self.labels]


class Mono(object):
    def __init__(self):
        self.now = 5000.0

    def __call__(self):
        return self.now


class ReleaseCheckBase(DeviceRouteBase):
    def setUp(self):
        super().setUp()
        self.adapter = Counting()
        self.mono = Mono()
        self.manager.lifecycle.running_version = "0.50.0"
        self.manager.lifecycle.channel = "pypi"
        self.checker = ReleaseChecker(
            self.storage, Installation("pip", "pypi", "0.50.0"),
            adapter_factory=lambda cancelled: self.adapter, monotonic=self.mono,
            startup_delay=3600)
        self.manager.release_checker = self.checker
        self.manager.lifecycle.release_checks = True
        self.addCleanup(self.checker.stop)

    def post(self, **kwargs):
        return self.request("/api/admin/releases/check", "POST", {}, **kwargs)

    def finish_queued_check(self):
        self.assertTrue(self.checker.run_once())


class CheckRouteTests(ReleaseCheckBase):
    def test_it_needs_authorization(self):
        for token in (None, "wrong", "device-token"):
            self.assertEqual(self.post(token=token)[0], 401)
        self.assertEqual(self.adapter.calls, 0)

    def test_a_bearer_request_queues_a_check_without_running_it_inline(self):
        status, headers, body = self.post()
        self.assertEqual((status, body), (202, {"queued": True, "status": "queued"}))
        self.assertEqual(headers["Cache-Control"], "no-store")
        # The handler only queued it; no registry call was made on this thread.
        self.assertEqual(self.adapter.calls, 0)
        self.finish_queued_check()
        self.assertEqual(self.adapter.calls, 1)

    def test_a_second_request_joins_the_first(self):
        self.assertEqual(self.post()[2]["status"], "queued")
        status, _, body = self.post()
        self.assertEqual((status, body["status"]), (202, "coalesced"))

    def test_a_request_within_a_minute_of_a_start_is_throttled(self):
        self.post()
        self.finish_queued_check()
        status, headers, body = self.post()
        self.assertEqual(status, 429)
        self.assertEqual(body, {"error": "check_throttled", "retry_after": 60})
        self.assertEqual(headers["Retry-After"], "60")
        self.mono.now += 30
        self.assertEqual(self.post()[1]["Retry-After"], "30")
        self.mono.now += 31
        self.assertEqual(self.post()[0], 202)

    def test_a_registry_imposed_delay_is_a_429_with_its_own_wait(self):
        self.adapter.error = RegistryError("rate_limited", 7200)
        self.post()
        self.finish_queued_check()
        self.mono.now += 120          # well past the one-minute cooldown
        status, headers, body = self.post()
        self.assertEqual((status, body["error"]), (429, "rate_limited"))
        self.assertTrue(7000 < int(headers["Retry-After"]) <= 7200)
        self.assertEqual(body["retry_after"], int(headers["Retry-After"]))

    def test_a_browser_session_needs_the_csrf_token_and_a_matching_origin(self):
        cookie, session, _ = self.login()
        path = "/api/admin/releases/check"
        self.assertEqual(self.request(path, "POST", {}, token=None,
                                      headers={"Cookie": cookie})[0], 403)
        self.assertEqual(self.request(path, "POST", {}, token=None, headers={
            "Cookie": cookie, "Origin": "https://hostile.example",
            "X-CSRF-Token": session["csrf_token"]})[0], 403)
        self.assertEqual(self.request(path, "POST", {}, token=None, headers={
            "Cookie": cookie, "Origin": self.base_url, "X-CSRF-Token": "wrong"})[0], 403)
        self.assertEqual(self.adapter.calls, 0)
        status, _, body = self.request(path, "POST", {}, token=None, headers={
            "Cookie": cookie, "Origin": self.base_url,
            "X-CSRF-Token": session["csrf_token"]})
        self.assertEqual((status, body["status"]), (202, "queued"))

    def test_a_hostile_host_is_refused(self):
        self.assertEqual(self.post(headers={"Host": "hostile.example"})[0], 403)
        self.assertEqual(self.post(headers={"Origin": "https://hostile.example"})[0], 403)

    def test_an_oversized_or_chunked_body_is_refused(self):
        self.assertEqual(self.request("/api/admin/releases/check", "POST",
                                      raw=b"x" * 513)[0], 413)
        self.assertEqual(self.adapter.calls, 0)

    def test_an_empty_body_is_fine(self):
        self.assertEqual(self.request("/api/admin/releases/check", "POST")[0], 202)

    def test_it_is_post_only(self):
        for method in ("GET", "PUT", "DELETE"):
            self.assertNotEqual(self.request("/api/admin/releases/check", method)[0], 202)

    def test_a_development_build_answers_conflict(self):
        self.manager.release_checker = ReleaseChecker(
            self.storage, Installation("development", "development", "0.50.0"))
        status, _, body = self.post()
        self.assertEqual((status, body), (409, {"error": "release_checks_disabled"}))

    def test_no_checker_answers_conflict(self):
        self.manager.release_checker = None
        self.assertEqual(self.post()[0], 409)

    def test_the_response_carries_no_registry_or_exception_text(self):
        self.adapter.error = RegistryError("http_error")
        self.post()
        self.finish_queued_check()
        _, _, body = self.request("/api/admin/releases")
        self.assertEqual((body["state"], body["error"]), ("error", "http_error"))
        self.assertNotIn("Traceback", json.dumps(body))


class ReadPathsStayOffTheNetworkTests(ReleaseCheckBase):
    def test_get_release_and_get_status_never_call_the_registry(self):
        for _ in range(5):
            self.assertEqual(self.request("/api/admin/releases")[0], 200)
            self.assertEqual(self.request("/api/admin/status")[0], 200)
            self.assertEqual(self.request("/api/admin/devices")[0], 200)
        self.assertEqual(self.adapter.calls, 0)

    def test_a_cached_result_is_served_with_no_further_traffic(self):
        self.post()
        self.finish_queued_check()
        self.assertEqual(self.adapter.calls, 1)
        for _ in range(5):
            _, _, body = self.request("/api/admin/releases")
            self.assertEqual((body["state"], body["latest"]), ("available", "0.51.0"))
        self.assertEqual(self.adapter.calls, 1)

    def test_the_device_poll_never_calls_the_registry(self):
        api = ApiServer("127.0.0.1", 0, self.storage, "device-token",
                        status_provider=lambda lazy=False: {"cards": [], "sources": []},
                        device_lifecycle=self.manager.lifecycle)
        api.start()
        self.addCleanup(api.stop)
        opener = build_opener(ProxyHandler({}))
        for _ in range(5):
            request = Request("http://127.0.0.1:%d/api/v1/status?cards=lazy" % api.actual_port,
                              headers={"Authorization": "Bearer device-token",
                                       "X-Presto-Client": "presto"})
            with opener.open(request, timeout=5) as response:
                body = json.loads(response.read())
            self.assertEqual(body["daemon_release"]["state"], "unknown")
        self.assertEqual(self.adapter.calls, 0)


class AdminProjectionTests(ReleaseCheckBase):
    def test_before_any_check_the_page_says_so_and_offers_the_check(self):
        _, _, body = self.request("/api/admin/releases")
        self.assertEqual((body["state"], body["can_check"], body["checking"]),
                         ("unknown", True, False))
        self.assertIsNone(body["latest"])

    def test_a_queued_check_is_visible_as_checking(self):
        self.post()
        _, _, body = self.request("/api/admin/releases")
        self.assertTrue(body["checking"])

    def test_an_available_result_has_a_target_specific_instruction_and_official_link(self):
        self.post()
        self.finish_queued_check()
        _, _, body = self.request("/api/admin/releases")
        self.assertEqual(body["source_url"], "https://pypi.org/project/presto-ai-dash/0.51.0/")
        self.assertEqual(body["instructions"]["target"], "0.51.0")
        commands = " ".join(step["text"] for group in body["instructions"]["groups"]
                            for step in group["steps"])
        self.assertIn("pipx", commands)
        self.assertIn("pip", commands)
        self.assertIsNotNone(body["next_check_at"])
        self.assertIsNone(body["running_note"])

    def test_an_ambiguous_wheel_carries_the_note_and_the_named_install(self):
        self.manager.release_checker = ReleaseChecker(
            self.storage, Installation("pip", "pypi", "0.50.0", ambiguous=True))
        self.manager.lifecycle.running_version = "0.50.0"
        _, _, body = self.request("/api/admin/releases")
        self.assertEqual(body["running_note"], "version_ambiguous")
        titles = " ".join(group["title"] for group in body["instructions"]["groups"])
        self.assertIn("Not sure this is the final 0.50.0?", titles)
        self.assertIn('pipx install --force "presto-ai-dash==0.50.0"',
                      body["instructions"]["update"])

    def test_the_status_view_carries_the_same_release_block(self):
        self.post()
        self.finish_queued_check()
        _, _, status = self.request("/api/admin/status")
        _, _, release = self.request("/api/admin/releases")
        self.assertEqual(status["release"]["state"], release["state"])
        self.assertEqual(status["release"]["latest"], release["latest"])


class DeviceStateIsIndependentTests(ReleaseCheckBase):
    def device(self):
        return self.request("/api/admin/devices/" + DEVICE)[2]

    def test_release_checks_and_an_upgrade_leave_device_update_state_alone(self):
        before = self.device()
        self.post()
        self.finish_queued_check()
        self.assertEqual(self.device(), before)
        # The daemon is upgraded: a new running version, a new notice scope.
        self.manager.lifecycle.running_version = "0.51.0"
        _, _, shown = self.request("/api/admin/releases")
        self.assertNotEqual(shown["state"], "available")
        self.assertEqual(self.device(), before)
        self.assertEqual(before["update"]["state"], "manual_action")
        self.assertTrue(before["update"]["needs_attention"])

    def test_the_status_response_carries_both_notices_independently(self):
        self.post()
        self.finish_queued_check()
        api = ApiServer("127.0.0.1", 0, self.storage, "device-token",
                        status_provider=lambda lazy=False: {"cards": [], "sources": []},
                        device_lifecycle=self.manager.lifecycle)
        api.start()
        self.addCleanup(api.stop)
        report = protocol.lifecycle_text({
            "v": 1, "boot": 1, "seq": 9, "scheme": "legacy-v1", "revision": "0123456789ab",
            "bootstrap": None, "slot": None, "boot_confirmed": True, "caps": [],
            "update": protocol.idle_update()})
        request = Request(
            "http://127.0.0.1:%d/api/v1/status?cards=lazy" % api.actual_port,
            headers={"Authorization": "Bearer device-token", "X-Presto-Client": "Kitchen Presto",
                     "X-Presto-Revision": "0123456789ab", "X-Presto-Device-ID": DEVICE,
                     "X-Presto-Lifecycle": report})
        with build_opener(ProxyHandler({})).open(request, timeout=5) as response:
            body = json.loads(response.read())
        self.assertEqual(body["daemon_release"]["state"], "available")
        self.assertEqual(body["daemon_release"]["latest"], "0.51.0")
        self.assertEqual(body["device_lifecycle"]["update"]["state"], "manual_action")
        self.assertLess(len(json.dumps(body["daemon_release"])), 1024)
        self.assertLess(len(json.dumps(body["device_lifecycle"])), 2048)
        self.assertEqual(self.adapter.calls, 1)
