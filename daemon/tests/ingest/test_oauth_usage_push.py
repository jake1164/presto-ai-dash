"""The experimental OAuth reading on its way through the collector and the daemon.

The readings stay honest about what they are: their own source, their own
fetch time, the existing rules about which reading wins. With the setting off,
nothing changes. No test contacts Anthropic: the embedded reader's opener is
replaced, and the daemon is a throwaway on loopback.
"""

from datetime import datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from presto_ai_dash import api as api_module, dashboard, ingest_extras
from presto_ai_dash.collectors import presto_collector as collector
from presto_ai_dash.ingest import _check_window, export_age, validate
from presto_ai_dash.models import now_utc
from presto_ai_dash.quotas import claude_provenance
from presto_ai_dash.sources.claude import ClaudeSource
from ingest.test_collector_watch import FolderBase, PushBase, session_line
from ingest.test_pushed_readings import PushBase as ReadingsBase
from ingest.test_pushed_readings import envelope, stamp, usage_cache, window
from providers.test_claude_oauth_usage import (BODY, Opener, REFRESH, Reply, SECRET,
                                               block_real_network, http_error)


def oauth_reading(now=None, five=44.0, seven=66.0, age=timedelta(seconds=30)):
    now = now or datetime.now(timezone.utc)
    return {"fetched_at": stamp(now - age),
            "windows": [window("five_hour", "5h", 300, five, stamp(now + timedelta(hours=3))),
                        window("seven_day", "Weekly", 10080, seven, stamp(now + timedelta(days=4)))]}


def status_line(five=10.0, seven=20.0, now=None):
    now = now or datetime.now(timezone.utc)
    windows = [window("five_hour", "5h", 300, five, stamp(now + timedelta(hours=3))),
               window("seven_day", "Weekly", 10080, seven, stamp(now + timedelta(days=4)))]
    return envelope(name="claude-statusline", windows=windows,
                    evidence={item["id"]: "%064x" % int(item["used_percent"] * 7 + 1)
                              for item in windows})


def five_hour(quota):
    return next(item for item in quota["windows"] if item["id"] == "five_hour")


class ValidationTests(unittest.TestCase):
    def clean(self, value):
        return ingest_extras.clean_oauth_usage(value, datetime.now(timezone.utc), _check_window)

    def test_a_good_reading_keeps_its_own_fetch_time(self):
        reading = oauth_reading(age=timedelta(minutes=4))
        self.assertEqual(self.clean(reading)["fetched_at"],
                         datetime.fromisoformat(reading["fetched_at"]).isoformat())

    def test_anything_extra_is_refused(self):
        for extra in ({"token": SECRET}, {"credit": None}, {"available": True}, {"path": "x"}):
            with self.assertRaises(ValueError):
                self.clean(dict(oauth_reading(), **extra))

    def test_a_reading_with_no_number_or_no_time_is_refused(self):
        empty = oauth_reading()
        for item in empty["windows"]:
            item.update(used_percent=None, state="unavailable")
        for bad in (empty, {"windows": oauth_reading()["windows"]}, dict(oauth_reading(), windows=[])):
            with self.assertRaises(ValueError):
                self.clean(bad)

    def test_a_reading_from_the_future_or_too_long_ago_is_refused(self):
        for age in (timedelta(hours=-1), timedelta(days=8)):
            with self.assertRaises(ValueError):
                self.clean(oauth_reading(age=age))

    def test_only_a_claude_source_takes_one(self):
        from presto_ai_dash.sources.codex import CodexSource
        with self.assertRaises(ValueError):
            validate(envelope(oauth_usage=oauth_reading()), CodexSource)
        validate(envelope(oauth_usage=oauth_reading()), ClaudeSource)


class SourcePriorityTests(ReadingsBase):
    def collect(self):
        return self.quota(self.claude_source().collect())

    def test_delayed_oauth_report_cannot_replace_a_newer_fetch(self):
        for with_windows in (False, True):
            self.push(self.claude, envelope(oauth_usage=oauth_reading(five=70)))
            delayed = envelope(oauth_usage=oauth_reading(five=10, age=timedelta(hours=1)))
            if with_windows:
                delayed.update(status_line())
                delayed["oauth_usage"] = oauth_reading(five=10, age=timedelta(hours=1))
            self.push(self.claude, delayed)
            stored = self.storage.observation(self.claude["id"])["report"]
            self.assertEqual(stored["pushed_oauth_usage"]["windows"][0]["used_percent"], 70)

    def test_it_is_labelled_as_what_it_is_with_its_own_age(self):
        self.push(self.claude, envelope(oauth_usage=oauth_reading(age=timedelta(minutes=3))))
        quota = self.collect()
        five = five_hour(quota)
        self.assertEqual((five["used_percent"], five["source"]), (44.0, "oauth usage"))
        age = (now_utc() - datetime.fromisoformat(five["observed_at"])).total_seconds()
        self.assertAlmostEqual(age, 180, delta=5)  # fetched then, not received now
        self.assertNotIn("pushed_oauth_usage", quota)
        self.assertEqual(quota["oauth_usage"]["reported_by"], "laptop")
        self.assertIn("OAuth", quota["note"])

    def test_a_fresh_oauth_reading_beats_a_stale_cache(self):
        self.push(self.claude, envelope(usage_cache=usage_cache(), oauth_usage=oauth_reading()))
        five = five_hour(self.collect())
        self.assertEqual((five["used_percent"], five["source"]), (44.0, "oauth usage"))

    def test_a_newer_cache_beats_an_older_oauth_reading(self):
        self.push(self.claude, envelope(usage_cache=usage_cache(),
                                        oauth_usage=oauth_reading(age=timedelta(hours=1))))
        five = five_hour(self.collect())
        self.assertEqual((five["used_percent"], five["source"]), (31.0, "usage cache"))

    def test_on_a_tie_oauth_beats_the_cache(self):
        moment = datetime.now(timezone.utc) - timedelta(minutes=2)
        cache = usage_cache(fetched_at=stamp(moment))
        reading = oauth_reading(now=moment, age=timedelta(0))
        self.push(self.claude, envelope(usage_cache=cache, oauth_usage=reading))
        self.assertEqual(five_hour(self.collect())["source"], "oauth usage")

    def test_a_status_line_received_later_beats_it(self):
        self.push(self.claude, envelope(oauth_usage=oauth_reading()))
        self.push(self.claude, status_line())
        five = five_hour(self.collect())
        self.assertEqual((five["used_percent"], five["source"]), (10.0, "terminal export"))

    def test_on_a_tie_the_status_line_wins(self):
        moment = now_utc()
        self.push(self.claude, status_line(now=moment), now=moment)
        self.push(self.claude, envelope(oauth_usage=oauth_reading(now=moment, age=timedelta(0))),
                  now=moment)
        self.assertEqual(five_hour(self.collect())["source"], "terminal export")

    def test_a_newer_oauth_reading_beats_an_old_status_line(self):
        earlier = now_utc() - timedelta(minutes=30)
        self.push(self.claude, status_line(), now=earlier)
        self.push(self.claude, envelope(oauth_usage=oauth_reading()))
        self.assertEqual(five_hour(self.collect())["source"], "oauth usage")

    def test_a_status_line_push_does_not_erase_it(self):
        self.push(self.claude, envelope(oauth_usage=oauth_reading()))
        self.push(self.claude, status_line())
        self.assertIn("pushed_oauth_usage", self.storage.observation(self.claude["id"])["report"])

    def test_a_round_without_a_reading_leaves_the_last_one_standing(self):
        # What a failed request looks like from here: an envelope with no reading.
        self.push(self.claude, envelope(oauth_usage=oauth_reading()))
        self.push(self.claude, envelope(usage_cache=usage_cache()))
        five = five_hour(self.collect())
        self.assertEqual((five["used_percent"], five["source"]), (44.0, "oauth usage"))

    def test_each_window_is_judged_alone(self):
        reading = oauth_reading()
        reading["windows"][1].update(used_percent=None, state="unavailable")
        self.push(self.claude, envelope(usage_cache=usage_cache(), oauth_usage=reading))
        quota = self.collect()
        seven = next(item for item in quota["windows"] if item["id"] == "seven_day")
        self.assertEqual(five_hour(quota)["source"], "oauth usage")
        self.assertEqual(seven["source"], "usage cache")

    def test_the_old_reading_ages_and_is_not_called_fresh(self):
        self.push(self.claude, envelope(oauth_usage=oauth_reading(age=timedelta(hours=2))))
        quota = self.collect()
        age = (now_utc() - datetime.fromisoformat(five_hour(quota)["observed_at"])).total_seconds()
        self.assertGreater(age, quota["stale_after_seconds"])

    def test_the_labels_say_oauth_on_the_card_and_the_status_tab(self):
        self.assertEqual(dashboard._source_short("oauth usage"), "oauth")
        self.assertEqual(dashboard._source_short("usage cache"), "cache")
        self.assertEqual(dashboard._source_short("terminal export"), "export")
        self.push(self.claude, envelope(oauth_usage=oauth_reading()))
        line = claude_provenance(self.collect(), {}, now_utc())["quota"]
        self.assertTrue(line.startswith("Quota: oauth usage - last reading"), line)

    def test_with_nothing_pushed_the_report_has_no_trace_of_it(self):
        self.push(self.claude, envelope(usage_cache=usage_cache()))
        quota = self.collect()
        self.assertNotIn("oauth_usage", quota)
        self.assertEqual(five_hour(quota)["source"], "usage cache")
        self.assertNotIn("OAuth", quota["note"])


class ExportAgeTests(ReadingsBase):
    def test_no_reading_yet(self):
        self.assertEqual(export_age(self.storage, self.claude["id"]), "none")

    def test_the_age_of_the_older_window(self):
        moment = now_utc()
        self.push(self.claude, status_line(), now=moment - timedelta(seconds=90))
        self.assertEqual(export_age(self.storage, self.claude["id"], moment), "90")

    def test_a_redraw_does_not_look_fresh(self):
        moment = now_utc()
        body = status_line()
        self.push(self.claude, body, now=moment - timedelta(seconds=400))
        self.push(self.claude, body, now=moment - timedelta(seconds=1))
        self.assertEqual(export_age(self.storage, self.claude["id"], moment), "400")

    def test_a_missing_window_means_oauth_still_has_something_to_add(self):
        body = status_line()
        body["windows"][1].update(used_percent=None, resets_at=None, state="unavailable")
        self.push(self.claude, body)
        self.assertEqual(export_age(self.storage, self.claude["id"]), "none")

    def test_pushed_readings_do_not_count_as_a_status_line(self):
        self.push(self.claude, envelope(oauth_usage=oauth_reading(), usage_cache=usage_cache()))
        self.assertEqual(export_age(self.storage, self.claude["id"]), "none")

    def test_a_source_that_takes_no_oauth_reading_gets_no_answer(self):
        self.assertIsNone(export_age(self.storage, self.codex["id"]))
        self.assertIsNone(export_age(self.storage, "no-such-instance"))


class ConfigTests(FolderBase):
    def load(self, **changes):
        entry = {"service": "claude", "token_file": "t",
                 "settings_path": str(self.profile / "settings.json")}
        entry.update(changes)
        return collector.load_config(self.write_config(accounts=[entry]))["accounts"][0]

    def test_it_is_off_unless_asked_for(self):
        self.assertIs(self.load()["oauth_usage"], False)

    def test_on_means_five_minutes_and_no_dry_run(self):
        account = self.load(oauth_usage=True)
        self.assertEqual(account["oauth_usage_interval_seconds"], 300)
        self.assertIs(account["oauth_usage_dry_run"], False)

    def test_the_interval_has_a_floor_and_a_ceiling(self):
        self.assertEqual(self.load(oauth_usage=True, oauth_usage_interval_seconds=600)
                         ["oauth_usage_interval_seconds"], 600)
        for value in (299, 0, -1, 86401):
            with self.assertRaises(collector.ConfigError):
                self.load(oauth_usage=True, oauth_usage_interval_seconds=value)

    def test_it_needs_the_activity_reader(self):
        with self.assertRaises(collector.ConfigError):
            self.load(oauth_usage=True, activity=False)

    def test_a_truthy_string_does_not_turn_it_on(self):
        with self.assertRaises(collector.ConfigError):
            self.load(oauth_usage="true")

    def test_codex_has_no_such_setting(self):
        with self.assertRaises(collector.ConfigError):
            collector.load_config(self.write_config(accounts=[
                {"service": "codex", "token_file": "t", "oauth_usage": True}]))


class WatchBase(PushBase):
    def setUp(self):
        super().setUp()
        block_real_network(self)
        self.state_dir = Path(self.temp.name) / "state"
        self.state_dir.mkdir()
        self.reader = collector._embedded("claude_oauth_usage")
        self.fake = Opener()
        # Rounds here are milliseconds apart, so the daemon's two-second throttle stands aside.
        for target, name, value in ((collector, "_state_directory", lambda **kw: str(self.state_dir)),
                                    (self.reader, "default_opener", lambda: self.fake),
                                    (api_module._IngestRateLimiter, "check",
                                     lambda limiter, key, now: True)):
            patcher = patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        environment = patch.dict(os.environ)
        environment.start()
        self.addCleanup(environment.stop)
        for name in self.reader.OTHER_ROUTES:
            os.environ.pop(name, None)
        self.write_credentials()
        self.write_cache()
        self.assertTrue(collector._oauth_poller(self.oauth_config()["accounts"][0]).attest(
            datetime.now(timezone.utc).timestamp()))

    def write_credentials(self, hours=6):
        expires = (datetime.now(timezone.utc).timestamp() + hours * 3600) * 1000
        (self.profile / ".credentials.json").write_text(json.dumps({"claudeAiOauth": {
            "accessToken": SECRET, "refreshToken": REFRESH, "expiresAt": expires}}), encoding="utf-8")

    def oauth_entry(self, **changes):
        entry = {"service": "claude", "token_file": "claude.token",
                 "settings_path": str(self.profile / "settings.json"), "oauth_usage": True}
        entry.update(changes)
        return entry

    def oauth_config(self, **changes):
        path = self.write_config(accounts=[self.oauth_entry(**changes)], url=self.url)
        return collector.load_config(path)

    def rounds(self, config, count, times=None, account=None):
        account = account or collector.Account(config["accounts"][0])
        out = io.StringIO()
        for index in range(count):
            moment = times[index] if times else None
            collector.run_round(config, [account], now=moment, out=out, opener=self.opener,
                                verbose=True)
        return account, out.getvalue()

    def state_files(self):
        return [path for path in self.state_dir.iterdir() if path.name.startswith("claude-oauth-")
                and path.suffix == ".json"]

    def quota(self):
        return self.claude_source().collect().metrics["quota"]


class WatchTests(WatchBase):
    def test_the_first_round_only_asks_the_daemon_and_the_second_may_request(self):
        self.write_session(session_line(seconds_ago=20))
        config = self.oauth_config()
        account, output = self.rounds(config, 1)
        self.assertEqual(len(self.fake.requests), 0)
        self.assertIn("oauth daemon_not_ready", output)
        self.rounds(config, 1, account=account)
        self.assertEqual(len(self.fake.requests), 1)
        five = five_hour(self.quota())
        self.assertEqual((five["used_percent"], five["source"]), (34.0, "oauth usage"))

    def test_the_reading_is_sent_once(self):
        self.write_session(session_line(seconds_ago=20))
        account, _ = self.rounds(self.oauth_config(), 3)
        self.assertEqual(len(self.fake.requests), 1)
        self.assertIsNone(account.oauth_pending)

    def test_a_cache_that_is_older_loses_and_keeps_its_own_label_in_the_report(self):
        self.write_session(session_line(seconds_ago=20))
        self.write_cache()
        self.rounds(self.oauth_config(), 2)
        quota = self.quota()
        self.assertEqual(five_hour(quota)["source"], "oauth usage")
        self.assertTrue(quota["usage_cache"]["available"])

    def test_idle_claude_makes_no_request_while_reports_continue(self):
        self.write_session(session_line(seconds_ago=3600))
        self.write_cache()
        _, output = self.rounds(self.oauth_config(), 3)
        self.assertEqual(self.fake.requests, [])
        self.assertIn("oauth idle", output)
        self.assertEqual(five_hour(self.quota())["source"], "usage cache")

    def test_a_fresh_status_line_at_the_daemon_stops_the_request(self):
        self.write_session(session_line(seconds_ago=20))
        moment = now_utc()
        windows = [{"id": key, "label": label, "period_minutes": minutes, "used_percent": 5.0,
                    "resets_at": stamp(moment + timedelta(hours=2)), "state": "available"}
                   for key, label, minutes in collector.WINDOWS]
        from presto_ai_dash.ingest import SCHEMA_NAME, ingest
        ingest(self.storage, self.claude["id"], {
            "schema": SCHEMA_NAME, "windows": windows,
            "collector": {"name": "claude-statusline", "version": "2", "host": "local"},
            "evidence": {item["id"]: "a" * 64 for item in windows}})
        _, output = self.rounds(self.oauth_config(), 3)
        self.assertEqual(self.fake.requests, [])
        self.assertIn("oauth status_line_fresh", output)

    def test_a_failed_request_keeps_the_last_reading_and_other_reports_going(self):
        self.write_session(session_line(seconds_ago=20, request_id="one"))
        self.write_cache()
        config = self.oauth_config()
        start = datetime.now(timezone.utc).timestamp()
        account, _ = self.rounds(config, 2, times=[start, start + 1])
        self.assertEqual(five_hour(self.quota())["used_percent"], 34.0)
        for failure in (http_error(429, "60"), http_error(500), http_error(401)):
            self.setUpFailure(failure, account, config, start)
            start += 10
        five = five_hour(self.quota())
        self.assertEqual((five["used_percent"], five["source"]), (34.0, "oauth usage"))
        self.assertTrue(self.quota()["local_activity"]["available"])

    def setUpFailure(self, failure, account, config, start):
        """Clear the way for one more request, and have it fail."""
        self.fake.replies = [failure]
        path = self.state_files()[0]
        state = json.loads(path.read_text(encoding="utf-8"))
        state.update(last_attempt_at=start - 3600, backoff_until=0)
        path.write_text(json.dumps(state), encoding="utf-8")
        self.write_session(session_line(seconds_ago=1, request_id="again"))
        self.rounds(config, 1, account=account)

    def test_a_reading_that_could_not_be_delivered_is_kept_for_one_interval(self):
        self.write_session(session_line(seconds_ago=20))
        config = self.oauth_config()
        start = datetime.now(timezone.utc).timestamp()
        account, _ = self.rounds(config, 2, times=[start, start + 1])
        account.oauth_pending = (json.loads(json.dumps(
            {"fetched_at": stamp(now_utc()), "windows": [window("five_hour", "5h", 300, 7.0, None)]})),
            start)
        with patch.object(collector, "send", side_effect=collector.Problem("down")):
            collector.run_round(config, [account], now=start + 2, out=io.StringIO(), opener=self.opener)
        self.assertIsNotNone(account.oauth_pending)
        account.skip_until = 0
        pieces = {}
        account._oauth_step(pieces, [], None, None, start + 400)
        self.assertIsNone(account.oauth_pending)
        self.assertEqual(pieces, {})

    def test_a_daemon_too_old_to_store_it_costs_no_request(self):
        self.write_session(session_line(seconds_ago=20))
        with patch.object(collector, "send", return_value=None):
            self.rounds(self.oauth_config(), 3)
        self.assertEqual(self.fake.requests, [])

    def test_a_stop_is_said_once_and_carries_no_secret(self):
        self.write_session(session_line(seconds_ago=20))
        self.fake.replies = [http_error(401)]
        _, output = self.rounds(self.oauth_config(), 4)
        self.assertEqual(len(self.fake.requests), 1)
        self.assertEqual(output.count("OAuth usage requests are stopped (401)"), 1)
        self.assertNotIn(SECRET, output)

    def test_no_credential_reaches_the_daemon_the_log_or_the_state(self):
        self.write_session(session_line(seconds_ago=20))
        _, output = self.rounds(self.oauth_config(), 3)
        stored = json.dumps(self.storage.observation(self.claude["id"]))
        files = "".join(path.read_text(encoding="utf-8") for path in self.state_dir.iterdir())
        for text in (stored, output, files):
            for secret in (SECRET, REFRESH, "claudeAiOauth", "accessToken", "Bearer"):
                self.assertNotIn(secret, text)

    def test_only_the_two_window_fields_reach_the_daemon(self):
        self.write_session(session_line(seconds_ago=20))
        self.fake.replies = [Reply(dict(BODY, extra_usage={"note": "SENTINEL-EXTRA"}))]
        self.rounds(self.oauth_config(), 2)
        report = self.storage.observation(self.claude["id"])["report"]
        self.assertEqual(set(report["pushed_oauth_usage"]), {"fetched_at", "windows", "received_at"})
        self.assertNotIn("SENTINEL-EXTRA", json.dumps(report))

    def test_dry_run_in_the_watch_sends_nothing_and_opens_nothing(self):
        self.write_session(session_line(seconds_ago=20))
        with patch.object(self.reader, "read_access_token", side_effect=AssertionError("opened")):
            _, output = self.rounds(self.oauth_config(oauth_usage_dry_run=True), 3)
        self.assertEqual(self.fake.requests, [])
        self.assertIn("oauth dry_run", output)
        self.assertNotIn("oauth_usage", self.storage.observation(self.claude["id"])["report"])


class WhenOffTests(WatchBase):
    def test_nothing_is_read_sent_or_written(self):
        self.write_session(session_line(seconds_ago=20))
        self.write_cache()
        config = self.oauth_config(oauth_usage=False)
        sent = []
        real = collector.send

        def spy(url, token, envelope, opener=None):
            sent.append(envelope)
            return real(url, token, envelope, opener)

        with patch.object(self.reader, "read_access_token", side_effect=AssertionError("opened")), \
                patch.object(self.reader, "fetch", side_effect=AssertionError("sent")), \
                patch.object(collector, "send", spy):
            account, output = self.rounds(config, 3)
        self.assertEqual(self.fake.requests, [])
        self.assertEqual(self.state_files(), [])
        self.assertIsNone(account.oauth_poller)
        self.assertNotIn("oauth", output.lower())
        self.assertEqual(set(sent[0]), {"schema", "collector", "client_time", "activity",
                                        "usage_cache"})
        self.assertEqual(five_hour(self.quota())["source"], "usage cache")

    def test_an_existing_collector_json_loads_as_it_did(self):
        path = self.write_config(accounts=[{"service": "claude", "token_file": "claude.token",
                                            "settings_path": str(self.profile / "settings.json")}],
                                 url=self.url)
        account = collector.load_config(path)["accounts"][0]
        self.assertEqual(set(account), {"service", "token_file", "name", "settings_path", "activity",
                                        "usage_cache", "usage_cache_path", "oauth_usage"})

    def test_a_status_line_run_loads_no_reader_and_writes_no_oauth_state(self):
        before = set(self.state_dir.iterdir())
        with patch.dict(collector._EMBEDDED_LOADED, clear=True), \
                patch("sys.stdin", io.TextIOWrapper(io.BytesIO(b"{}"))), \
                patch("sys.stdout", io.StringIO()):
            collector.main(["claude-statusline", "--url", "http://127.0.0.1:9/x",
                            "--token-file", str(self.folder / "absent")],
                           state_path=str(self.folder / "state.json"))
            self.assertEqual(collector._EMBEDDED_LOADED, {})
        self.assertEqual(set(self.state_dir.iterdir()), before)


class CommandTests(WatchBase):
    def run_command(self, action, **changes):
        path = self.write_config(accounts=[self.oauth_entry(**changes)], url=self.url)
        out = io.StringIO()
        code = collector.oauth_command(action, path, out)
        return code, out.getvalue()

    def test_dry_run_says_yes_when_a_request_would_go_out(self):
        self.write_session(session_line(seconds_ago=20))
        with patch.object(self.reader, "read_access_token", side_effect=AssertionError("opened")), \
                patch.object(self.reader, "fetch", side_effect=AssertionError("sent")):
            code, output = self.run_command("dry-run")
        self.assertEqual(code, 0)
        self.assertIn("would request now: YES", output)
        self.assertIn("credentials were not opened and nothing was sent", output)
        self.assertEqual(self.state_files(), [])

    def test_dry_run_works_with_the_experiment_off_and_says_so(self):
        self.write_session(session_line(seconds_ago=3600))
        code, output = self.run_command("dry-run", oauth_usage=False)
        self.assertEqual(code, 0)
        self.assertIn("no (oauth_usage is off)", output)
        self.assertIn("would request now: no", output)
        self.assertIn("reason: idle", output)

    def test_status_shows_times_status_and_source_and_no_secret(self):
        self.write_session(session_line(seconds_ago=20))
        self.write_cache()
        self.rounds(self.oauth_config(), 2)
        code, output = self.run_command("status")
        self.assertEqual(code, 0)
        self.assertIn("last response: status 200, ok", output)
        self.assertIn("selected source: oauth", output)
        self.assertRegex(output, r"status 200 ok")
        for secret in (SECRET, REFRESH, "Bearer"):
            self.assertNotIn(secret, output)

    def test_stop_then_reenable(self):
        self.write_session(session_line(seconds_ago=20))
        self.assertEqual(self.run_command("stop")[0], 0)
        self.rounds(self.oauth_config(), 3)
        self.assertEqual(self.fake.requests, [])
        self.assertIn("stopped: stopped by hand", self.run_command("status")[1])
        self.assertEqual(self.run_command("reenable")[0], 0)
        self.rounds(self.oauth_config(), 2)
        self.assertEqual(len(self.fake.requests), 1)

    def test_hook_config_is_valid_and_names_this_profile(self):
        code, output = self.run_command("hook-config")
        self.assertEqual(code, 0)
        block = json.loads(output.split("\n", 1)[1])
        self.assertIn("UserPromptSubmit", block["hooks"])
        self.assertIn(str(self.profile).replace("\\", "/"), output)

    def test_a_missing_configuration_is_an_answer_not_a_crash(self):
        out = io.StringIO()
        self.assertEqual(collector.oauth_command("status", str(self.folder / "absent.json"), out), 2)


class AloneTests(WatchBase):
    def test_the_commands_work_from_the_collector_file_alone(self):
        self.write_session(session_line(seconds_ago=20))
        lonely = Path(self.temp.name) / "remote"
        lonely.mkdir()
        copy = lonely / "presto_collector.py"
        copy.write_bytes(Path(collector.__file__).read_bytes())
        path = self.write_config(accounts=[self.oauth_entry()], url=self.url)
        environment = {key: value for key, value in os.environ.items()
                       if key.upper() != "PYTHONPATH"}
        environment["LOCALAPPDATA"] = environment["XDG_STATE_HOME"] = str(self.state_dir)
        for action, expected in (("dry-run", "would request now: YES"), ("status", "last request:")):
            result = subprocess.run([sys.executable, "-I", str(copy), "claude-oauth", action,
                                     "--config", path], cwd=str(lonely), env=environment,
                                    capture_output=True, text=True, timeout=60)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(expected, result.stdout)


if __name__ == "__main__":
    unittest.main()
