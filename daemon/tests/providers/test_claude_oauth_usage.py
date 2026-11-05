"""The experimental OAuth usage reader: when it may ask, and what happens after.

Nothing here contacts Anthropic. Every request goes to a stand-in opener, and a
guard fails any test that opens a socket to anything but loopback. Time is
passed in, so "five minutes later" costs nothing.
"""

import ast
from datetime import datetime, timedelta, timezone
from email.message import Message
from http.server import BaseHTTPRequestHandler, HTTPServer
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from presto_ai_dash.collectors import claude_oauth_usage as oauth
from presto_ai_dash.collectors import presto_collector as collector

SECRET = "sk-ant-oat01-SENTINEL-ACCESS-TOKEN"
REFRESH = "sk-ant-ort01-SENTINEL-REFRESH-TOKEN"
NOW = 1_800_000_000.0
BODY = {"five_hour": {"utilization": 34.0, "resets_at": "2027-01-15T10:00:00.476+00:00"},
        "seven_day": {"utilization": 53.0, "resets_at": "2027-01-18T10:00:00+00:00"},
        "seven_day_opus": None, "extra_usage": {"is_enabled": False}}


def block_real_network(test):
    """Fail the test at once if anything but loopback is contacted."""
    real = socket.socket.connect

    def guarded(self, address):
        if isinstance(address, tuple) and address[0] not in ("127.0.0.1", "::1", "localhost"):
            raise AssertionError("a test tried to reach %r" % (address,))
        return real(self, address)

    patcher = patch("socket.socket.connect", guarded)
    patcher.start()
    test.addCleanup(patcher.stop)


class Reply:
    status = 200

    def __init__(self, body=BODY):
        self.body = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")

    def read(self, limit=-1):
        return self.body if limit < 0 else self.body[:limit]

    def __enter__(self):
        return self

    def __exit__(self, *details):
        return False


def http_error(code, retry_after=None):
    headers = Message()
    if retry_after is not None:
        headers["Retry-After"] = retry_after
    return urllib.error.HTTPError(oauth.ENDPOINT, code, "refused", headers, io.BytesIO(b"{}"))


class Opener:
    """Stands in for the network: answers in turn and keeps every request."""

    def __init__(self, *replies):
        self.replies = list(replies) or [Reply()]
        self.requests = []
        self.lock = threading.Lock()

    def open(self, request, timeout=None):
        with self.lock:
            self.requests.append(request)
            reply = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        if isinstance(reply, BaseException):
            raise reply
        return reply


def active(seconds_ago=30, now=NOW):
    return {"available": True, "last_request_at":
            datetime.fromtimestamp(now - seconds_ago, timezone.utc).isoformat()}


class Base(unittest.TestCase):
    def setUp(self):
        block_real_network(self)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        # A space, as a Windows profile often has.
        root = Path(self.temp.name) / "My Profile"
        self.profile = root / ".claude"
        self.profile.mkdir(parents=True)
        self.settings = str(self.profile / "settings.json")
        self.state_dir = str(root / "state")
        os.mkdir(self.state_dir)
        self.credentials = self.profile / ".credentials.json"
        self.write_credentials()
        self.opener = Opener()
        (self.profile / ".claude.json").write_text(json.dumps({
            "cachedUsageUtilization": {"accountUuid": "fixture-account"}}), encoding="utf-8")
        self.assertTrue(self.poller().attest(NOW))

    def write_credentials(self, expires=(NOW + 30 * 86400) * 1000, token=SECRET):
        self.credentials.write_text(json.dumps({"claudeAiOauth": {
            "accessToken": token, "refreshToken": REFRESH, "expiresAt": expires,
            "scopes": ["user:inference"], "subscriptionType": "pro"}}), encoding="utf-8")

    def poller(self, opener=None, **changes):
        settings = {"version": "2", "opener": opener or self.opener, "environ": {},
                    "destination": lambda: "fixture-destination"}
        settings.update(changes)
        return oauth.OAuthPoller(self.settings, self.state_dir, **settings)

    def poll(self, poller=None, now=NOW, seconds_ago=30, ready=True, export_age=None, **changes):
        return (poller or self.poller()).poll(now, active(seconds_ago, now), ready, export_age,
                                               **changes)

    def state(self):
        return oauth.StateFile(oauth.state_path(self.state_dir, self.settings)).load()

    def state_text(self):
        return Path(oauth.state_path(self.state_dir, self.settings)).read_text(encoding="utf-8")

    def count(self):
        return len(self.opener.requests)


class ActivityTests(Base):
    def test_no_activity_means_no_request(self):
        poller = self.poller()
        for summary in (None, {"available": False}, {"available": True, "last_request_at": None}):
            result = poller.poll(NOW, summary, True, None)
            self.assertEqual(result["reason"], "no_activity")
        self.assertEqual(self.count(), 0)

    def test_new_activity_makes_a_request_with_only_the_needed_headers(self):
        result = self.poll()
        self.assertTrue(result["attempted"])
        self.assertEqual(self.count(), 1)
        request = self.opener.requests[0]
        self.assertEqual(request.full_url, "https://api.anthropic.com/api/oauth/usage")
        self.assertEqual(request.get_method(), "GET")
        self.assertIsNone(request.data)
        self.assertEqual(request.get_header("Authorization"), "Bearer " + SECRET)
        self.assertEqual(request.get_header("Anthropic-beta"), "oauth-2025-04-20")
        # An honest name: this must never pass for Claude Code.
        agent = request.get_header("User-agent")
        self.assertTrue(agent.startswith("presto-ai-dash-collector/2"), agent)
        self.assertNotIn("claude", agent.lower())

    def test_old_activity_is_idle(self):
        result = self.poll(seconds_ago=oauth.ACTIVE_SECONDS + 1)
        self.assertEqual(result["reason"], "idle")
        self.assertEqual(self.count(), 0)

    def test_repeated_activity_inside_the_interval_makes_no_second_request(self):
        poller = self.poller()
        self.poll(poller)
        for elapsed in (60, 120, 180, 240, 299):
            result = self.poll(poller, now=NOW + elapsed, seconds_ago=1)
            self.assertEqual(result["reason"], "cooldown")
        self.assertEqual(self.count(), 1)

    def test_continuing_activity_gets_one_request_every_five_minutes(self):
        poller = self.poller()
        for minute in range(0, 31):  # a 60 s collector, Claude busy throughout
            self.poll(poller, now=NOW + minute * 60, seconds_ago=5)
        self.assertEqual(self.count(), 7)  # 0, 5, 10, 15, 20, 25, 30

    def test_the_collector_looking_again_is_not_activity(self):
        # The same log entry, seen round after round, with the cooldown long past.
        poller = self.poller()
        moment = active(10)
        poller.poll(NOW, moment, True, None)
        for minute in range(1, 40):
            result = poller.poll(NOW + minute * 60, moment, True, None)
            self.assertFalse(result["attempted"])
        self.assertEqual(self.count(), 1)

    def test_activity_no_newer_than_the_last_request_never_triggers_another(self):
        # The one moment the idle rule does not cover: exactly at the end of the cooldown.
        poller = self.poller()
        self.poll(poller, seconds_ago=0)
        result = poller.poll(NOW + oauth.ACTIVE_SECONDS, active(0), True, None)
        self.assertEqual(result["reason"], "no_new_activity")
        self.assertEqual(self.count(), 1)

    def test_going_idle_stops_requests_after_at_most_one_final_reading(self):
        poller = self.poller()
        self.poll(poller)
        last_activity = NOW + 100  # the turn ended after that request
        reasons = []
        for minute in range(5, 120, 1):
            now = NOW + minute * 60
            reasons.append(poller.poll(now, active(now - last_activity, now), True, None)["reason"])
        # One reading picks up the end of the turn, then silence.
        self.assertEqual(self.count(), 2)
        self.assertEqual(reasons[-1], "idle")

    def test_resuming_after_idle_refreshes_at_once_when_the_cooldown_has_passed(self):
        poller = self.poller()
        self.poll(poller)
        self.assertEqual(self.poll(poller, now=NOW + 3600, seconds_ago=999)["reason"], "idle")
        self.assertEqual(self.count(), 1)
        self.assertTrue(self.poll(poller, now=NOW + 3660, seconds_ago=2)["attempted"])
        self.assertEqual(self.count(), 2)

    def test_resuming_inside_the_cooldown_waits_for_it(self):
        poller = self.poller()
        self.poll(poller)
        self.assertEqual(self.poll(poller, now=NOW + 200, seconds_ago=2)["reason"], "cooldown")
        self.assertTrue(self.poll(poller, now=NOW + 301, seconds_ago=1)["attempted"])

    def test_activity_from_the_future_is_not_believed(self):
        result = self.poll(seconds_ago=-3600)
        self.assertEqual(result["reason"], "no_activity")
        self.assertEqual(self.count(), 0)

    def test_a_small_clock_skew_still_counts(self):
        self.assertTrue(self.poll(seconds_ago=-5)["attempted"])


class ConfigurationGateTests(Base):
    def test_the_interval_can_never_go_below_five_minutes(self):
        poller = self.poller(interval=1)
        self.assertEqual(poller.interval, 300)
        self.poll(poller)
        self.assertEqual(self.poll(poller, now=NOW + 60, seconds_ago=1)["reason"], "cooldown")

    def test_the_decision_itself_keeps_the_floor(self):
        state = dict(oauth.new_state(), last_attempt_at=NOW - 60)
        result = oauth.evaluate(state, NOW, NOW - 1, interval=1, environ={})
        self.assertEqual(result["reason"], "cooldown")

    def test_a_longer_interval_is_honoured(self):
        poller = self.poller(interval=1800)
        self.poll(poller)
        self.assertEqual(self.poll(poller, now=NOW + 400, seconds_ago=1)["reason"], "cooldown")
        self.assertTrue(self.poll(poller, now=NOW + 1801, seconds_ago=1)["attempted"])

    def test_a_fresh_status_line_is_preferred(self):
        self.assertEqual(self.poll(export_age=120)["reason"], "status_line_fresh")
        self.assertEqual(self.count(), 0)

    def test_an_old_status_line_does_not_stop_a_request(self):
        self.assertTrue(self.poll(export_age=400)["attempted"])

    def test_a_daemon_that_cannot_store_the_reading_costs_no_request(self):
        self.assertEqual(self.poll(ready=False)["reason"], "daemon_not_ready")
        self.assertEqual(self.count(), 0)

    def test_an_account_change_costs_no_request(self):
        self.assertEqual(self.poll(account_changed=True)["reason"], "account_changed")
        self.assertEqual(self.count(), 0)

    def test_another_route_to_claude_costs_no_request(self):
        for name in oauth.OTHER_ROUTES:
            result = self.poll(self.poller(environ={name: "x"}))
            self.assertEqual(result["reason"], "other_route")
            self.assertNotIn("x ", result["detail"].replace(name, ""))
        self.assertEqual(self.count(), 0)

    def test_no_private_folder_means_no_request(self):
        poller = oauth.OAuthPoller(self.settings, None, opener=self.opener, environ={})
        self.assertEqual(self.poll(poller)["reason"], "no_state_directory")
        self.assertEqual(self.count(), 0)


class SharedBudgetTests(Base):
    def test_many_sessions_at_once_make_one_request(self):
        barrier = threading.Barrier(12)
        results = []

        def session():
            poller = self.poller()
            barrier.wait()
            results.append(self.poll(poller))

        threads = [threading.Thread(target=session) for _ in range(12)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(self.count(), 1)
        self.assertEqual(sum(1 for result in results if result["attempted"]), 1)

    def test_real_processes_share_one_budget(self):
        # Separate interpreters: only the file system joins them.
        script = (
            "import sys; sys.path.insert(0, sys.argv[1])\n"
            "from presto_ai_dash.collectors import claude_oauth_usage as o\n"
            "class R:\n"
            "    status = 200\n"
            "    def read(self, n): return b'{\"five_hour\": {\"utilization\": 1.0, "
            "\"resets_at\": null}}'\n"
            "    def __enter__(self): return self\n"
            "    def __exit__(self, *a): return False\n"
            "class O:\n"
            "    def open(self, request, timeout=None): return R()\n"
            "now = float(sys.argv[4])\n"
            "s = {'available': True, 'last_request_at': o._iso(now - 5)}\n"
            "p = o.OAuthPoller(sys.argv[2], sys.argv[3], opener=O(), environ={}, "
            "destination=lambda: 'fixture-destination')\n"
            "print(p.poll(now, s, True, None)['attempted'])\n")
        source = str(Path(oauth.__file__).resolve().parents[2])
        now = str(datetime.now(timezone.utc).timestamp())
        self.write_credentials(expires=(float(now) + 3600) * 1000)
        children = [subprocess.Popen([sys.executable, "-c", script, source, self.settings,
                                      self.state_dir, now], stdout=subprocess.PIPE, text=True)
                    for _ in range(6)]
        answers = [child.communicate(timeout=60)[0].strip() for child in children]
        self.assertEqual(sorted(answers), ["False"] * 5 + ["True"], answers)
        self.assertEqual(len(self.state()["history"]), 1)

    def test_two_entries_for_one_profile_share_a_budget(self):
        self.poll(self.poller())
        self.assertEqual(self.poll(self.poller(), now=NOW + 10, seconds_ago=1)["reason"], "cooldown")
        self.assertEqual(self.count(), 1)

    def test_different_profiles_do_not_share_one(self):
        other = oauth.account_key(str(Path(self.temp.name) / "other" / "settings.json"))
        self.assertNotEqual(other, oauth.account_key(self.settings))

    def test_a_restart_keeps_the_cooldown(self):
        self.poll(self.poller())
        self.assertEqual(self.poll(self.poller(), now=NOW + 60, seconds_ago=1)["reason"], "cooldown")
        self.assertEqual(self.count(), 1)

    def test_a_restart_keeps_a_backoff(self):
        self.opener = Opener(http_error(429))
        self.poll(self.poller())
        self.assertEqual(self.poll(self.poller(), now=NOW + 600, seconds_ago=1)["reason"], "backoff")
        self.assertEqual(self.count(), 1)

    def test_a_restart_keeps_a_stop(self):
        self.opener = Opener(http_error(401))
        self.poll(self.poller())
        self.assertEqual(self.poll(self.poller(), now=NOW + 86400, seconds_ago=1)["reason"], "stopped")
        self.assertEqual(self.count(), 1)

    def test_the_turn_is_spent_before_the_request_goes_out(self):
        # A crash in mid-request must not repeat it after the restart.
        seen = []

        class Watching(Opener):
            def open(inner, request, timeout=None):
                seen.append(self.state()["last_attempt_at"])
                return Opener.open(inner, request, timeout)

        self.poll(self.poller(Watching()))
        self.assertEqual(seen, [NOW])

    def test_a_lock_left_by_a_dead_process_is_cleared(self):
        lock = oauth.state_path(self.state_dir, self.settings) + ".lock"
        Path(lock).write_text("", encoding="utf-8")
        old = datetime.now(timezone.utc).timestamp() - oauth.LOCK_STALE_SECONDS - 5
        os.utime(lock, (old, old))
        self.assertTrue(self.poll()["attempted"])
        self.assertTrue(os.path.exists(lock))  # persistent inode, released kernel lock

    def test_a_lock_held_now_refuses_the_turn(self):
        owner = oauth.StateFile(oauth.state_path(self.state_dir, self.settings))
        held = owner._acquire()
        self.assertIsNotNone(held)
        try:
            with patch.object(oauth, "LOCK_WAIT_SECONDS", 0.05):
                self.assertEqual(self.poll()["reason"], "claim_refused")
        finally:
            held.close()
        self.assertEqual(self.count(), 0)


class StateFailsClosedTests(Base):
    def test_a_damaged_state_file_means_no_request(self):
        for text in ("{not json", "[]", '{"version": 99}', '{"version": 1, "last_attempt_at": "x"}',
                     '{"version": 1, "backoff_until": NaN}'):
            Path(oauth.state_path(self.state_dir, self.settings)).write_text(text, encoding="utf-8")
            self.assertEqual(self.poll()["reason"], "state_unreadable", text)
        self.assertEqual(self.count(), 0)

    def test_reenable_starts_over_from_a_damaged_file(self):
        path = Path(oauth.state_path(self.state_dir, self.settings))
        path.write_text("{not json", encoding="utf-8")
        self.assertTrue(self.poller().reenable())
        self.assertTrue(self.poll()["attempted"])

    def test_a_state_that_cannot_be_written_means_no_request(self):
        with patch.object(oauth.StateFile, "_save", side_effect=OSError("full")):
            self.assertEqual(self.poll()["reason"], "identity_unverified")
        self.assertEqual(self.count(), 0)

    def test_a_refusal_that_cannot_be_saved_still_stops_this_process(self):
        self.opener = Opener(http_error(401))
        poller = self.poller()
        real = oauth.StateFile.update

        def lose_the_outcome(file, change, repair=False):
            return None if any(line for line in [change.__qualname__] if "_request" in line) \
                else real(file, change, repair)

        with patch.object(oauth.StateFile, "update", lose_the_outcome):
            self.poll(poller)
        self.assertEqual(self.poll(poller, now=NOW + 900, seconds_ago=1)["reason"], "stopped")
        self.assertEqual(self.count(), 1)

    @unittest.skipIf(os.name == "nt" and not hasattr(os, "symlink"), "no symlinks")
    def test_a_planted_link_is_not_followed(self):
        path = oauth.state_path(self.state_dir, self.settings)
        try:
            os.symlink(str(self.credentials), path)
        except OSError:
            self.skipTest("this account cannot make symlinks")
        self.assertEqual(self.poll()["reason"], "state_unreadable")
        self.assertEqual(self.count(), 0)


class ResponseTests(Base):
    def test_200_gives_the_windows_and_the_moment_they_were_fetched(self):
        result = self.poll()
        reading = result["reading"]
        self.assertEqual(reading["fetched_at"], oauth._iso(NOW))
        self.assertEqual(set(reading), {"fetched_at", "windows"})
        five, seven = reading["windows"]
        self.assertEqual((five["id"], five["used_percent"], five["label"]),
                         ("five_hour", 34.0, "5h"))
        self.assertEqual(five["resets_at"], "2027-01-15T10:00:00.476000+00:00")
        self.assertEqual((seven["id"], seven["used_percent"], seven["period_minutes"]),
                         ("seven_day", 53.0, 10080))
        state = self.state()
        self.assertEqual((state["last_status"], state["last_outcome"]), (200, "ok"))
        self.assertEqual(state["last_success_at"], NOW)

    def test_one_missing_window_is_reported_as_unavailable(self):
        self.opener = Opener(Reply({"five_hour": BODY["five_hour"], "seven_day": None}))
        windows = self.poll()["reading"]["windows"]
        self.assertEqual([window["state"] for window in windows], ["available", "unavailable"])

    def test_nothing_but_the_two_windows_leaves_the_response(self):
        body = dict(BODY, extra_usage={"secret_field": "SENTINEL-EXTRA"},
                    iguana_necktie={"limit_dollars": 100, "used_dollars": 1})
        self.opener = Opener(Reply(body))
        self.assertNotIn("SENTINEL-EXTRA", json.dumps(self.poll()["reading"]))

    def test_401_and_403_stop_until_reenabled(self):
        for code, outcome in ((401, "unauthorized"), (403, "forbidden")):
            self.setUp()
            self.opener = Opener(http_error(code), Reply())
            poller = self.poller()
            result = self.poll(poller)
            self.assertIsNone(result["reading"])
            self.assertEqual((self.state()["last_status"], self.state()["last_outcome"]),
                             (code, outcome))
            self.assertEqual(self.state()["stopped"], str(code))
            for day in range(1, 6):
                now = NOW + day * 86400
                self.assertEqual(self.poll(poller, now=now, seconds_ago=1)["reason"], "stopped")
            self.assertEqual(self.count(), 1)
            self.assertTrue(self.poller().reenable())
            # A running poller sees the lift: the stop lives in the file.
            self.assertTrue(self.poll(poller, now=NOW + 7 * 86400, seconds_ago=1)["attempted"])
            self.assertEqual(self.count(), 2)

    def test_429_uses_the_extended_backoff_and_not_the_interval(self):
        self.opener = Opener(http_error(429), Reply())
        poller = self.poller()
        self.poll(poller)
        self.assertEqual(self.state()["backoff_until"], NOW + 1800)
        for elapsed in (300, 600, 1200, 1799):
            self.assertEqual(self.poll(poller, now=NOW + elapsed, seconds_ago=1)["reason"], "backoff")
        self.assertEqual(self.count(), 1)
        self.assertTrue(self.poll(poller, now=NOW + 1800, seconds_ago=1)["attempted"])

    def test_retry_after_wins_when_it_is_longer(self):
        self.opener = Opener(http_error(429, "7200"))
        self.poll()
        self.assertEqual(self.state()["backoff_until"], NOW + 7200)

    def test_the_backoff_stands_when_retry_after_is_shorter(self):
        self.opener = Opener(http_error(429, "60"))
        self.poll()
        self.assertEqual(self.state()["backoff_until"], NOW + 1800)

    def test_retry_after_as_a_date_is_understood(self):
        moment = datetime.fromtimestamp(NOW + 5400, timezone.utc)
        self.opener = Opener(http_error(429, moment.strftime("%a, %d %b %Y %H:%M:%S GMT")))
        self.poll()
        self.assertEqual(self.state()["backoff_until"], NOW + 5400)

    def test_nonsense_retry_after_falls_back_to_the_backoff(self):
        self.assertIsNone(oauth.retry_after_seconds("soon", NOW))
        self.assertIsNone(oauth.retry_after_seconds(None, NOW))
        self.assertEqual(oauth.retry_after_seconds("9" * 30, NOW), oauth.MAX_RETRY_AFTER_SECONDS)

    def test_429s_in_a_row_wait_longer_up_to_a_limit(self):
        self.opener = Opener(http_error(429))
        poller = self.poller()
        now, waits = NOW, []
        for _ in range(6):
            self.poll(poller, now=now, seconds_ago=1)
            waits.append(self.state()["backoff_until"] - now)
            now = self.state()["backoff_until"] + 1
        self.assertEqual(waits, [1800, 3600, 7200, 14400, 21600, 21600])

    def test_a_success_clears_the_backoff_counters(self):
        self.opener = Opener(http_error(429), Reply())
        poller = self.poller()
        self.poll(poller)
        self.poll(poller, now=NOW + 1800, seconds_ago=1)
        state = self.state()
        self.assertEqual((state["rate_limited"], state["failures"], state["backoff_until"]), (0, 0, 0))

    def test_a_network_failure_backs_off_and_gives_no_reading(self):
        failures = (urllib.error.URLError("dns"), socket.timeout("slow"), ConnectionResetError())
        for failure in failures:
            self.setUp()
            self.opener = Opener(failure)
            result = self.poll()
            self.assertIsNone(result["reading"])
            state = self.state()
            self.assertEqual((state["last_status"], state["last_outcome"]), (None, "network_error"))
            self.assertEqual(state["backoff_until"], NOW + 900)
            self.assertIsNone(state["stopped"])

    def test_a_server_error_backs_off_and_honours_retry_after(self):
        self.opener = Opener(http_error(503, "3600"))
        self.poll()
        self.assertEqual(self.state()["backoff_until"], NOW + 3600)
        self.assertEqual(self.state()["last_status"], 503)

    def test_a_response_that_cannot_be_read_gives_no_reading(self):
        for body in (b"<html>", b"[]", b'{"five_hour": null, "seven_day": null}',
                     b'{"five_hour": {"utilization": NaN}}', b"x" * (oauth.MAX_RESPONSE_BYTES + 5)):
            self.setUp()
            self.opener = Opener(Reply(body))
            self.assertIsNone(self.poll()["reading"], body[:30])
            self.assertEqual(self.state()["last_outcome"], "format_changed")
            self.assertEqual(self.state()["backoff_until"], NOW + 900)

    def test_a_redirect_answer_is_a_failure(self):
        self.opener = Opener(http_error(302))
        self.assertIsNone(self.poll()["reading"])
        self.assertEqual(self.state()["last_outcome"], "redirect_refused")

    def test_the_history_stays_short(self):
        state = oauth.new_state()
        for index in range(200):
            oauth.apply_outcome(state, 200, "ok", NOW + index)
        self.assertEqual(len(state["history"]), oauth.MAX_HISTORY)


class CredentialTests(Base):
    def test_a_token_near_expiry_is_never_sent_and_costs_no_turn(self):
        self.write_credentials(expires=(NOW + 299) * 1000)
        result = self.poll()
        self.assertEqual(result["reason"], "token_expiring")
        self.assertEqual(self.count(), 0)
        self.assertIsNone(self.state()["last_attempt_at"])

    def test_a_refreshed_token_is_picked_up_with_no_restart(self):
        self.write_credentials(expires=(NOW + 10) * 1000)
        poller = self.poller()
        self.poll(poller)
        self.write_credentials(expires=(NOW + 9 * 3600) * 1000)
        self.assertTrue(self.poll(poller, now=NOW + 60, seconds_ago=1)["attempted"])

    def test_missing_or_unusable_credentials_are_a_quiet_no(self):
        cases = {"credentials_unavailable": ("{not json", None),
                 "credentials_unusable": ('{"claudeAiOauth": {"accessToken": 5}}',
                                          '{"other": 1}')}
        for reason, texts in cases.items():
            for text in filter(None, texts):
                self.credentials.write_text(text, encoding="utf-8")
                self.assertEqual(self.poll()["reason"], reason, text)
        self.credentials.unlink()
        self.assertEqual(self.poll()["reason"], "credentials_unavailable")
        self.assertEqual(self.count(), 0)

    def test_a_token_that_could_split_a_header_is_refused(self):
        self.write_credentials(token="abc\r\nX-Evil: 1")
        self.assertEqual(self.poll()["reason"], "credentials_unusable")
        self.assertEqual(self.count(), 0)

    def test_the_credentials_file_is_never_changed(self):
        before = self.credentials.read_bytes()
        moment = self.credentials.stat().st_mtime_ns
        self.poll()
        self.poll(now=NOW + 600, seconds_ago=1)
        self.assertEqual(self.credentials.read_bytes(), before)
        self.assertEqual(self.credentials.stat().st_mtime_ns, moment)

    def test_nothing_ever_refreshes_a_token(self):
        self.poll()
        self.assertEqual({request.full_url for request in self.opener.requests}, {oauth.ENDPOINT})
        self.assertEqual({request.get_method() for request in self.opener.requests}, {"GET"})


class NoExposureTests(Base):
    def assert_clean(self, text):
        for secret in (SECRET, REFRESH, "Bearer", "claudeAiOauth", "accessToken"):
            self.assertNotIn(secret, text)

    def test_no_outcome_leaves_a_token_in_the_results_or_the_state(self):
        outcomes = (Reply(), http_error(401), http_error(403), http_error(429), http_error(500),
                    urllib.error.URLError("failed with Bearer " + SECRET),
                    ValueError("Bearer " + SECRET), Reply(b"<html>" + SECRET.encode()))
        for outcome in outcomes:
            self.setUp()
            self.opener = Opener(outcome)
            poller = self.poller()
            result = self.poll(poller)
            self.assert_clean(repr(result))
            self.assert_clean(self.state_text())
            self.assert_clean("\n".join(oauth.status_lines(self.state(), NOW)))

    def test_the_poller_keeps_no_token(self):
        poller = self.poller()
        self.poll(poller)
        self.assert_clean(repr(vars(poller)))

    def test_a_broken_credentials_file_does_not_leak_through_a_reason(self):
        self.credentials.write_text('{"claudeAiOauth": {"accessToken": "%s", "expiresAt": "x"}}'
                                    % SECRET, encoding="utf-8")
        self.assert_clean(repr(self.poll()))

    def test_the_log_output_holds_no_token(self):
        captured = io.StringIO()
        with self.assertLogs(level="DEBUG") as logs:
            import logging
            logging.getLogger("oauth-test").debug("start")
            self.poll()
            self.opener.replies = [http_error(401)]
            self.poll(now=NOW + 600, seconds_ago=1)
        self.assert_clean("\n".join(logs.output) + captured.getvalue())


class RealOpenerTests(Base):
    """The opener production uses, against a server on this machine."""

    def serve(self):
        seen = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append((self.path, self.headers.get("Authorization")))
                if self.path == "/usage":
                    self.send_response(302)
                    self.send_header("Location", "http://127.0.0.1:%d/stolen"
                                     % self.server.server_address[1])
                else:
                    self.send_response(200)
                self.end_headers()

            def log_message(self, *args):
                pass

        server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return "http://127.0.0.1:%d/usage" % server.server_address[1], seen

    def test_a_redirect_is_never_followed_with_the_token(self):
        url, seen = self.serve()
        status, body, _header, outcome = oauth.fetch(SECRET, "2", url=url)
        self.assertEqual((status, body, outcome), (302, None, "redirect_refused"))
        self.assertEqual([path for path, _ in seen], ["/usage"])

    def test_the_environment_cannot_put_a_proxy_in_the_path(self):
        with patch.dict(os.environ, {"HTTPS_PROXY": "http://127.0.0.1:9",
                                     "HTTP_PROXY": "http://127.0.0.1:9", "NO_PROXY": ""}):
            url, seen = self.serve()
            self.assertEqual(oauth.fetch(SECRET, "2", url=url)[0], 302)
        self.assertEqual(len(seen), 1)

    def test_the_endpoint_is_fixed_and_encrypted(self):
        self.assertTrue(oauth.ENDPOINT.startswith("https://api.anthropic.com/"))


class DryRunTests(Base):
    def test_dry_run_opens_no_credential_and_sends_nothing(self):
        poller = self.poller(dry_run=True)
        with patch.object(oauth, "read_access_token", side_effect=AssertionError("opened")), \
                patch.object(oauth, "fetch", side_effect=AssertionError("sent")):
            result = self.poll(poller)
        self.assertEqual(result["reason"], "dry_run")
        self.assertFalse(result["attempted"])
        self.assertEqual(self.count(), 0)

    def test_dry_run_shows_the_pacing_without_spending_the_real_turn(self):
        poller = self.poller(dry_run=True)
        self.poll(poller)
        self.assertEqual(self.poll(poller, now=NOW + 60, seconds_ago=1)["reason"], "cooldown")
        self.assertEqual(self.poll(poller, now=NOW + 301, seconds_ago=1)["reason"], "dry_run")
        self.assertIsNone(self.state()["last_attempt_at"])
        # Turning the experiment on for real is not held up by the rehearsal.
        self.assertTrue(self.poll(self.poller(), now=NOW + 302, seconds_ago=1)["attempted"])

    def test_preview_writes_nothing(self):
        poller = self.poller()
        with patch.object(oauth, "read_access_token", side_effect=AssertionError("opened")):
            result = poller.preview(NOW, active(10))
        self.assertTrue(result["eligible"])
        self.assertFalse(os.path.exists(oauth.state_path(self.state_dir, self.settings)))

    def test_preview_reports_every_kind_of_no(self):
        self.opener = Opener(http_error(429))
        poller = self.poller()
        self.poll(poller)
        result = poller.preview(NOW + 60, active(1, NOW + 60))
        self.assertEqual(result["reason"], "backoff")
        self.assertEqual(result["next_at"], NOW + 1800)


class SwitchTests(Base):
    def test_the_stop_switch_acts_before_the_next_request_of_a_running_process(self):
        running = self.poller()
        self.assertTrue(self.poller().stop(NOW))
        self.assertEqual(self.poll(running)["reason"], "stopped")
        self.assertEqual(self.count(), 0)

    def test_reenable_lifts_the_stop_but_not_a_backoff(self):
        self.opener = Opener(http_error(429))
        poller = self.poller()
        self.poll(poller)
        poller.stop(NOW + 1)
        poller.reenable()
        self.assertEqual(self.poll(poller, now=NOW + 60, seconds_ago=1)["reason"], "backoff")


class SourceChoiceTests(unittest.TestCase):
    def test_the_newest_wins_and_the_status_line_wins_a_tie(self):
        pick = oauth.selected_source
        self.assertEqual(pick(NOW, None, None, None), "none")
        self.assertEqual(pick(NOW, None, None, NOW - 9), "usage cache")
        self.assertEqual(pick(NOW, None, NOW - 5, NOW - 9), "oauth")
        self.assertEqual(pick(NOW, None, NOW - 9, NOW - 5), "usage cache")
        self.assertEqual(pick(NOW, 2, NOW - 5, NOW - 9), "status line")
        self.assertEqual(pick(NOW, 5, NOW - 5, NOW - 9), "status line")


class HookTests(Base):
    def setUp(self):
        super().setUp()
        self.script = str(Path(collector.__file__).resolve())
        self.environment = {key: value for key, value in os.environ.items()
                            if key.upper() != "PYTHONPATH"}
        self.environment["LOCALAPPDATA"] = str(Path(self.state_dir))
        self.environment["XDG_STATE_HOME"] = str(Path(self.state_dir))

    def hook_folder(self):
        return os.path.join(self.state_dir, "presto-ai-dash")

    def run_hook(self, *arguments, stdin=b""):
        return subprocess.run([sys.executable, "-I", self.script, "claude-hook"] + list(arguments),
                              input=stdin, capture_output=True, env=self.environment, timeout=60)

    def test_a_hook_is_silent_and_succeeds_and_keeps_no_prompt(self):
        prompt = b'{"prompt": "SENTINEL-PROMPT", "session_id": "SENTINEL-SESSION", "cwd": "/x"}'
        result = self.run_hook("--event", "UserPromptSubmit", "--settings-path", self.settings,
                               stdin=prompt)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, b"", b""))
        written = Path(oauth.hook_path(self.hook_folder(), self.settings)).read_text(encoding="utf-8")
        self.assertEqual(set(json.loads(written)), {"at", "event"})
        self.assertNotIn("SENTINEL", written)

    def test_nothing_a_hook_is_given_can_block_claude(self):
        # Exit 2 would stop the prompt, so argparse must never see these.
        for arguments in ((), ("--nonsense",), ("--event",), ("--event", "Other"),
                          ("--event", "Stop", "--settings-path")):
            result = self.run_hook(*arguments)
            self.assertEqual((result.returncode, result.stdout, result.stderr), (0, b"", b""),
                             arguments)

    def test_a_failure_inside_a_hook_stays_inside_it(self):
        with patch.object(collector, "_state_directory", return_value=self.state_dir):
            self.assertEqual(collector.claude_hook(["--event", "Stop", "--settings-path", "\0bad"]), 0)
        with patch.object(collector, "_state_directory", side_effect=RuntimeError("broken")):
            self.assertEqual(collector.main(["claude-hook", "--event", "Stop"]), 0)

    def test_an_unknown_event_writes_nothing(self):
        self.assertFalse(oauth.write_hook_signal(self.state_dir, self.settings, "Other", NOW))
        self.assertIsNone(oauth.read_hook_signal(self.state_dir, self.settings))

    def test_a_hook_alone_counts_as_activity(self):
        oauth.write_hook_signal(self.state_dir, self.settings, "UserPromptSubmit", NOW - 5)
        self.assertTrue(self.poller().poll(NOW, None, True, None)["attempted"])

    def test_a_hook_beats_a_log_that_has_not_caught_up(self):
        oauth.write_hook_signal(self.state_dir, self.settings, "Stop", NOW - 5)
        self.assertTrue(self.poll(seconds_ago=4000)["attempted"])

    def test_an_old_or_future_hook_is_not_activity(self):
        poller = self.poller()
        oauth.write_hook_signal(self.state_dir, self.settings, "Stop", NOW - 4000)
        self.assertEqual(poller.poll(NOW, None, True, None)["reason"], "idle")
        oauth.write_hook_signal(self.state_dir, self.settings, "Stop", NOW + 4000)
        self.assertEqual(poller.poll(NOW, None, True, None)["reason"], "no_activity")

    def test_a_damaged_hook_file_is_ignored(self):
        for text in ("{not json", "[]", '{"at": "soon"}', '{"at": true}'):
            Path(oauth.hook_path(self.state_dir, self.settings)).write_text(text, encoding="utf-8")
            self.assertIsNone(oauth.read_hook_signal(self.state_dir, self.settings), text)

    def test_the_settings_block_never_blocks_and_needs_no_shell(self):
        block = oauth.hook_settings("C:\\Py thon\\python.exe", "C:\\a b\\presto_collector.py",
                                    "C:\\Users\\x\\.claude\\settings.json")
        self.assertEqual(set(block["hooks"]), {"UserPromptSubmit", "Stop"})
        for event, groups in block["hooks"].items():
            handler = groups[0]["hooks"][0]
            self.assertIs(handler["async"], True)
            self.assertEqual(handler["type"], "command")
            self.assertEqual(handler["command"], "C:/Py thon/python.exe")
            self.assertEqual(handler["args"][:4], ["C:/a b/presto_collector.py", "claude-hook",
                                                   "--event", event])


class WindowsTests(Base):
    def test_a_file_held_open_by_another_process_is_retried(self):
        real = os.replace
        calls = []

        def busy_then_free(source, target):
            calls.append(target)
            if len(calls) < 3:
                raise PermissionError(13, "in use")
            return real(source, target)

        with patch.object(oauth.os, "replace", busy_then_free), \
                patch.object(oauth.time, "sleep"):
            self.assertTrue(self.poll()["attempted"])
        self.assertEqual(self.state()["last_attempt_at"], NOW)

    def test_a_file_that_stays_held_refuses_the_turn_and_leaves_no_debris(self):
        with patch.object(oauth.os, "replace", side_effect=PermissionError(13, "in use")), \
                patch.object(oauth.time, "sleep"):
            self.assertEqual(self.poll()["reason"], "identity_unverified")
        self.assertEqual(self.count(), 0)
        self.assertFalse(any(name.endswith(".tmp") for name in os.listdir(self.state_dir)))

    def test_a_platform_without_nofollow_still_works(self):
        with patch.object(oauth.os, "O_NOFOLLOW", 0, create=True):
            self.assertTrue(self.poll()["attempted"])

    def test_case_and_slashes_do_not_split_a_profile_on_windows(self):
        if os.name != "nt":
            self.skipTest("Windows paths only")
        self.assertEqual(oauth.account_key(self.settings),
                         oauth.account_key(self.settings.upper().replace("\\", "/")))


class StaysPortableTests(unittest.TestCase):
    def test_the_reader_parses_as_python_3_8(self):
        text = Path(oauth.__file__).read_text(encoding="utf-8")
        ast.parse(text, feature_version=(3, 8))

    def test_the_reader_imports_only_the_standard_library_and_its_sibling(self):
        tree = ast.parse(Path(oauth.__file__).read_text(encoding="utf-8"))
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                names.add("." * node.level + (node.module or ""))
        self.assertLessEqual(names, {"datetime", "email", "hashlib", "json", "math", "os", "stat",
                                     "time", "urllib", ".claude_usage_cache", "email.utils",
                                     "urllib.error", "urllib.request", "msvcrt", "fcntl", "tempfile"})


if __name__ == "__main__":
    unittest.main()
