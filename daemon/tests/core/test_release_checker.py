"""The background release checker: schedule, cache, cooldown, restart and shutdown.

The registry is a fake adapter, so no test here contacts the network. The
status projection is checked against the same SQLite cache the checker writes.
"""

from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from presto_ai_dash import release_checker
from presto_ai_dash.device_lifecycle import DeviceLifecycle, release_instructions
from presto_ai_dash.release import Installation
from presto_ai_dash.release_checker import ReleaseChecker, adapter_for, stamp
from presto_ai_dash.release_policy import Release, parse_docker, parse_python
from presto_ai_dash.release_registry import (Cancelled, GhcrAdapter, PypiAdapter,
                                             RegistryError)
from presto_ai_dash.storage import Storage

START = 1793400000.0


class Clock(object):
    def __init__(self, now=START):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class FakeAdapter(object):
    """Stands in for one registry. ``plan`` lists what each call does."""

    registry = "pypi"

    def __init__(self, plan, docker=False):
        self.plan = list(plan)
        self.calls = 0
        self.docker = docker
        self.gate = None

    def package_url(self, label=None):
        base = "https://pypi.org/project/presto-ai-dash/"
        return base + label + "/" if label else base

    def releases(self):
        self.calls += 1
        if self.gate is not None:
            self.gate()
        step = self.plan.pop(0) if len(self.plan) > 1 else self.plan[0]
        if isinstance(step, Exception):
            raise step
        parse = parse_docker if self.docker else parse_python
        return [Release(parse(label), label) for label in step]


def installation(running="0.50.0", channel="pypi", ambiguous=False):
    kind = {"pypi": "pip", "ghcr": "docker"}.get(channel, "development")
    return Installation(kind, channel, running, ambiguous=ambiguous)


class Base(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "dash.db"
        self.storage = Storage(self.path)
        self.addCleanup(self.storage.close)
        self.clock = Clock()
        self.mono = Clock(1000.0)

    def checker(self, adapter, running="0.50.0", channel="pypi", **kwargs):
        kwargs.setdefault("startup_delay", 0)
        found = ReleaseChecker(self.storage, installation(running, channel, kwargs.pop(
            "ambiguous", False)), adapter_factory=lambda cancelled: adapter, clock=self.clock,
            monotonic=self.mono, **kwargs)
        return found

    def lifecycle(self, running="0.50.0", channel="pypi"):
        found = DeviceLifecycle(self.storage, clock=self.clock, running_version=running,
                                channel=channel)
        found.release_checks = True
        return found

    def row(self, channel="pypi"):
        return self.storage.release_cache(channel)


class OutcomeTests(Base):
    def test_a_newer_release_is_available_and_the_times_are_recorded(self):
        adapter = FakeAdapter([["0.50.0", "0.51.0"]])
        self.checker(adapter).run_once()
        row = self.row()
        self.assertEqual((row["state"], row["latest"], row["error_code"]),
                         ("available", "0.51.0", None))
        self.assertEqual(row["checked_at"], stamp(START))
        self.assertEqual(row["last_attempt_at"], stamp(START))
        self.assertEqual(row["source_url"], "https://pypi.org/project/presto-ai-dash/0.51.0/")
        shown = self.lifecycle().release()
        self.assertEqual((shown["state"], shown["latest"]), ("available", "0.51.0"))

    def test_the_newest_release_is_current(self):
        self.checker(FakeAdapter([["0.49.0", "0.50.0"]])).run_once()
        row = self.row()
        self.assertEqual((row["state"], row["latest"]), ("current", "0.50.0"))
        self.assertEqual(row["source_url"], "https://pypi.org/project/presto-ai-dash/")

    def test_a_container_candidate_compares_with_the_exact_embedded_label(self):
        adapter = FakeAdapter([["0.50.0-rc4", "0.50.0-rc5"]], docker=True)
        self.checker(adapter, running="0.50.0-rc4", channel="ghcr").run_once()
        row = self.row("ghcr")
        self.assertEqual((row["running"], row["state"], row["latest"]),
                         ("0.50.0-rc4", "available", "0.50.0-rc5"))

    def test_a_stable_install_ignores_a_newer_candidate(self):
        self.checker(FakeAdapter([["0.50.0", "0.51.0rc1"]])).run_once()
        self.assertEqual(self.row()["state"], "current")

    def test_an_unknown_running_version_makes_no_registry_request(self):
        adapter = FakeAdapter([["0.50.0"]])
        self.checker(adapter, running="weird-build").run_once()
        self.assertEqual(adapter.calls, 0)
        self.assertEqual((self.row()["state"], self.row()["error_code"]),
                         ("error", "running_version_unknown"))

    def test_a_development_build_is_never_checked(self):
        adapter = FakeAdapter([["0.50.0"]])
        found = self.checker(adapter, channel="development")
        self.assertFalse(found.enabled)
        self.assertEqual(found.request_check(), ("disabled", None))
        found.start()
        found.stop()
        self.assertEqual(adapter.calls, 0)
        self.assertIsNone(self.row("development"))

    def test_a_switched_off_check_is_never_run(self):
        adapter = FakeAdapter([["0.50.0"]])
        found = self.checker(adapter, enabled=False)
        self.assertEqual(found.request_check(), ("disabled", None))
        self.assertEqual(adapter.calls, 0)

    def test_the_off_switch_reads_the_environment(self):
        for value in ("off", "0", "false", "NO", " Off "):
            self.assertTrue(release_checker.checks_switched_off(
                {"DASH_RELEASE_CHECK": value}), value)
        for value in ("", "on", "1", "yes"):
            self.assertFalse(release_checker.checks_switched_off(
                {"DASH_RELEASE_CHECK": value}), value)
        self.assertFalse(release_checker.checks_switched_off({}))

    def test_the_ambiguous_wheel_is_checked_as_an_early_candidate(self):
        self.checker(FakeAdapter([["0.50.0rc4", "0.50.0rc5"]]), ambiguous=True).run_once()
        row = self.row()
        self.assertEqual((row["running"], row["state"], row["latest"]),
                         ("0.50.0", "available", "0.50.0rc5"))


class AdapterChoiceTests(unittest.TestCase):
    def test_each_installation_gets_exactly_its_own_registry(self):
        self.assertIsInstance(adapter_for(installation("0.50.0", "pypi")), PypiAdapter)
        self.assertIsInstance(adapter_for(installation("0.50.0-rc4", "ghcr")), GhcrAdapter)
        self.assertIsNone(adapter_for(installation("0.50.0", "development")))

    def test_the_hosts_are_fixed_per_registry(self):
        self.assertEqual(adapter_for(installation("0.50.0", "pypi")).HOST, "pypi.org")
        self.assertEqual(adapter_for(installation("0.50.0-rc4", "ghcr")).HOST, "ghcr.io")


class FailureTests(Base):
    def seed_success(self):
        adapter = FakeAdapter([["0.50.0", "0.51.0"]])
        found = self.checker(adapter)
        found.run_once()
        return found, adapter

    def test_a_failure_keeps_the_last_good_result_and_its_time(self):
        found, adapter = self.seed_success()
        good = stamp(self.clock.now)
        self.clock.advance(3600)
        adapter.plan = [RegistryError("timeout")]
        found.run_once()
        row = self.row()
        self.assertEqual((row["state"], row["error_code"], row["latest"]),
                         ("error", "timeout", "0.51.0"))
        self.assertEqual(row["checked_at"], good)
        self.assertEqual(row["last_attempt_at"], stamp(self.clock.now))

    def test_a_failed_check_is_not_reported_as_current_or_available(self):
        found, adapter = self.seed_success()
        adapter.plan = [RegistryError("network_error")]
        found.run_once()
        shown = self.lifecycle().release()
        self.assertEqual((shown["state"], shown["error"]), ("error", "network_error"))
        self.assertEqual(shown["latest"], "0.51.0")

    def test_every_fixed_error_code_is_stored_and_shown(self):
        for code in ("timeout", "network_error", "http_error", "rate_limited",
                     "auth_challenge", "invalid_metadata", "incomplete_listing",
                     "no_eligible_release"):
            with self.subTest(code=code):
                adapter = FakeAdapter([RegistryError(code)])
                self.checker(adapter).run_once()
                shown = self.lifecycle().release()
                self.assertEqual((shown["state"], shown["error"]), ("error", code))

    def test_a_first_failure_has_no_checked_time_and_never_claims_current(self):
        self.checker(FakeAdapter([RegistryError("http_error")])).run_once()
        row = self.row()
        self.assertIsNone(row["checked_at"])
        self.assertIsNone(row["latest"])
        self.assertEqual(row["source_url"], "https://pypi.org/project/presto-ai-dash/")

    def test_an_unexpected_exception_is_a_fixed_code_never_its_text(self):
        class Boom(FakeAdapter):
            def releases(self):
                raise ValueError("secret /home/user/path and token abc")
        with self.assertLogs("presto_ai_dash.release_checker", "ERROR"):
            self.checker(Boom([[]])).run_once()
        row = self.row()
        self.assertEqual(row["error_code"], "invalid_metadata")
        self.assertNotIn("secret", repr(row))

    def test_a_good_result_after_a_failure_clears_the_error(self):
        found, adapter = self.seed_success()
        adapter.plan = [RegistryError("timeout")]
        found.run_once()
        adapter.plan = [["0.50.0", "0.52.0"]]
        self.clock.advance(60)
        found.run_once()
        row = self.row()
        self.assertEqual((row["state"], row["latest"], row["error_code"]),
                         ("available", "0.52.0", None))
        self.assertEqual(row["checked_at"], stamp(self.clock.now))


class RetryAfterPersistenceTests(Base):
    def limited(self, seconds=7200):
        adapter = FakeAdapter([RegistryError("rate_limited", seconds)])
        found = self.checker(adapter)
        found.run_once()
        return found, adapter

    def test_a_manual_check_waits_for_the_registrys_own_delay(self):
        found, adapter = self.limited(7200)
        self.mono.advance(61)          # past the 60 s cooldown
        self.clock.advance(61)
        outcome, wait = found.request_check()
        self.assertEqual(outcome, "rate_limited")
        self.assertEqual(wait, 7200 - 61)
        self.assertEqual(adapter.calls, 1)

    def test_it_is_allowed_again_when_the_delay_has_passed(self):
        found, _ = self.limited(7200)
        self.mono.advance(7300)
        self.clock.advance(7300)
        self.assertEqual(found.request_check(), ("queued", None))

    def test_a_restart_does_not_bypass_it(self):
        self.limited(7200)
        self.clock.advance(700)        # past the 10 minute restart spacing
        self.mono.advance(700)
        restarted = self.checker(FakeAdapter([["0.50.0"]]), startup_delay=15)
        self.assertEqual(restarted._first_delay(), 7200 - 700)
        self.assertEqual(restarted.request_check(), ("rate_limited", 7200 - 700))
        self.assertEqual(restarted.next_check_at(), stamp(START + 7200))

    def test_a_rate_limit_that_names_no_time_still_waits(self):
        found, _ = self.limited(None)
        self.clock.advance(120)
        self.mono.advance(120)
        self.assertEqual(found.request_check()[0], "rate_limited")
        self.assertEqual(self.row()["retry_after_at"],
                         stamp(START + release_checker.RATE_LIMIT_DEFAULT_S))

    def test_the_delay_is_stored_with_the_row_and_shown_nowhere_it_should_not_be(self):
        self.limited(7200)
        self.assertEqual(self.row()["retry_after_at"], stamp(START + 7200))
        shown = self.lifecycle().release()
        self.assertNotIn("retry_after_at", shown)
        self.assertEqual(shown["error"], "rate_limited")

    def test_success_and_other_failures_clear_it(self):
        found, adapter = self.limited(7200)
        adapter.plan = [RegistryError("timeout")]
        self.clock.advance(7300)
        self.mono.advance(7300)
        found.run_once()
        self.assertIsNone(self.row()["retry_after_at"])
        adapter.plan = [RegistryError("rate_limited", 600)]
        self.clock.advance(100)
        found.run_once()
        self.assertIsNotNone(self.row()["retry_after_at"])
        adapter.plan = [["0.50.0"]]
        self.clock.advance(1000)
        found.run_once()
        self.assertIsNone(self.row()["retry_after_at"])

    def test_a_recheck_in_flight_keeps_the_delay_it_started_with(self):
        found, adapter = self.limited(7200)
        adapter.plan = [["0.50.0"]]
        seen = []
        adapter.gate = lambda: seen.append(self.row()["retry_after_at"])
        self.clock.advance(7300)
        found.run_once()
        self.assertEqual(seen, [stamp(START + 7200)])

    def test_an_old_database_gains_the_column(self):
        self.storage.close()
        import sqlite3
        connection = sqlite3.connect(str(self.path))
        connection.execute("ALTER TABLE daemon_release_cache DROP COLUMN retry_after_at")
        connection.execute("PRAGMA user_version = 10")
        connection.commit()
        connection.close()
        reopened = Storage(self.path)
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.schema_version(), Storage.SCHEMA_VERSION)
        reopened.save_release_cache({
            "channel": "pypi", "running": "0.50.0", "latest": None, "checked_at": None,
            "last_attempt_at": None, "state": "error", "error_code": "rate_limited",
            "source_url": None, "retry_after_at": stamp(START)})
        self.assertEqual(reopened.release_cache("pypi")["retry_after_at"], stamp(START))


class StalenessTests(Base):
    def test_a_result_older_than_48_hours_is_stale_not_current(self):
        found = self.checker(FakeAdapter([["0.50.0"]]))
        found.run_once()
        shown = self.lifecycle()
        self.clock.advance(172800)
        self.assertEqual(shown.release()["state"], "current")
        self.clock.advance(1)
        self.assertEqual(shown.release()["state"], "stale")

    def test_an_old_available_result_is_stale_so_the_arrow_goes_away(self):
        self.checker(FakeAdapter([["0.50.0", "0.51.0"]])).run_once()
        shown = self.lifecycle()
        self.clock.advance(172801)
        self.assertEqual(shown.release()["state"], "stale")
        self.assertEqual(shown.release()["latest"], "0.51.0")

    def test_an_error_stays_an_error_as_it_ages(self):
        self.checker(FakeAdapter([["0.50.0"]])).run_once()
        adapter = FakeAdapter([RegistryError("timeout")])
        self.checker(adapter).run_once()
        self.clock.advance(400000)
        self.assertEqual(self.lifecycle().release()["state"], "error")

    def test_before_any_result_it_is_unknown_with_a_checker_and_disabled_without(self):
        self.assertEqual(self.lifecycle().release()["state"], "unknown")
        plain = DeviceLifecycle(self.storage, clock=self.clock, running_version="0.50.0",
                                channel="pypi")
        self.assertEqual(plain.release()["state"], "disabled")
        self.assertEqual(self.lifecycle(channel="development").release()["state"], "disabled")


class CacheScopeTests(Base):
    def test_cache_from_another_running_version_is_ignored(self):
        self.checker(FakeAdapter([["0.50.0", "0.51.0"]])).run_once()
        self.assertEqual(self.lifecycle().release()["state"], "available")
        upgraded = self.lifecycle(running="0.51.0")
        shown = upgraded.release()
        self.assertEqual((shown["state"], shown["latest"]), ("unknown", None))

    def test_an_upgrade_clears_the_old_notice(self):
        self.checker(FakeAdapter([["0.50.0", "0.51.0"]])).run_once()
        self.assertEqual(self.lifecycle().release()["state"], "available")
        self.assertNotEqual(self.lifecycle(running="0.51.0").release()["state"], "available")

    def test_a_new_check_for_a_new_version_does_not_inherit_old_evidence(self):
        self.checker(FakeAdapter([["0.50.0", "0.51.0"]])).run_once()
        adapter = FakeAdapter([RegistryError("http_error")])
        self.checker(adapter, running="0.51.0").run_once()
        row = self.row()
        self.assertEqual((row["running"], row["latest"], row["checked_at"]),
                         ("0.51.0", None, None))

    def test_another_channels_row_is_not_evidence(self):
        self.storage.save_release_cache({
            "channel": "ghcr", "running": "0.50.0", "latest": "9.9.9", "checked_at": stamp(START),
            "last_attempt_at": stamp(START), "state": "available", "error_code": None,
            "source_url": None})
        self.assertEqual(self.lifecycle(channel="pypi").release()["state"], "unknown")

    def test_a_first_check_is_recorded_as_checking_while_it_runs(self):
        seen = []
        adapter = FakeAdapter([["0.50.0"]])
        adapter.gate = lambda: seen.append(self.lifecycle().release()["state"])
        self.checker(adapter).run_once()
        self.assertEqual(seen, ["checking"])
        self.assertEqual(self.row()["state"], "current")

    def test_a_restart_clears_a_check_the_last_process_abandoned(self):
        self.storage.save_release_cache({
            "channel": "pypi", "running": "0.50.0", "latest": None, "checked_at": None,
            "last_attempt_at": stamp(START), "state": "checking", "error_code": None,
            "source_url": None})
        self.assertEqual(self.lifecycle().release()["state"], "checking")
        self.storage.clear_release_checking()
        self.assertEqual(self.lifecycle().release()["state"], "unknown")

    def test_the_result_survives_a_restart(self):
        self.checker(FakeAdapter([["0.50.0", "0.51.0"]])).run_once()
        self.storage.close()
        reopened = Storage(self.path)
        self.addCleanup(reopened.close)
        reopened.clear_release_checking()
        found = DeviceLifecycle(reopened, clock=self.clock, running_version="0.50.0",
                                channel="pypi")
        found.release_checks = True
        self.assertEqual(found.release()["state"], "available")

    def test_a_recheck_keeps_showing_the_last_result_while_it_runs(self):
        found = self.checker(FakeAdapter([["0.50.0", "0.51.0"]]))
        found.run_once()
        seen = []
        adapter = FakeAdapter([["0.50.0", "0.51.0"]])
        adapter.gate = lambda: seen.append(self.lifecycle().release()["state"])
        self.clock.advance(100)
        self.checker(adapter).run_once()
        self.assertEqual(seen, ["available"])


class ScheduleTests(Base):
    def test_the_first_check_waits_for_the_startup_delay(self):
        found = self.checker(FakeAdapter([["0.50.0"]]), startup_delay=15)
        self.assertEqual(found._first_delay(), 15)

    def test_a_restart_does_not_ask_again_within_the_spacing(self):
        self.checker(FakeAdapter([["0.50.0"]])).run_once()
        self.clock.advance(100)
        found = self.checker(FakeAdapter([["0.50.0"]]), startup_delay=15)
        self.assertEqual(found._first_delay(), release_checker.RESTART_SPACING_S - 100)

    def test_success_waits_about_a_day_with_bounded_spread(self):
        found = self.checker(FakeAdapter([["0.50.0"]]))
        found.run_once()
        for _ in range(50):
            self.assertAlmostEqual(found._next_delay(), 86400, delta=86400 * 0.1 + 1)

    def test_failures_back_off_and_are_capped(self):
        adapter = FakeAdapter([RegistryError("timeout")])
        found = self.checker(adapter)
        delays = []
        for _ in range(7):
            found.run_once()
            self.mono.advance(1)
            delays.append(found._next_delay())
        self.assertEqual(delays[:4], list(release_checker.BACKOFF_S))
        self.assertEqual(set(delays[4:]), {release_checker.BACKOFF_S[-1]})
        self.assertLessEqual(max(delays), 86400)

    def test_a_rate_limit_waits_at_least_as_long_as_the_registry_asked(self):
        found = self.checker(FakeAdapter([RegistryError("rate_limited", 7200)]))
        found.run_once()
        self.assertEqual(found._next_delay(), 7200)

    def test_success_resets_the_backoff(self):
        adapter = FakeAdapter([RegistryError("timeout")])
        found = self.checker(adapter)
        found.run_once()
        adapter.plan = [["0.50.0"]]
        found.run_once()
        self.assertEqual(found._failures, 0)

    def test_next_check_is_reported_from_the_last_attempt(self):
        found = self.checker(FakeAdapter([["0.50.0"]]))
        found.run_once()
        self.assertEqual(found.next_check_at(), stamp(START + 86400))


class RequestTests(Base):
    def test_a_request_is_queued_then_coalesced_then_throttled(self):
        adapter = FakeAdapter([["0.50.0"]])
        found = self.checker(adapter)
        self.assertEqual(found.request_check(), ("queued", None))
        self.assertEqual(found.request_check(), ("coalesced", None))
        self.assertTrue(found.is_checking())
        self.assertEqual(adapter.calls, 0)
        found.run_once()
        self.assertFalse(found.is_checking())
        outcome, wait = found.request_check()
        self.assertEqual(outcome, "throttled")
        self.assertEqual(wait, 60)
        self.mono.advance(59.5)
        self.assertEqual(found.request_check()[0], "throttled")
        self.mono.advance(1)
        self.assertEqual(found.request_check(), ("queued", None))

    def test_the_cooldown_is_one_start_per_sixty_seconds(self):
        found = self.checker(FakeAdapter([["0.50.0"]]))
        starts = 0
        for _ in range(130):
            if found.request_check()[0] == "queued":
                starts += 1
                found.run_once()
            self.mono.advance(1)
        self.assertEqual(starts, 3)

    def test_concurrent_requests_start_one_check(self):
        adapter = FakeAdapter([["0.50.0"]])
        release_gate, entered = threading.Event(), threading.Event()
        adapter.gate = lambda: (entered.set(), release_gate.wait(5))
        found = self.checker(adapter)
        found.start()
        self.addCleanup(found.stop)
        outcomes = []
        threads = [threading.Thread(target=lambda: outcomes.append(found.request_check()[0]))
                   for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(outcomes.count("queued"), 1)
        self.assertTrue(entered.wait(5))
        self.assertEqual(found.request_check()[0], "coalesced")
        release_gate.set()
        deadline = time.time() + 5
        while found.is_checking() and time.time() < deadline:
            time.sleep(0.01)
        self.assertEqual(adapter.calls, 1)

    def test_run_once_does_not_double_a_running_check(self):
        adapter = FakeAdapter([["0.50.0"]])
        found = self.checker(adapter)
        inner = []
        adapter.gate = lambda: inner.append(found.run_once())
        found.run_once()
        self.assertEqual(inner, [False])
        self.assertEqual(adapter.calls, 1)


class ThreadTests(Base):
    def test_start_returns_at_once_even_when_the_registry_hangs(self):
        gate = threading.Event()
        adapter = FakeAdapter([["0.50.0"]])
        adapter.gate = lambda: gate.wait(10)
        found = self.checker(adapter)
        started = time.time()
        found.start()
        self.assertLess(time.time() - started, 1)
        deadline = time.time() + 5
        while adapter.calls == 0 and time.time() < deadline:
            time.sleep(0.01)
        self.assertTrue(found.is_checking())
        started = time.time()
        self.assertEqual(self.lifecycle().release()["state"], "checking")
        self.assertLess(time.time() - started, 1)
        gate.set()
        found.stop()

    def test_the_background_thread_runs_a_check_after_startup(self):
        adapter = FakeAdapter([["0.50.0", "0.51.0"]])
        found = self.checker(adapter)
        found.start()
        self.addCleanup(found.stop)
        deadline = time.time() + 5
        while self.row() is None or self.row()["state"] == "checking":
            if time.time() > deadline:
                self.fail("no check ran")
            time.sleep(0.01)
        self.assertEqual(self.row()["state"], "available")

    def test_shutdown_during_a_check_writes_nothing_and_returns_promptly(self):
        gate = threading.Event()
        adapter = FakeAdapter([["0.50.0", "0.51.0"]])
        entered = threading.Event()
        adapter.gate = lambda: (entered.set(), gate.wait(10))
        found = self.checker(adapter)
        found.start()
        self.assertTrue(entered.wait(5))
        started = time.time()
        timer = threading.Timer(0.2, gate.set)
        timer.start()
        found.stop(timeout=3)
        timer.join()
        self.assertLess(time.time() - started, 3)
        deadline = time.time() + 3
        while found.is_checking() and time.time() < deadline:
            time.sleep(0.01)
        # The first check was recorded as in flight and its result abandoned.
        self.assertEqual(self.row()["state"], "checking")
        self.assertIsNone(self.row()["checked_at"])

    def test_the_cancel_signal_reaches_the_adapter(self):
        seen = []
        found = ReleaseChecker(self.storage, installation(), clock=self.clock,
                               monotonic=self.mono, startup_delay=0,
                               adapter_factory=lambda cancelled: seen.append(cancelled) or
                               FakeAdapter([["0.50.0"]]))
        found.run_once()
        self.assertFalse(seen[0]())
        found.stop()
        self.assertTrue(seen[0]())

    def test_cancelled_is_not_a_failure(self):
        class Gone(FakeAdapter):
            def releases(self):
                raise Cancelled()
        found = self.checker(Gone([[]]))
        found.run_once()
        self.assertNotEqual(self.row()["state"], "error")
        self.assertEqual(found._failures, 0)

    def test_stop_is_safe_before_start_and_twice(self):
        found = self.checker(FakeAdapter([["0.50.0"]]))
        found.stop()
        found.stop()


class InstructionTests(unittest.TestCase):
    def commands(self, channel, latest, state="available"):
        result = release_instructions(channel, latest, state)
        return result, " | ".join(step["text"] for group in result["groups"]
                                  for step in group["steps"] if step["kind"] == "command")

    def test_a_stable_wheel_target_offers_upgrade_and_pinned_commands(self):
        result, text = self.commands("pypi", "0.51.0")
        self.assertIn("pipx upgrade presto-ai-dash", text)
        self.assertIn('pipx install --force "presto-ai-dash==0.51.0"', text)
        self.assertIn("python -m pip install --upgrade presto-ai-dash", text)
        self.assertEqual(result["target"], "0.51.0")

    def test_a_candidate_wheel_target_names_the_exact_version(self):
        result, text = self.commands("pypi", "0.50.0rc5")
        self.assertIn('pipx install --force "presto-ai-dash==0.50.0rc5"', text)
        self.assertIn('python -m pip install --upgrade "presto-ai-dash==0.50.0rc5"', text)
        self.assertNotIn("pipx upgrade", text)

    def test_a_pinned_image_target_changes_dash_image_then_pulls_and_recreates(self):
        result, text = self.commands("ghcr", "0.50.0-rc5")
        self.assertIn("DASH_IMAGE=ghcr.io/jake1164/presto-ai-dash:0.50.0-rc5", text)
        self.assertIn("docker compose pull dash", text)
        self.assertIn("docker compose up -d dash", text)
        self.assertNotIn("latest stable tag", " ".join(g["title"] for g in result["groups"]))
        self.assertIn("recreated", result["note"])
        self.assertIn("cannot see how your Compose file", result["note"])

    def test_a_stable_image_target_also_covers_latest_followers(self):
        result, _ = self.commands("ghcr", "0.51.0")
        titles = " ".join(group["title"] for group in result["groups"])
        self.assertIn("latest stable tag", titles)
        self.assertIn("DASH_IMAGE", titles)

    def test_no_target_means_a_placeholder_never_a_guess(self):
        for state in ("current", "error", "stale", "unknown", "checking", "disabled"):
            _, text = self.commands("pypi", "0.51.0", state)
            self.assertIn("<version>", text)
            self.assertNotIn("0.51.0", text)

    def test_an_unsafe_label_never_reaches_a_command(self):
        for label in ("0.51.0; rm -rf /", "$(x)", "a b", "x" * 40, None):
            _, text = self.commands("pypi", label)
            self.assertNotIn(";", text)
            self.assertIn("<version>", text)

    def test_an_ambiguous_wheel_is_told_how_to_install_the_final_release(self):
        result = release_instructions("pypi", None, "current", ambiguous="0.50.0")
        group = result["groups"][-1]
        self.assertIn("Not sure", group["title"])
        commands = [step["text"] for step in group["steps"] if step["kind"] == "command"]
        self.assertIn('pipx install --force "presto-ai-dash==0.50.0"', commands)
        self.assertIn('python -m pip install --force-reinstall "presto-ai-dash==0.50.0"', commands)
        self.assertIn("cannot be told apart", group["steps"][0]["text"])
        self.assertIn('pipx install --force "presto-ai-dash==0.50.0"', result["update"])

    def test_other_wheels_and_images_get_no_such_group(self):
        for result in (release_instructions("pypi", None, "current"),
                       release_instructions("ghcr", None, "current", ambiguous="0.50.0")):
            self.assertNotIn("Not sure", " ".join(g["title"] for g in result["groups"]))

    def test_a_development_build_gets_no_commands(self):
        result = release_instructions("development", "0.51.0", "available")
        self.assertEqual((result["update"], result["groups"]), ([], []))


if __name__ == "__main__":
    unittest.main()
