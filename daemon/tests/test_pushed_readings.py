"""Readings pushed from another machine: activity, the usage cache, Codex.

A containerised daemon has no Claude files and no Codex login. A collector on
the machine that has them pushes what the daemon's own readers would find, and
the sources use it. Everything is checked on both sides: a strict validator on
the way in, and sources that treat a pushed reading like a local one - and
never let it pass for something fresher than it is.
"""

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from presto_ai_dash import ingest_extras
from presto_ai_dash.collectors import codex_report
from presto_ai_dash.ingest import SCHEMA_NAME, ingest, validate
from presto_ai_dash.models import now_utc
from presto_ai_dash.sources.claude import ClaudeSource
from presto_ai_dash.sources.codex import CodexSource
from test_codex_reset_credits import NOW as FIXTURE_NOW, credit_row, days_from_now, response
from test_management import ManagementBase


def stamp(moment):
    return moment.isoformat()


def window(window_id, label, minutes, percent, resets):
    return {"id": window_id, "label": label, "period_minutes": minutes,
            "used_percent": percent, "resets_at": resets,
            "state": "available" if percent is not None else "unavailable"}


def envelope(name="claude-watch", **extra):
    result = {"schema": SCHEMA_NAME,
              "collector": {"name": name, "version": "2", "host": "laptop"},
              "client_time": stamp(datetime.now(timezone.utc))}
    result.update(extra)
    return result


def activity(now=None, **changes):
    now = now or datetime.now(timezone.utc)
    value = {"available": True, "complete": True,
             "last_request_at": stamp(now - timedelta(seconds=30)),
             "last_model": "claude-sonnet-5-5", "last_entrypoint": "claude-vscode",
             "requests_5h": 12, "tokens_5h": 45000, "tokens_7d": 900000}
    value.update(changes)
    return value


def usage_cache(now=None, **changes):
    now = now or datetime.now(timezone.utc)
    value = {"available": True, "reason": None,
             "fetched_at": stamp(now - timedelta(minutes=20)),
             "windows": [window("five_hour", "5h", 300, 31.0, stamp(now + timedelta(hours=3))),
                         window("seven_day", "Weekly", 10080, 58.0, stamp(now + timedelta(days=4)))],
             "credit": {"limit": 50.0, "used": 12.5, "currency": "USD",
                        "resets_at": stamp(now + timedelta(days=20))},
             "account_changed": False,
             "refreshes": [stamp(now - timedelta(hours=2)), stamp(now - timedelta(minutes=20))],
             "since": stamp(now - timedelta(days=1))}
    value.update(changes)
    return value


class PushBase(ManagementBase):
    def setUp(self):
        super().setUp()
        self.disable_fake()
        self.claude = self.manager.save({"type": "claude", "name": "Claude", "enabled": True,
                                         "config": {"settings_path": str(Path(self.temp.name) / "s.json")}})
        self.codex = self.manager.save({"type": "codex", "name": "Codex", "enabled": True,
                                        "config": {}})

    def claude_source(self, **config):
        base = {"settings_path": str(Path(self.temp.name) / "s.json")}
        base.update(config)
        source = ClaudeSource(base)
        source.id = self.claude["id"]
        source.attach_storage(self.storage)
        return source

    def codex_source(self):
        source = CodexSource({})
        source.id = self.codex["id"]
        source.attach_storage(self.storage)
        return source

    def push(self, row, body, now=None):
        return ingest(self.storage, row["id"], body, now=now)

    def quota(self, snapshot):
        return snapshot.metrics["quota"]


class ActivityValidationTests(unittest.TestCase):
    NOW = datetime.now(timezone.utc)

    def clean(self, **changes):
        return ingest_extras.clean_activity(activity(self.NOW, **changes), self.NOW)

    def test_a_good_summary_is_kept_field_for_field(self):
        cleaned = self.clean()
        self.assertEqual(cleaned["requests_5h"], 12)
        self.assertEqual(cleaned["last_model"], "claude-sonnet-5-5")

    def test_a_field_the_local_reader_does_not_return_is_refused(self):
        for key in ("cwd", "path", "session_id", "prompt", "transcript_path"):
            with self.subTest(key=key):
                with self.assertRaises(ValueError):
                    ingest_extras.clean_activity(dict(activity(self.NOW), **{key: "x"}), self.NOW)

    def test_wrong_types_and_ranges_are_refused(self):
        for changes in ({"available": "yes"}, {"requests_5h": -1}, {"requests_5h": 1.5},
                        {"tokens_7d": 10 ** 14}, {"last_model": "x" * 65},
                        {"last_model": "café"}, {"last_entrypoint": ""},
                        {"last_request_at": "yesterday"},
                        {"last_request_at": stamp(self.NOW + timedelta(hours=1))},
                        {"reason": "x" * 121}):
            with self.subTest(changes=changes):
                with self.assertRaises(ValueError):
                    self.clean(**changes)

    def test_a_request_a_moment_ahead_is_clamped_not_refused(self):
        ahead = stamp(self.NOW + timedelta(minutes=2))
        self.assertLessEqual(datetime.fromisoformat(self.clean(last_request_at=ahead)["last_request_at"]),
                             self.NOW)

    def test_an_unavailable_summary_is_valid(self):
        cleaned = self.clean(available=False, reason="no session directory",
                             last_request_at=None, last_model=None, last_entrypoint="?",
                             requests_5h=0, tokens_5h=0, tokens_7d=0)
        self.assertFalse(cleaned["available"])


class UsageCacheValidationTests(unittest.TestCase):
    NOW = datetime.now(timezone.utc)

    def check_window(self, *args):
        from presto_ai_dash.ingest import _check_window
        return _check_window(*args)

    def clean(self, **changes):
        return ingest_extras.clean_usage_cache(usage_cache(self.NOW, **changes), self.NOW,
                                               self.check_window)

    def test_a_good_reading_keeps_its_own_fetch_time(self):
        cleaned = self.clean()
        fetched = datetime.fromisoformat(cleaned["fetched_at"])
        self.assertAlmostEqual((self.NOW - fetched).total_seconds(), 1200, delta=1)
        self.assertEqual(cleaned["credit"]["limit"], 50.0)

    def test_a_reading_from_the_future_is_refused_and_a_small_skew_is_capped(self):
        with self.assertRaises(ValueError):
            self.clean(fetched_at=stamp(self.NOW + timedelta(hours=2)))
        capped = self.clean(fetched_at=stamp(self.NOW + timedelta(minutes=2)))
        self.assertLessEqual(datetime.fromisoformat(capped["fetched_at"]), self.NOW)

    def test_a_reading_more_than_seven_days_old_is_refused(self):
        with self.assertRaises(ValueError):
            self.clean(fetched_at=stamp(self.NOW - timedelta(days=8)))
        self.clean(fetched_at=stamp(self.NOW - timedelta(days=6)))

    def test_an_available_reading_needs_a_fetch_time_and_an_unavailable_one_has_none(self):
        with self.assertRaises(ValueError):
            self.clean(fetched_at=None)
        with self.assertRaises(ValueError):
            self.clean(available=False, reason="x", fetched_at=stamp(self.NOW))
        cleaned = self.clean(available=False, reason="no usage cache found", fetched_at=None,
                             windows=[], credit=None)
        self.assertIsNone(cleaned["fetched_at"])

    def test_extra_keys_and_a_path_are_refused(self):
        for key in ("path", "message", "accountUuid", "email", "organization"):
            with self.subTest(key=key):
                with self.assertRaises(ValueError):
                    ingest_extras.clean_usage_cache(
                        dict(usage_cache(self.NOW), **{key: "x"}), self.NOW, self.check_window)

    def test_windows_obey_the_envelope_window_rules(self):
        bad = [window("five_hour", "5h", 300, 31.0, None),
               dict(window("seven_day", "Weekly", 10080, 5.0, None), extra=1)]
        for windows in (bad[1:], [window("other", "x", 5, 1.0, None)],
                        [window("five_hour", "5h", 300, -1.0, None)],
                        [window("five_hour", "5h", 300, 1.0, None)] * 2):
            with self.subTest(windows=windows):
                with self.assertRaises(ValueError):
                    self.clean(windows=windows)

    def test_the_credit_and_lists_are_checked(self):
        for changes in ({"credit": {"limit": 5, "used": 1, "currency": "EUR",
                                    "resets_at": stamp(self.NOW)}},
                        {"credit": {"limit": -5, "used": 1, "currency": "USD",
                                    "resets_at": stamp(self.NOW)}},
                        {"credit": {"limit": 5, "used": 1, "currency": "USD"}},
                        {"refreshes": [stamp(self.NOW)] * 51},
                        {"since": "long ago"}, {"account_changed": "no"}):
            with self.subTest(changes=changes):
                with self.assertRaises(ValueError):
                    self.clean(**changes)


class CodexValidationTests(unittest.TestCase):
    NOW = datetime.now(timezone.utc)

    def test_reset_credits(self):
        good = {"available": 2, "expires_at": [stamp(self.NOW + timedelta(days=3))]}
        self.assertEqual(ingest_extras.clean_reset_credits(good, self.NOW)["available"], 2)
        for bad in ({"available": -1, "expires_at": []}, {"available": 100, "expires_at": []},
                    {"available": 1, "expires_at": [stamp(self.NOW)] * 9},
                    {"available": 1, "expires_at": ["soon"]},
                    {"available": 1, "expires_at": [], "id": "credit-1"},
                    {"available": 1, "expires_at": [stamp(self.NOW + timedelta(days=99999))]}):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    ingest_extras.clean_reset_credits(bad, self.NOW)
        none = ingest_extras.clean_reset_credits({"available": None, "expires_at": []}, self.NOW)
        self.assertIsNone(none["available"])

    def test_credits(self):
        self.assertEqual(ingest_extras.clean_credits({"balance": "12.5", "unlimited": False}),
                         {"balance": "12.5", "unlimited": False})
        self.assertIsNone(ingest_extras.clean_credits({"balance": None, "unlimited": None})["balance"])
        for bad in ({"balance": "0E-10", "unlimited": False}, {"balance": "1" * 17, "unlimited": None},
                    {"balance": 5, "unlimited": None}, {"balance": "5", "unlimited": "no"},
                    {"balance": "5", "unlimited": None, "account": "x"}):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    ingest_extras.clean_credits(bad)


class WhichSourceTakesWhatTests(PushBase):
    def test_a_source_refuses_the_readings_it_does_not_take(self):
        with self.assertRaises(ValueError):
            self.push(self.codex, envelope("codex-watch", activity=activity()))
        with self.assertRaises(ValueError):
            self.push(self.claude, envelope(reset_credits={"available": 1, "expires_at": []}))
        with self.assertRaises(ValueError):
            validate(envelope(), type("Plain", (), {}))
            validate(dict(envelope(), activity=activity()), type("Plain", (), {}))

    def test_readings_without_quota_need_a_source_that_has_default_windows(self):
        with self.assertRaises(ValueError):
            self.push(self.codex, envelope("codex-watch", credits={"balance": "1", "unlimited": None}))


class ClaudePushTests(PushBase):
    def test_activity_pushed_alone_reaches_the_report_and_the_status(self):
        self.assertTrue(self.push(self.claude, envelope(activity=activity())))
        quota = self.quota(self.claude_source().collect())
        self.assertTrue(quota["local_activity"]["available"])
        self.assertEqual(quota["local_activity"]["tokens_5h"], 45000)
        self.assertNotIn("pushed_activity", quota)
        self.assertEqual(self.claude_source().collect().status, "active")

    def test_the_quota_windows_stay_unavailable_until_a_status_line_reports_them(self):
        self.push(self.claude, envelope(activity=activity()))
        quota = self.quota(self.claude_source().collect())
        self.assertEqual([w["state"] for w in quota["windows"]], ["unavailable", "unavailable"])

    def test_a_pushed_usage_cache_fills_the_windows_with_its_own_age(self):
        self.push(self.claude, envelope(usage_cache=usage_cache()))
        quota = self.quota(self.claude_source().collect())
        five = next(w for w in quota["windows"] if w["id"] == "five_hour")
        self.assertEqual(five["used_percent"], 31.0)
        self.assertEqual(five["source"], "usage cache")
        age = (now_utc() - datetime.fromisoformat(five["observed_at"])).total_seconds()
        self.assertAlmostEqual(age, 1200, delta=5)  # the fetch time, not the receive time
        self.assertEqual(quota["usage_cache"]["credit"]["used"], 12.5)
        self.assertNotIn("pushed_usage_cache", quota)

    def test_a_later_status_line_push_does_not_erase_the_pushed_readings(self):
        self.push(self.claude, envelope(activity=activity(), usage_cache=usage_cache()))
        self.push(self.claude, envelope("claude-statusline", windows=[
            window("five_hour", "5h", 300, 40.0, stamp(now_utc() + timedelta(hours=2))),
            window("seven_day", "Weekly", 10080, 60.0, stamp(now_utc() + timedelta(days=3)))],
            evidence={"five_hour": "a" * 64, "seven_day": "b" * 64}))
        quota = self.quota(self.claude_source().collect())
        self.assertTrue(quota["local_activity"]["available"])
        self.assertTrue(quota["usage_cache"]["available"])
        five = next(w for w in quota["windows"] if w["id"] == "five_hour")
        self.assertEqual(five["used_percent"], 40.0)  # the newer terminal reading wins

    def test_a_push_of_one_reading_keeps_the_other(self):
        self.push(self.claude, envelope(activity=activity()))
        self.push(self.claude, envelope(usage_cache=usage_cache()))
        quota = self.quota(self.claude_source().collect())
        self.assertTrue(quota["local_activity"]["available"])
        self.assertTrue(quota["usage_cache"]["available"])

    def test_activity_goes_quiet_when_the_collector_stops(self):
        long_ago = now_utc() - timedelta(minutes=30)
        self.push(self.claude, envelope(activity=activity(long_ago)), now=long_ago)
        quota = self.quota(self.claude_source().collect())
        self.assertFalse(quota["local_activity"]["available"])
        self.assertIn("stopped reporting", quota["local_activity"]["reason"])
        self.assertEqual(quota["local_activity"]["tokens_5h"], 0)

    def test_a_reading_that_the_collector_could_not_make_is_reported_as_such(self):
        self.push(self.claude, envelope(usage_cache={
            "available": False, "reason": "no usage cache found", "fetched_at": None,
            "windows": [], "credit": None, "account_changed": False,
            "refreshes": [], "since": None}))
        quota = self.quota(self.claude_source().collect())
        self.assertFalse(quota["usage_cache"]["available"])
        self.assertIn("laptop", quota["usage_cache"]["message"])
        self.assertNotIn("/", quota["usage_cache"]["message"])

    def test_the_daemons_own_reader_wins_when_it_has_a_reading(self):
        self.push(self.claude, envelope(activity=activity(tokens_5h=1)))
        local = {"available": True, "complete": True, "last_request_at": None,
                 "last_model": None, "last_entrypoint": "?", "requests_5h": 0,
                 "tokens_5h": 777, "tokens_7d": 777}
        source = self.claude_source(activity_logs=True)
        with patch.object(source, "_activity", return_value=local):
            quota = self.quota(source.collect())
        self.assertEqual(quota["local_activity"]["tokens_5h"], 777)

    def test_the_pushed_reading_fills_in_when_the_local_reader_has_nothing(self):
        self.push(self.claude, envelope(activity=activity(tokens_5h=4242)))
        source = self.claude_source(activity_logs=True)  # no projects directory here
        quota = self.quota(source.collect())
        self.assertEqual(quota["local_activity"]["tokens_5h"], 4242)

    def test_a_heartbeat_alone_does_not_break_the_source(self):
        self.assertTrue(self.push(self.claude, envelope()))
        snapshot = self.claude_source().collect()
        self.assertEqual(snapshot.status, "warning")
        self.assertEqual(self.quota(snapshot)["windows"][0]["state"], "unavailable")

    def test_nothing_private_is_stored(self):
        self.push(self.claude, envelope(activity=activity(), usage_cache=usage_cache()))
        stored = json.dumps(self.storage.observation(self.claude["id"]))
        for needle in ("cwd", "transcript", "accountUuid", "email", "projects"):
            self.assertNotIn(needle, stored)


class CodexPushTests(PushBase):
    def setUp(self):
        super().setUp()
        # No test starts a real Codex: this machine may have a real login.
        # A test that needs a local reading patches these again.
        from presto_ai_dash.sources._codex_rpc import CodexReportingError
        for name, effect in (("find_executable", CodexReportingError("Install Codex")),
                             ("read_subscription", AssertionError("a real Codex was started"))):
            patcher = patch("presto_ai_dash.sources.codex." + name, side_effect=effect)
            patcher.start()
            self.addCleanup(patcher.stop)

    def reading(self, now=None, **extra):
        now = now or datetime.now(timezone.utc)
        return envelope("codex-watch", windows=[
            window("codex:primary", "Weekly", 10080, 14, stamp(now + timedelta(days=7))),
            {"id": "codex:secondary", "label": "Secondary", "period_minutes": None,
             "used_percent": None, "resets_at": None, "state": "unavailable"}],
            evidence={"codex:primary": "a" * 64, "codex:secondary": "b" * 64},
            reset_credits={"available": 2, "expires_at": [stamp(now + timedelta(days=4))]},
            credits={"balance": "0", "unlimited": False}, **extra)

    def test_a_pushed_reading_is_used_without_starting_a_local_program(self):
        self.assertTrue(self.push(self.codex, self.reading()))
        source = self.codex_source()
        with patch("presto_ai_dash.sources.codex.find_executable",
                   side_effect=AssertionError("must not look for Codex")):
            snapshot = source.collect()
        quota = self.quota(snapshot)
        self.assertEqual(snapshot.status, "ok")
        self.assertEqual(quota["windows"][0]["used_percent"], 14)
        self.assertEqual(quota["reset_credits"]["available"], 2)
        self.assertEqual(quota["credits"]["balance"], "0")
        self.assertIsNone(quota["error"])

    def test_a_window_at_the_limit_is_rate_limited(self):
        body = self.reading()
        body["windows"][0]["used_percent"] = 100
        self.push(self.codex, body)
        self.assertEqual(self.codex_source().collect().status, "rate_limited")

    def test_a_quiet_collector_and_no_local_codex_shows_the_last_reading_as_it_ages(self):
        old = now_utc() - timedelta(hours=2)
        self.push(self.codex, self.reading(old), now=old)
        snapshot = self.codex_source().collect()  # no codex program on this machine
        quota = self.quota(snapshot)
        self.assertNotEqual(snapshot.status, "offline")
        age = (now_utc() - datetime.fromisoformat(quota["windows"][0]["observed_at"])).total_seconds()
        self.assertGreater(age, 3600)

    def test_with_no_push_and_no_codex_the_source_is_offline_as_before(self):
        snapshot = self.codex_source().collect()
        self.assertEqual(snapshot.status, "offline")

    def test_a_stale_push_with_a_working_local_codex_uses_the_local_one(self):
        old = now_utc() - timedelta(hours=2)
        self.push(self.codex, self.reading(old), now=old)
        source = self.codex_source()
        with patch("presto_ai_dash.sources.codex.find_executable", return_value="codex"), \
                patch("presto_ai_dash.sources.codex.read_subscription",
                      return_value=response()):
            snapshot = source.collect()
        self.assertEqual(snapshot.status, "ok")
        self.assertEqual(self.quota(snapshot)["windows"][0]["used_percent"], 14)

    def test_a_push_stays_inside_what_codex_may_carry(self):
        with self.assertRaises(ValueError):
            self.push(self.codex, dict(self.reading(), usage_cache=usage_cache()))


class ReportMatchesTheSourceTests(unittest.TestCase):
    """The collector's reading of Codex's answer means what the daemon's own does."""

    def same(self, data):
        report, limited = CodexSource().parse(data, FIXTURE_NOW)
        windows, pushed_limited, reset_credits, credits = codex_report.rate_limit_windows(data)
        self.assertEqual(limited, pushed_limited)
        keys = ("id", "label", "period_minutes", "used_percent", "resets_at", "state")
        self.assertEqual([{key: w[key] for key in keys} for w in report["windows"]], windows)
        self.assertEqual(report["reset_credits"], reset_credits)
        self.assertEqual(report["credits"], credits)

    def test_the_ordinary_answer(self):
        self.same(response({"availableCount": 2,
                            "credits": [credit_row(expires_at=days_from_now(4))]}))

    def test_a_balance_in_exponent_notation(self):
        self.same(response(balance="1.5E+2"))

    def test_several_buckets(self):
        data = response()
        data["rateLimitsByLimitId"]["other_bucket"] = {
            "primary": {"usedPercent": 100, "windowDurationMins": 300,
                        "resetsAt": days_from_now(1)}, "secondary": None}
        self.same(data)

    def test_the_legacy_single_bucket(self):
        self.same({"rateLimits": {"limitId": "codex",
                                  "primary": {"usedPercent": 3, "windowDurationMins": 300,
                                              "resetsAt": days_from_now(1)}}})

    def test_answers_that_are_not_understood_raise(self):
        for data in ({}, {"rateLimits": 5}, {"rateLimitsByLimitId": {}}, [],
                     {"rateLimitsByLimitId": {"codex": {"primary": {"usedPercent": -1}}}},
                     {"rateLimitsByLimitId": {"codex": {"primary": {"usedPercent": float("nan")}}}}):
            with self.subTest(data=data):
                with self.assertRaises(ValueError):
                    codex_report.rate_limit_windows(data)

    def test_ids_fit_the_envelope(self):
        data = {"rateLimitsByLimitId": {"Weird Bucket/Name-" + "x" * 80: {
            "primary": {"usedPercent": 1, "windowDurationMins": 60, "resetsAt": days_from_now(1)}}}}
        windows = codex_report.rate_limit_windows(data)[0]
        for item in windows:
            self.assertRegex(item["id"], r"^[a-z0-9_.:]{1,32}$")


if __name__ == "__main__":
    unittest.main()
