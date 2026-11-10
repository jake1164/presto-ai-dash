"""Reading what the Presto Claude mod wrote, and sending it as quota.

The file format and the reader are checked on their own, then through the
collector's account and its watch round against a throwaway daemon. No real
Claude profile is read; every folder is a temporary one.
"""

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sys
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from presto_ai_dash import api
from presto_ai_dash.collectors import claude_mod, presto_collector as collector
from presto_ai_dash.ingest import validate
from presto_ai_dash.sources.claude import ClaudeSource
from ingest.test_collector_watch import FolderBase, PushBase

NOW = time.time()


def when(seconds_ago):
    return datetime.fromtimestamp(NOW - seconds_ago, timezone.utc)


def iso(moment):
    return moment.isoformat().replace("+00:00", "Z")


def limits(five=9.5, seven=19.0, base=None):
    base = base or when(0)
    return [{"kind": "five_hour", "percent_used": five, "resets_at": iso(base + timedelta(hours=3))},
            {"kind": "seven_day", "percent_used": seven, "resets_at": iso(base + timedelta(days=3))}]


def document(instance="aaa", seq=1, observed=None, rate_limits=None, **changes):
    body = {"schema": claude_mod.MOD_SCHEMA, "mod_version": 1, "instance": instance, "seq": seq,
            "observed_at": None if observed is None else iso(observed),
            "rate_limits": [] if observed is None else (rate_limits or limits(base=observed))}
    body.update(changes)
    return body


def write_file(folder, instance="aaa", **kwargs):
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / ("session-%s.json" % instance)
    path.write_text(json.dumps(document(instance=instance, **kwargs)) + "\n", encoding="utf-8")
    return path


class ParseTests(unittest.TestCase):
    def parse(self, **changes):
        return claude_mod.parse_file(json.dumps(document(observed=when(30), **changes)))

    def test_a_reading_is_understood(self):
        item = self.parse()
        self.assertEqual((item["instance"], item["seq"]), ("aaa", 1))
        self.assertEqual([limit["kind"] for limit in item["limits"]], ["five_hour", "seven_day"])
        self.assertEqual(item["limits"][0]["resets_at"].tzinfo, timezone.utc)

    def test_the_marker_a_session_writes_when_it_starts_holds_no_reading(self):
        item = claude_mod.parse_file(json.dumps(document()))
        self.assertIsNone(item["observed_at"])
        self.assertEqual(item["limits"], [])

    def test_the_millisecond_times_the_mod_writes_are_understood(self):
        text = json.dumps(document(observed=when(30))).replace("Z\"", ".123Z\"")
        self.assertIsNotNone(claude_mod.parse_file(text)["observed_at"])

    def test_every_malformed_file_is_refused(self):
        good = document(observed=when(30))
        cases = {
            "not json": "{not json",
            "a list": "[]",
            "an unknown key": json.dumps(dict(good, cwd="/private")),
            "a missing key": json.dumps({key: value for key, value in good.items() if key != "seq"}),
            "another schema": json.dumps(dict(good, schema="other/1")),
            "a bad session id": json.dumps(dict(good, instance="../etc")),
            "a zero sequence": json.dumps(dict(good, seq=0)),
            "a boolean sequence": json.dumps(dict(good, seq=True)),
            "a time without an offset": json.dumps(dict(good, observed_at="2026-11-10T12:00:00")),
            "a reading without windows": json.dumps(dict(good, rate_limits=[])),
            "a marker with windows": json.dumps(dict(good, observed_at=None)),
            "an unknown window": json.dumps(dict(good, rate_limits=[
                dict(good["rate_limits"][0], kind="spend_limit")])),
            "a repeated window": json.dumps(dict(good, rate_limits=[good["rate_limits"][0]] * 2)),
            "a percent that is text": json.dumps(dict(good, rate_limits=[
                dict(good["rate_limits"][0], percent_used="9")])),
            "a negative percent": json.dumps(dict(good, rate_limits=[
                dict(good["rate_limits"][0], percent_used=-1)])),
            "an extra window field": json.dumps(dict(good, rate_limits=[
                dict(good["rate_limits"][0], note="x")])),
        }
        for label, text in cases.items():
            with self.assertRaises(claude_mod.ModFileError, msg=label):
                claude_mod.parse_file(text)

    def test_an_error_never_quotes_the_file(self):
        try:
            claude_mod.parse_file(json.dumps(document(observed=when(30), instance="SECRET/../x")))
        except claude_mod.ModFileError as error:
            self.assertNotIn("SECRET", str(error))


class ReaderTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name) / "claude-quota"
        self.reader = claude_mod.ModReader(str(self.folder))
        self.patch = patch.object(claude_mod, "RETRY_SECONDS", 0)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def refresh(self):
        return self.reader.refresh(NOW)

    def test_a_folder_that_is_not_there_means_the_mod_is_not_installed(self):
        result = self.refresh()
        self.assertEqual(result["status"], "not installed")
        self.assertIsNone(result["reading"])

    def test_an_empty_folder_has_no_sessions(self):
        self.folder.mkdir()
        self.assertEqual(self.refresh()["status"], "no sessions yet")

    def test_a_marker_means_the_mod_is_loaded_but_has_no_reading(self):
        write_file(self.folder, seq=1)
        result = self.refresh()
        self.assertEqual(result["status"], "loaded, no reading yet")
        self.assertIsNone(result["reading"])

    def test_a_new_reading_is_reported_once(self):
        write_file(self.folder, seq=2, observed=when(20))
        first = self.refresh()
        self.assertEqual(first["status"], "reading")
        self.assertEqual(first["reading"]["seq"], 2)
        self.reader.acknowledge()
        self.assertEqual(self.refresh()["status"], "waiting for a response")

    def test_a_reading_that_was_not_delivered_is_offered_again(self):
        write_file(self.folder, seq=2, observed=when(20))
        self.assertEqual(self.refresh()["status"], "reading")
        again = self.refresh()                       # the send failed, nobody acknowledged
        self.assertEqual(again["status"], "reading")
        self.assertEqual(again["reading"]["seq"], 2)
        self.reader.acknowledge()
        self.assertIsNone(self.refresh()["reading"])

    def test_a_newer_reading_replaces_one_that_was_never_delivered(self):
        write_file(self.folder, seq=2, observed=when(60))
        self.refresh()
        write_file(self.folder, seq=3, observed=when(10), rate_limits=limits(base=when(60)))
        self.assertEqual(self.refresh()["reading"]["seq"], 3)

    def test_a_file_that_is_only_touched_is_not_a_new_reading(self):
        path = write_file(self.folder, seq=2, observed=when(20))
        self.refresh()
        self.reader.acknowledge()
        os.utime(path, (NOW + 5, NOW + 5))
        path.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")   # rewritten, same event
        self.assertIsNone(self.refresh()["reading"])

    def test_a_higher_sequence_is_a_new_reading_even_with_the_same_figures(self):
        write_file(self.folder, seq=2, observed=when(120))
        self.refresh()
        write_file(self.folder, seq=3, observed=when(10), rate_limits=limits(base=when(120)))
        result = self.refresh()
        self.assertEqual(result["status"], "reading")
        self.assertEqual(result["reading"]["seq"], 3)

    def test_the_newest_reading_across_sessions_wins(self):
        write_file(self.folder, instance="old", seq=5, observed=when(300))
        write_file(self.folder, instance="new", seq=2, observed=when(30))
        self.assertEqual(self.refresh()["reading"]["instance"], "new")

    def test_a_restarted_collector_sends_the_latest_reading_again(self):
        write_file(self.folder, seq=2, observed=when(20))
        self.refresh()
        again = claude_mod.ModReader(str(self.folder)).refresh(NOW)
        self.assertEqual(again["status"], "reading")

    def test_a_reading_too_old_to_trust_is_not_forwarded(self):
        write_file(self.folder, seq=2, observed=when(claude_mod.MAX_READING_AGE_SECONDS + 60))
        result = self.refresh()
        self.assertIsNone(result["reading"])
        self.assertEqual(result["status"], "waiting for a response")

    def test_a_reading_from_the_future_is_not_forwarded(self):
        write_file(self.folder, seq=2, observed=when(-3600))
        self.assertIsNone(self.refresh()["reading"])

    def test_a_half_written_file_is_ignored_and_the_last_reading_stands(self):
        path = write_file(self.folder, seq=2, observed=when(20))
        self.assertEqual(self.refresh()["status"], "reading")
        path.write_text('{"schema": "presto-ai-dash.claude-mod/1", "mod_', encoding="utf-8")
        result = self.refresh()
        self.assertIsNone(result["reading"])
        self.assertTrue(any("could not be read" in note for note in result["notes"]))

    def test_a_file_that_is_whole_on_the_second_try_is_read(self):
        path = write_file(self.folder, seq=2, observed=when(20))
        text = path.read_text(encoding="utf-8")
        path.write_text(text[:30], encoding="utf-8")
        calls = []

        def finish_the_write(_seconds):
            calls.append(1)
            path.write_text(text, encoding="utf-8")

        with patch.object(claude_mod.time, "sleep", finish_the_write):
            result = self.refresh()
        self.assertEqual(calls, [1])
        self.assertEqual(result["status"], "reading")

    def test_other_files_and_folders_are_left_alone(self):
        self.folder.mkdir()
        (self.folder / "notes.txt").write_text("hello")
        (self.folder / "session-x.json.bak").write_text("{}")
        (self.folder / "session-sub.json").mkdir()
        self.assertEqual(self.refresh()["status"], "no sessions yet")
        self.assertTrue((self.folder / "notes.txt").exists())

    def test_a_link_is_not_followed(self):
        self.folder.mkdir()
        target = Path(self.temp.name) / "elsewhere.json"
        target.write_text(json.dumps(document(observed=when(20))))
        try:
            os.symlink(target, self.folder / "session-link.json")
        except (OSError, NotImplementedError):
            self.skipTest("symbolic links cannot be made here")
        self.assertEqual(self.refresh()["status"], "no sessions yet")

    def test_a_file_too_large_to_be_a_mod_file_is_ignored(self):
        self.folder.mkdir()
        (self.folder / "session-big.json").write_text(" " * (claude_mod.MAX_FILE_BYTES + 1))
        self.assertEqual(self.refresh()["status"], "unreadable files")

    def test_a_session_silent_for_a_day_is_cleaned_up(self):
        path = write_file(self.folder, seq=2, observed=when(20))
        old = NOW - claude_mod.PRUNE_AFTER_SECONDS - 60
        os.utime(path, (old, old))
        self.assertEqual(self.refresh()["status"], "no sessions yet")
        self.assertFalse(path.exists())


class AccountTests(FolderBase):
    def mod_account(self, **changes):
        settings = dict(activity=False, usage_cache=False, mod=True,
                        mod_folder=str(self.root / "claude-quota"))
        settings.update(changes)
        return self.account(**settings)

    def envelope(self, account):
        return account.envelope("laptop", NOW)

    def test_a_new_reading_becomes_windows_dated_by_the_event(self):
        write_file(self.root / "claude-quota", seq=2, observed=when(45))
        envelope, notes, warning = self.envelope(self.mod_account())
        self.assertIsNone(warning)
        self.assertEqual([window["used_percent"] for window in envelope["windows"]], [9.5, 19.0])
        self.assertEqual(datetime.fromisoformat(envelope["client_time"]), when(45))
        self.assertTrue(all(window["state"] == "available" for window in envelope["windows"]))
        self.assertIn("new reading", notes[0])
        validate(envelope, ClaudeSource)

    def test_every_new_event_is_new_evidence_and_a_repeat_is_not_sent(self):
        folder = self.root / "claude-quota"
        account = self.mod_account()
        write_file(folder, seq=2, observed=when(45))
        first = self.envelope(account)[0]
        account.mod_reader.acknowledge()               # run_round does this once the send worked
        self.assertNotIn("windows", self.envelope(account)[0])
        write_file(folder, seq=3, observed=when(5), rate_limits=limits(base=when(45)))
        third = self.envelope(account)[0]
        self.assertNotEqual(first["evidence"], third["evidence"])     # same figures, new response
        self.assertNotEqual(first["progress"], third["progress"])

    def test_nothing_private_is_sent(self):
        write_file(self.root / "claude-quota", instance="SENTINEL-SESSION-ID", seq=2,
                   observed=when(45))
        text = json.dumps(self.envelope(self.mod_account())[0])
        self.assertNotIn("SENTINEL-SESSION-ID", text)
        self.assertNotIn(str(self.root), text)

    def test_no_reading_sends_a_heartbeat_and_says_why(self):
        envelope, notes, warning = self.envelope(self.mod_account())
        self.assertNotIn("windows", envelope)
        self.assertIsNone(warning)
        self.assertIn("not installed", notes[0])

    def test_a_window_the_mod_did_not_report_is_sent_as_unavailable(self):
        write_file(self.root / "claude-quota", seq=2, observed=when(45),
                   rate_limits=limits(base=when(45))[:1])
        windows = {window["id"]: window for window in self.envelope(self.mod_account())[0]["windows"]}
        self.assertEqual(windows["seven_day"]["state"], "unavailable")
        self.assertIsNone(windows["seven_day"]["used_percent"])

    def test_the_mod_can_be_switched_off(self):
        write_file(self.root / "claude-quota", seq=2, observed=when(45))
        envelope = self.account(activity=True, usage_cache=False, mod=False,
                                mod_folder=str(self.root / "claude-quota")).envelope("laptop", NOW)[0]
        self.assertNotIn("windows", envelope)


class ConfigTests(FolderBase):
    def load(self, entry):
        return collector.load_config(self.write_config(
            accounts=[dict({"service": "claude", "token_file": "t"}, **entry)]))["accounts"][0]

    def test_the_mod_is_on_by_default_beside_the_configuration_file(self):
        account = self.load({})
        self.assertTrue(account["mod"])
        self.assertEqual(Path(account["mod_folder"]), self.folder / "claude-quota")

    def test_a_folder_can_be_named(self):
        account = self.load({"mod_folder": "other"})
        self.assertEqual(Path(account["mod_folder"]), self.folder / "other")

    def test_the_mod_alone_is_enough_for_an_account(self):
        self.assertTrue(self.load({"activity": False, "usage_cache": False})["mod"])

    def test_an_account_that_reads_nothing_is_refused(self):
        with self.assertRaises(collector.ConfigError) as caught:
            self.load({"activity": False, "usage_cache": False, "mod": False})
        self.assertIn("mod", str(caught.exception))

    def test_the_new_settings_are_checked(self):
        for bad in ({"mod": "yes"}, {"mod_folder": 5}):
            with self.assertRaises(collector.ConfigError):
                self.load(bad)


class EndToEndTests(PushBase):
    def mod_folder(self):
        return self.folder / "claude-quota"

    def test_a_mod_reading_reaches_the_daemon_dated_by_the_event(self):
        write_file(self.mod_folder(), seq=2, observed=when(90))
        code, output = self.run_watch(self.config_for("claude"))
        self.assertEqual(code, 0, output)
        quota = self.claude_source().collect().metrics["quota"]
        five = next(window for window in quota["windows"] if window["id"] == "five_hour")
        self.assertEqual(five["used_percent"], 9.5)
        self.assertLess(abs(datetime.fromisoformat(five["observed_at"]) - when(90)).total_seconds(), 2)

    def test_a_second_round_with_no_new_response_does_not_freshen_the_quota(self):
        write_file(self.mod_folder(), seq=2, observed=when(600))
        config = self.config_for("claude")
        self.assertEqual(self.run_watch(config)[0], 0)
        before = self.claude_source().collect().metrics["quota"]["windows"][0]["observed_at"]
        with patch.object(api._IngestRateLimiter, "INTERVAL_SECONDS", 0.0):   # two rounds, one second apart
            self.assertEqual(self.run_watch(config)[0], 0)
        stored = self.claude_source().collect().metrics["quota"]
        self.assertEqual(stored["windows"][0]["observed_at"], before)
        self.assertIsNotNone(stored["collector_seen_at"])

    def test_a_reading_too_old_to_trust_never_reaches_the_daemon(self):
        write_file(self.mod_folder(), seq=2,
                   observed=when(claude_mod.MAX_READING_AGE_SECONDS + 600))
        self.assertEqual(self.run_watch(self.config_for("claude"))[0], 0)
        quota = self.claude_source().collect().metrics["quota"]
        self.assertTrue(all(window["used_percent"] is None for window in quota["windows"]))


if __name__ == "__main__":
    unittest.main()
