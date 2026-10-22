"""Phase 5d: the Claude Code usage cache reader (claude_usage_cache.py).

Everything here uses synthetic fixture files in a temp directory. No test ever
reads ~/.claude.json or ~/.claude. SENTINEL strings stand for the personal
data the real file also holds (email, organisation, project names, a token);
the reader's result and its log records must never contain them.
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path
import json
import logging
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from presto_ai_dash.collectors import claude_usage_cache
from presto_ai_dash.collectors.claude_usage_cache import (UsageCacheReader,
                                                           candidate_paths)

SENTINEL_EMAIL = "SENTINEL-ACCOUNT-EMAIL"
SENTINEL_ORG = "SENTINEL-ORGANISATION"
SENTINEL_UUID = "SENTINEL-ACCOUNT-UUID"
SENTINEL_PROJECT = "SENTINEL-PRIVATE-PROJECT"
SENTINEL_TOKEN = "SENTINEL-OAUTH-TOKEN"
SENTINELS = (SENTINEL_EMAIL, SENTINEL_ORG, SENTINEL_UUID, SENTINEL_PROJECT,
              SENTINEL_TOKEN)

NOW = datetime(2026, 10, 20, 8, tzinfo=timezone.utc)
LOGGER_NAME = "presto_ai_dash.claude_usage_cache"


def iso(moment):
    return moment.isoformat()


def milliseconds(moment):
    return int(moment.timestamp() * 1000)


def cache_document(fetched=NOW - timedelta(hours=2), five=12.0, weekly=66.0,
                   account=SENTINEL_UUID, credit_key="iguana_necktie",
                   credit=True, extra=None):
    """A cache file shaped like the real one, with the private parts included.

    The private parts are the ones the real file also holds: the account id,
    the email, the organisation, the project history and the MCP servers.
    """
    utilization = {}
    if five is not None:
        utilization["five_hour"] = {
            "utilization": five,
            "resets_at": iso(NOW + timedelta(hours=3)),
            "limit_dollars": None, "used_dollars": None,
            "remaining_dollars": None, "locked_reason": None}
    if weekly is not None:
        utilization["seven_day"] = {
            "utilization": weekly,
            "resets_at": iso(NOW + timedelta(days=3)),
            "limit_dollars": None, "used_dollars": None,
            "remaining_dollars": None, "locked_reason": None}
    if credit:
        utilization[credit_key] = {"utilization": 0,
                                   "resets_at": iso(NOW + timedelta(days=16)),
                                   "limit_dollars": 100, "used_dollars": 0,
                                   "remaining_dollars": 100}
    if extra:
        utilization.update(extra)
    document = {
        "cachedUsageUtilization": {
            "accountUuid": account,
            "fetchedAtMs": milliseconds(fetched),
            "utilization": utilization},
        "oauthAccount": {"emailAddress": SENTINEL_EMAIL,
                         "organizationName": SENTINEL_ORG,
                         "accessToken": SENTINEL_TOKEN},
        "projects": {"/home/you/" + SENTINEL_PROJECT: {"history": [SENTINEL_EMAIL]}},
        "mcpServers": {"private": {"command": SENTINEL_TOKEN}}}
    return document


class CacheBase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / ".claude.json"

    def write(self, document=None, raw=None, path=None):
        target = Path(path) if path else self.path
        if raw is None:
            raw = json.dumps(document if document is not None
                             else cache_document()).encode("utf-8")
        before = target.stat().st_mtime_ns if target.exists() else None
        target.write_bytes(raw)
        # The reader re-reads when the modification time moves, and a file
        # system (Linux's, for one) may not move its clock between two quick
        # writes, so a rewrite always gets a later time.
        if before is not None and target.stat().st_mtime_ns <= before:
            later = before + 1_000_000_000
            os.utime(str(target), ns=(later, later))
        return target

    def write_stamped(self, document, mtime):
        """Write a fixture with its own modification time.

        The reader re-reads only when the size or the modification time
        changed, and a file system may not move the clock between two quick
        writes, so the time is set here.
        """
        target = self.write(document)
        os.utime(str(target), (mtime, mtime))
        return target

    def reader(self, candidates=None, clock=None):
        return UsageCacheReader(candidates or [str(self.path)], clock=clock)

    def refresh(self, reader=None, now=NOW):
        reader = reader or self.reader()
        result = reader.refresh(now)
        self.assertIsInstance(result, dict)
        return result


class ReadResultTests(CacheBase):
    def test_the_reading_holds_the_two_windows_the_fetch_time_and_the_credit(self):
        self.write()
        result = self.refresh()
        self.assertIs(result["available"], True)
        self.assertIsNone(result["reason"])
        self.assertEqual(result["path"], str(self.path))
        self.assertEqual(result["fetched_at"], iso(NOW - timedelta(hours=2)))
        self.assertEqual([window["id"] for window in result["windows"]],
                         ["five_hour", "seven_day"])
        self.assertEqual(result["windows"][0]["used_percent"], 12.0)
        self.assertEqual(result["windows"][0]["label"], "5h")
        self.assertEqual(result["windows"][0]["period_minutes"], 300)
        self.assertEqual(result["windows"][0]["state"], "available")
        self.assertEqual(result["windows"][1]["used_percent"], 66.0)
        self.assertEqual(result["windows"][1]["label"], "Weekly")
        self.assertEqual(result["windows"][1]["period_minutes"], 10080)
        self.assertEqual(result["credit"], {"limit": 100, "used": 0,
                                            "currency": "USD",
                                            "resets_at": iso(NOW + timedelta(days=16))})
        self.assertIs(result["account_changed"], False)

    def test_the_result_keys_are_stable(self):
        self.write()
        result = self.refresh()
        self.assertEqual(set(result), {"available", "reason", "message", "path",
                                       "fetched_at", "windows", "credit",
                                       "account_changed", "refreshes", "since"})

    def test_a_null_window_is_not_reported_never_zero(self):
        self.write(cache_document(weekly=None))
        result = self.refresh()
        self.assertIs(result["available"], True)
        weekly = result["windows"][1]
        self.assertIsNone(weekly["used_percent"])
        self.assertEqual(weekly["state"], "unavailable")

    def test_a_null_five_hour_window_keeps_the_weekly_one(self):
        self.write(cache_document(five=None))
        result = self.refresh()
        self.assertIs(result["available"], True)
        self.assertIsNone(result["windows"][0]["used_percent"])
        self.assertEqual(result["windows"][1]["used_percent"], 66.0)

    def test_both_windows_null_is_unavailable_not_a_zero_reading(self):
        self.write(cache_document(five=None, weekly=None))
        result = self.refresh()
        self.assertIs(result["available"], False)
        self.assertEqual(result["reason"], "no quota reported")
        self.assertEqual(result["windows"], [])

    def test_no_sentinel_ever_reaches_the_result(self):
        self.write()
        result = self.refresh()
        text = json.dumps(result) + str(result)
        for sentinel in SENTINELS:
            self.assertNotIn(sentinel, text)


class ReadOnlyTests(CacheBase):
    def test_the_file_is_never_written_and_its_mtime_never_moves(self):
        self.write()
        before = self.path.stat()
        reader = self.reader()
        for _ in range(5):
            self.refresh(reader)
        after = self.path.stat()
        self.assertEqual(self.path.read_bytes(),
                         json.dumps(cache_document()).encode("utf-8"))
        self.assertEqual(before.st_mtime_ns, after.st_mtime_ns)
        self.assertEqual(before.st_size, after.st_size)

    def test_the_module_contains_no_write_or_touch_of_the_file(self):
        source = Path(claude_usage_cache.__file__).read_text(encoding="utf-8")
        for forbidden in ('"w"', '"a"', '"x"', '"r+"', '"w+"', '"a+"', '"rb+"',
                          "os.remove", "os.rename", "os.replace", "os.utime",
                          "os.chmod", "os.truncate", "shutil", ".write(",
                          "os.link", "os.symlink"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, source)

    def test_the_reader_only_opens_the_file_read_only(self):
        self.write()
        modes = []
        real_open = open

        def opener(file, mode="r", *args, **kwargs):
            modes.append(mode)
            return real_open(file, mode, *args, **kwargs)

        with patch("builtins.open", side_effect=opener):
            self.refresh()
        self.assertTrue(modes)
        for mode in modes:
            self.assertEqual(mode, "rb")


class IncrementalTests(CacheBase):
    def count_opens(self):
        counter = [0]
        real_open = open

        def opener(file, *args, **kwargs):
            counter[0] += 1
            return real_open(file, *args, **kwargs)

        patcher = patch("builtins.open", side_effect=opener)
        patcher.start()
        self.addCleanup(patcher.stop)
        return counter

    def test_an_unchanged_file_is_not_opened_again(self):
        self.write()
        counter = self.count_opens()
        reader = self.reader()
        self.refresh(reader)
        self.assertEqual(counter[0], 1)
        for _ in range(4):
            self.refresh(reader)
        self.assertEqual(counter[0], 1)

    def test_a_changed_file_is_read_again(self):
        self.write()
        counter = self.count_opens()
        reader = self.reader()
        self.refresh(reader)
        self.write(cache_document(five=20.0))
        result = self.refresh(reader)
        self.assertEqual(counter[0], 2)
        self.assertEqual(result["windows"][0]["used_percent"], 20.0)

    def test_a_ten_megabyte_file_is_parsed_once_and_not_re_read(self):
        padding = {"projects": {"/home/you/" + SENTINEL_PROJECT:
                                {"history": ["x" * 4096] * 3000}}}
        document = cache_document()
        document.update(padding)
        self.write(document)
        self.assertGreater(self.path.stat().st_size, 10 * 1024 * 1024)
        counter = self.count_opens()
        reader = self.reader()
        result = self.refresh(reader)
        self.assertIs(result["available"], True)
        self.assertEqual(result["windows"][0]["used_percent"], 12.0)
        self.assertEqual(counter[0], 1)
        self.refresh(reader)
        self.assertEqual(counter[0], 1)


class PathResolutionTests(CacheBase):
    def test_the_default_layout_finds_the_file_beside_the_claude_directory(self):
        settings = self.root / ".claude" / "settings.json"
        settings.parent.mkdir()
        settings.write_text("{}")
        candidates = candidate_paths(str(settings))
        self.assertEqual(candidates, [str(self.root / ".claude" / ".claude.json"),
                                      str(self.root / ".claude.json")])

    def test_a_custom_config_directory_keeps_the_file_inside_itself(self):
        settings = self.root / "claude-work" / "settings.json"
        settings.parent.mkdir()
        settings.write_text("{}")
        candidates = candidate_paths(str(settings))
        self.assertEqual(candidates, [str(self.root / "claude-work" / ".claude.json")])

    def test_an_explicit_path_wins_over_every_candidate(self):
        settings = self.root / ".claude" / "settings.json"
        candidates = candidate_paths(str(settings), str(self.root / "custom.json"))
        self.assertEqual(candidates[0], str(self.root / "custom.json"))
        self.assertEqual(len(candidates), 3)

    def test_the_reader_uses_the_first_candidate_that_exists(self):
        settings = self.root / ".claude" / "settings.json"
        settings.parent.mkdir()
        settings.write_text("{}")
        self.write(path=self.root / ".claude.json")
        reader = self.reader(candidate_paths(str(settings)))
        result = self.refresh(reader)
        self.assertIs(result["available"], True)
        self.assertEqual(result["path"], str(self.root / ".claude.json"))

    def test_no_candidate_exists_is_unavailable_with_the_paths_named(self):
        settings = self.root / ".claude" / "settings.json"
        settings.parent.mkdir()
        settings.write_text("{}")
        result = self.refresh(self.reader(candidate_paths(str(settings))))
        self.assertIs(result["available"], False)
        self.assertEqual(result["reason"], "no usage cache found")
        self.assertIn(str(self.root / ".claude.json"), result["message"])
        self.assertIn("open /usage", result["message"])


class CreditTests(CacheBase):
    def test_the_credit_is_found_by_shape_whatever_the_key_is_called(self):
        for key in ("iguana_necktie", "zebra_umbrella", "some_other_codename"):
            with self.subTest(key=key):
                self.write(cache_document(credit_key=key))
                result = self.refresh()
                self.assertIs(result["available"], True)
                self.assertEqual(result["credit"]["limit"], 100)
                self.assertEqual(result["credit"]["used"], 0)
                self.assertEqual(result["credit"]["currency"], "USD")

    def test_a_file_with_only_unknown_codename_keys_still_parses(self):
        self.write(cache_document(credit_key="", credit=False,
                                  extra={"mystery_key": {"utilization": 3,
                                                         "resets_at": iso(NOW)}}))
        result = self.refresh()
        self.assertIs(result["available"], True)
        self.assertEqual(result["windows"][0]["used_percent"], 12.0)
        self.assertIsNone(result["credit"])

    def test_the_credit_that_resets_soonest_wins(self):
        self.write(cache_document(extra={"soon": {"limit_dollars": 50,
                                                  "used_dollars": 10,
                                                  "resets_at": iso(NOW + timedelta(days=1))}}))
        result = self.refresh()
        self.assertEqual(result["credit"]["limit"], 50)

    def test_a_credit_without_a_limit_is_ignored(self):
        self.write(cache_document(extra={"broken": {"limit_dollars": 0,
                                                    "used_dollars": 0,
                                                    "resets_at": iso(NOW)}}))
        result = self.refresh()
        self.assertEqual(result["credit"]["limit"], 100)

    def test_the_credit_is_optional_and_never_an_error(self):
        self.write(cache_document(credit=False))
        result = self.refresh()
        self.assertIs(result["available"], True)
        self.assertIsNone(result["credit"])


class FetchTimeTests(CacheBase):
    def test_seconds_instead_of_milliseconds_is_a_format_change(self):
        document = cache_document()
        document["cachedUsageUtilization"]["fetchedAtMs"] = int(
            (NOW - timedelta(hours=2)).timestamp())
        self.write(document)
        result = self.refresh()
        self.assertIs(result["available"], False)
        self.assertIn("fetchedAtMs", result["reason"])
        self.assertIn("milliseconds", result["message"])
        self.assertNotIn("178", result["reason"])

    def test_a_fetch_time_before_2020_is_refused(self):
        document = cache_document()
        document["cachedUsageUtilization"]["fetchedAtMs"] = 1000
        self.write(document)
        result = self.refresh()
        self.assertIs(result["available"], False)
        self.assertIn("fetchedAtMs", result["reason"])

    def test_a_fetch_time_far_in_the_future_is_refused(self):
        document = cache_document()
        document["cachedUsageUtilization"]["fetchedAtMs"] = milliseconds(
            NOW + timedelta(days=30))
        self.write(document)
        result = self.refresh()
        self.assertIs(result["available"], False)
        self.assertIn("fetchedAtMs", result["reason"])

    def test_the_fetch_time_is_capped_at_now(self):
        document = cache_document()
        document["cachedUsageUtilization"]["fetchedAtMs"] = milliseconds(
            NOW + timedelta(hours=1))
        self.write(document)
        result = self.refresh()
        self.assertEqual(result["fetched_at"], iso(NOW))

    def test_a_fetch_time_older_than_a_week_still_returns_the_numbers(self):
        self.write(cache_document(fetched=NOW - timedelta(days=9)))
        result = self.refresh()
        self.assertIs(result["available"], True)
        self.assertEqual(result["windows"][0]["used_percent"], 12.0)
        self.assertIn("9 days old", result["message"])


class ValidationTests(CacheBase):
    def test_an_empty_file_is_unavailable(self):
        self.write(raw=b"")
        result = self.refresh()
        self.assertIs(result["available"], False)
        self.assertEqual(result["reason"], "read problem")

    def test_a_non_object_root_is_unavailable(self):
        self.write(raw=b"[1, 2, 3]")
        result = self.refresh()
        self.assertIs(result["available"], False)

    def test_a_missing_key_is_unavailable(self):
        self.write(raw=json.dumps({"somethingElse": {}}).encode("utf-8"))
        result = self.refresh()
        self.assertIs(result["available"], False)
        self.assertIn("cachedUsageUtilization", result["reason"])

    def test_a_key_with_the_wrong_type_is_unavailable(self):
        self.write(raw=json.dumps({"cachedUsageUtilization": "nope"}).encode("utf-8"))
        result = self.refresh()
        self.assertIs(result["available"], False)
        self.assertIn("cachedUsageUtilization", result["reason"])
        self.assertIn("string", result["reason"])

    def test_a_window_with_a_bad_field_is_dropped_with_its_reason(self):
        document = cache_document()
        document["cachedUsageUtilization"]["utilization"]["five_hour"]["utilization"] = "12"
        self.write(document)
        result = self.refresh()
        self.assertIs(result["available"], True)
        self.assertIsNone(result["windows"][0]["used_percent"])
        self.assertEqual(result["windows"][0]["state"], "unavailable")
        self.assertEqual(result["windows"][1]["used_percent"], 66.0)

    def test_a_bad_window_field_names_its_path_and_type_but_never_its_value(self):
        document = cache_document()
        document["cachedUsageUtilization"]["utilization"]["five_hour"]["utilization"] = "12"
        self.write(document)
        result = self.refresh()
        self.assertIn("cachedUsageUtilization.utilization.five_hour.utilization",
                      result["message"])
        self.assertIn("string", result["message"])
        self.assertNotIn("12", result["message"])

    def test_both_windows_bad_makes_the_whole_read_unavailable(self):
        document = cache_document()
        document["cachedUsageUtilization"]["utilization"]["five_hour"]["utilization"] = "12"
        document["cachedUsageUtilization"]["utilization"]["seven_day"]["utilization"] = []
        self.write(document)
        result = self.refresh()
        self.assertIs(result["available"], False)
        self.assertIn("cachedUsageUtilization.utilization.five_hour.utilization",
                      result["reason"])

    def test_a_bad_reset_time_drops_its_window(self):
        document = cache_document()
        document["cachedUsageUtilization"]["utilization"]["five_hour"]["resets_at"] = "soon"
        self.write(document)
        result = self.refresh()
        self.assertIs(result["available"], True)
        self.assertIsNone(result["windows"][0]["used_percent"])
        self.assertIn("resets_at", result["message"])

    def test_a_reset_time_too_far_away_drops_its_window(self):
        document = cache_document()
        document["cachedUsageUtilization"]["utilization"]["five_hour"]["resets_at"] = iso(
            NOW + timedelta(days=500))
        self.write(document)
        result = self.refresh()
        self.assertIsNone(result["windows"][0]["used_percent"])

    def test_nan_and_infinity_are_refused(self):
        for text in (b'{"cachedUsageUtilization": NaN}',
                     b'{"cachedUsageUtilization": {"utilization": Infinity}}'):
            with self.subTest(text=text):
                self.write(raw=text)
                result = self.refresh()
                self.assertIs(result["available"], False)

    def test_a_huge_number_is_refused(self):
        document = json.dumps(cache_document())
        huge = document.replace('"utilization": 12.0', '"utilization": 1e400')
        self.write(raw=huge.encode("utf-8"))
        result = self.refresh()
        self.assertIs(result["available"], True)
        self.assertIsNone(result["windows"][0]["used_percent"])
        self.assertEqual(result["windows"][1]["used_percent"], 66.0)

    def test_deeply_nested_junk_is_refused_without_raising(self):
        self.write(raw=b"[" * 5000 + b"]" * 5000)
        result = self.refresh()
        self.assertIs(result["available"], False)

    def test_invalid_utf8_is_refused(self):
        self.write(raw=b'{"cachedUsageUtilization": "\xff\xfe"}')
        result = self.refresh()
        self.assertIs(result["available"], False)

    def test_a_byte_order_mark_is_accepted(self):
        raw = b"\xef\xbb\xbf" + json.dumps(cache_document()).encode("utf-8")
        self.write(raw=raw)
        result = self.refresh()
        self.assertIs(result["available"], True)
        self.assertEqual(result["windows"][0]["used_percent"], 12.0)

    def test_a_file_above_the_size_limit_is_never_read(self):
        with open(self.path, "wb") as handle:
            handle.write(b"{}")
        os.truncate(str(self.path), 50 * 1024 * 1024)
        result = self.refresh()
        self.assertIs(result["available"], False)
        self.assertEqual(result["reason"], "usage cache too large")
        self.assertIn("32 MB safety limit", result["message"])
        self.assertIn("50 MB", result["message"])

    def test_a_directory_in_place_of_the_file_is_unavailable(self):
        directory = self.root / "as-a-directory"
        directory.mkdir()
        result = self.refresh(self.reader([str(directory)]))
        self.assertIs(result["available"], False)
        self.assertEqual(result["reason"], "not a regular file")

    def test_a_missing_file_is_unavailable(self):
        result = self.refresh(self.reader([str(self.root / "absent.json")]))
        self.assertIs(result["available"], False)
        self.assertEqual(result["reason"], "no usage cache found")


class AccountTests(CacheBase):
    def test_a_changed_account_is_reported_and_the_uuid_stays_hidden(self):
        self.write(cache_document(account=SENTINEL_UUID))
        reader = self.reader()
        result = self.refresh(reader)
        self.assertIs(result["available"], True)
        self.write(cache_document(account="ANOTHER-ACCOUNT-ID"))
        result = self.refresh(reader)
        self.assertIs(result["available"], False)
        self.assertEqual(result["reason"], "account changed")
        self.assertIs(result["account_changed"], True)
        self.assertIn("changed since the daemon started", result["message"])
        text = json.dumps(result) + str(result)
        self.assertNotIn(SENTINEL_UUID, text)
        self.assertNotIn("ANOTHER-ACCOUNT-ID", text)

    def test_the_account_hash_is_never_returned_or_logged(self):
        self.write()
        reader = self.reader()
        with self.assertLogs(LOGGER_NAME, level="DEBUG") as logs:
            self.refresh(reader)
        joined = "\n".join(logs.output)
        self.assertNotIn(SENTINEL_UUID, joined)
        digest = reader._account_fingerprint
        self.assertIsNotNone(digest)
        self.assertNotIn(digest, joined)
        self.assertNotIn(digest, json.dumps(self.refresh(reader)))

    def test_a_file_without_an_account_id_is_not_a_problem(self):
        document = cache_document()
        del document["cachedUsageUtilization"]["accountUuid"]
        self.write(document)
        result = self.refresh()
        self.assertIs(result["available"], True)
        self.assertIs(result["account_changed"], False)


class TransientTests(CacheBase):
    def broken(self):
        self.write(raw=b'{"cachedUsageUtilization": {"utilization": ')

    def test_two_transient_failures_keep_the_last_known_reading(self):
        self.write()
        reader = self.reader()
        good = self.refresh(reader)
        self.broken()
        for _ in range(2):
            result = self.refresh(reader)
            self.assertIs(result["available"], True)
            self.assertEqual(result["windows"][0]["used_percent"], 12.0)
            self.assertEqual(result["reason"], "read problem")

    def test_the_third_failure_becomes_a_persistent_problem(self):
        self.write()
        reader = self.reader()
        self.refresh(reader)
        self.broken()
        self.refresh(reader)
        self.refresh(reader)
        result = self.refresh(reader)
        self.assertIs(result["available"], False)
        self.assertEqual(result["reason"], "format changed")
        self.assertEqual(result["windows"], [])

    def test_a_transient_failure_without_a_previous_reading_is_unavailable(self):
        self.broken()
        result = self.refresh()
        self.assertIs(result["available"], False)
        self.assertEqual(result["reason"], "read problem")

    def test_a_read_permission_error_is_transient(self):
        self.write()
        reader = self.reader()
        self.refresh(reader)
        real_open = open

        def opener(file, *args, **kwargs):
            if str(file).endswith(".claude.json"):
                raise PermissionError("fixture: locked")
            return real_open(file, *args, **kwargs)

        with patch("builtins.open", side_effect=opener):
            result = self.refresh(reader)
        self.assertIs(result["available"], True)
        self.assertEqual(result["windows"][0]["used_percent"], 12.0)

    def test_a_permission_error_that_persists_becomes_access_denied(self):
        self.write()
        reader = self.reader()
        real_open = open

        def opener(file, *args, **kwargs):
            if str(file).endswith(".claude.json"):
                raise PermissionError("fixture: locked")
            return real_open(file, *args, **kwargs)

        with patch("builtins.open", side_effect=opener):
            for _ in range(3):
                result = self.refresh(reader)
        self.assertIs(result["available"], False)
        self.assertEqual(result["reason"], "access denied")

    def test_a_recovered_file_is_read_again(self):
        self.write()
        reader = self.reader()
        self.refresh(reader)
        self.broken()
        self.refresh(reader)
        self.write()
        result = self.refresh(reader)
        self.assertIs(result["available"], True)
        self.assertEqual(result["windows"][0]["used_percent"], 12.0)

    def test_refresh_never_raises_even_when_the_read_breaks(self):
        reader = self.reader()
        with patch.object(reader, "_refresh", side_effect=OSError("broken")):
            result = self.refresh(reader)
        self.assertIs(result["available"], False)
        self.assertEqual(result["windows"], [])

    def test_refresh_never_raises_when_a_failure_is_reported(self):
        reader = self.reader()
        with patch.object(reader, "_persistent",
                          side_effect=RuntimeError("broken")):
            result = self.refresh(reader)
        self.assertIs(result["available"], False)
        self.assertTrue(result["reason"])
        self.assertTrue(result["reason"].isascii())


class LoggingTests(CacheBase):
    def test_the_first_use_names_the_file_it_reads(self):
        self.write()
        with self.assertLogs(LOGGER_NAME, level="INFO") as logs:
            self.refresh()
        self.assertTrue(any("reading" in line and str(self.path) in line
                            for line in logs.output), logs.output)

    def test_the_file_is_named_only_once(self):
        self.write()
        reader = self.reader()
        with self.assertLogs(LOGGER_NAME, level="INFO") as logs:
            for _ in range(3):
                self.refresh(reader)
        self.assertEqual(len([line for line in logs.output if "reading" in line]), 1)

    def test_a_missing_file_is_warned_about_once_with_the_candidate_paths(self):
        reader = self.reader([str(self.root / "absent.json")])
        with self.assertLogs(LOGGER_NAME, level="WARNING") as logs:
            for _ in range(5):
                self.refresh(reader)
        warnings = [line for line in logs.output if "WARNING" in line]
        self.assertEqual(len(warnings), 1)
        self.assertIn("FILE NOT FOUND", warnings[0])
        self.assertIn(str(self.root / "absent.json"), warnings[0])
        self.assertIn("open /usage", warnings[0])

    def test_the_same_problem_is_not_warned_about_every_poll(self):
        reader = self.reader([str(self.root / "absent.json")])
        clock = [NOW]
        with self.assertLogs(LOGGER_NAME, level="WARNING") as logs:
            for _ in range(5):
                self.refresh(reader, clock[0])
                clock[0] += timedelta(seconds=10)
        warnings = [line for line in logs.output if "WARNING" in line]
        self.assertEqual(len(warnings), 1)

    def test_the_same_problem_is_warned_about_again_after_thirty_minutes(self):
        reader = self.reader([str(self.root / "absent.json")])
        with self.assertLogs(LOGGER_NAME, level="WARNING") as logs:
            self.refresh(reader, NOW)
            self.refresh(reader, NOW + timedelta(minutes=29))
            self.refresh(reader, NOW + timedelta(minutes=31))
        warnings = [line for line in logs.output if "WARNING" in line]
        self.assertEqual(len(warnings), 2)

    def test_a_changed_reason_is_warned_about_immediately(self):
        reader = self.reader([str(self.root / "absent.json")])
        with self.assertLogs(LOGGER_NAME, level="WARNING") as logs:
            self.refresh(reader, NOW)
            self.write()
            self.refresh(reader, NOW + timedelta(seconds=5))
        warnings = [line for line in logs.output if "WARNING" in line]
        self.assertEqual(len(warnings), 1)  # the good read warns about nothing

    def test_a_format_change_is_warned_about_with_the_field_and_the_type(self):
        document = cache_document()
        document["cachedUsageUtilization"]["utilization"]["five_hour"]["utilization"] = "12"
        document["cachedUsageUtilization"]["utilization"]["seven_day"]["utilization"] = "66"
        self.write(document)
        with self.assertLogs(LOGGER_NAME, level="WARNING") as logs:
            self.refresh()
        joined = "\n".join(logs.output)
        self.assertIn("FORMAT CHANGED", joined)
        self.assertIn("cachedUsageUtilization.utilization.five_hour.utilization", joined)
        self.assertIn("string", joined)
        self.assertIn("terminal-export readings are unaffected", joined)

    def test_a_transient_failure_is_debug_until_it_is_promoted(self):
        self.write()
        reader = self.reader()
        self.refresh(reader)
        self.write(raw=b"{broken")
        with self.assertLogs(LOGGER_NAME, level="DEBUG") as logs:
            self.refresh(reader)
            self.refresh(reader)
        debug_only = [line for line in logs.output if "WARNING" not in line]
        self.assertTrue(any("transient read problem" in line for line in debug_only),
                        logs.output)
        self.assertFalse(any("WARNING" in line for line in logs.output), logs.output)

    def test_the_promotion_is_warned_about(self):
        self.write()
        reader = self.reader()
        self.refresh(reader)
        self.write(raw=b"{broken")
        with self.assertLogs(LOGGER_NAME, level="WARNING") as logs:
            self.refresh(reader)
            self.refresh(reader)
            self.refresh(reader)
        joined = "\n".join(logs.output)
        self.assertIn("FORMAT CHANGED", joined)

    def test_a_recovery_is_logged(self):
        self.write()
        reader = self.reader()
        self.refresh(reader)
        self.write(raw=b"{broken")
        self.refresh(reader)
        self.write()
        with self.assertLogs(LOGGER_NAME, level="INFO") as logs:
            self.refresh(reader)
        self.assertTrue(any("RECOVERED" in line for line in logs.output), logs.output)

    def test_no_log_record_ever_holds_a_sentinel(self):
        self.write()
        reader = self.reader()
        records = []

        class Capture(logging.Handler):
            def emit(self, record):
                records.append("%s %s" % (record.levelname, record.getMessage()))

        handler = Capture()
        logger = logging.getLogger(LOGGER_NAME)
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)
        self.addCleanup(logger.removeHandler, handler)
        self.refresh(reader)
        self.write(raw=b"{broken")
        self.refresh(reader)
        self.write(cache_document(account="OTHER"))
        self.refresh(reader)
        joined = "\n".join(records)
        for sentinel in SENTINELS:
            self.assertNotIn(sentinel, joined)


class ModuleContractTests(unittest.TestCase):
    def test_the_module_imports_nothing_from_the_package(self):
        # Phase 5c reuses this file on other machines: stdlib only, no package.
        source = Path(claude_usage_cache.__file__).read_text(encoding="utf-8")
        self.assertNotIn("from .", source)
        self.assertNotIn("import presto_ai_dash", source)

    def test_the_module_has_no_package_import_statements(self):
        import ast
        tree = ast.parse(Path(claude_usage_cache.__file__).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertNotIn("presto_ai_dash", alias.name)
            if isinstance(node, ast.ImportFrom):
                self.assertEqual(node.level, 0)
                self.assertNotIn("presto_ai_dash", node.module or "")

    def test_the_logger_name_is_the_daemons(self):
        self.assertEqual(claude_usage_cache.LOGGER.name, LOGGER_NAME)


class DiagnosticsTests(CacheBase):
    def test_the_command_prints_the_reading_and_exits_zero(self):
        self.write()
        import io
        import contextlib
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = claude_usage_cache.main(["--path", str(self.path)])
        output = buffer.getvalue()
        self.assertEqual(code, 0)
        self.assertIn("5h: 12%", output)
        self.assertIn("Weekly: 66%", output)
        self.assertIn("Credit", output)
        for sentinel in SENTINELS:
            self.assertNotIn(sentinel, output)

    def test_the_command_prints_the_message_and_exits_one(self):
        import io
        import contextlib
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = claude_usage_cache.main(["--path", str(self.root / "absent.json")])
        self.assertEqual(code, 1)
        self.assertIn("FILE NOT FOUND", buffer.getvalue())


class RefreshRecordTests(CacheBase):
    """The record of the fetch times the daemon has seen on its own."""

    def test_the_first_read_records_the_fetch_time_and_the_start(self):
        self.write(cache_document(fetched=NOW - timedelta(hours=2)))
        result = self.refresh()
        self.assertEqual(result["refreshes"], [iso(NOW - timedelta(hours=2))])
        self.assertEqual(result["since"], iso(NOW))

    def test_the_same_fetch_time_is_never_counted_twice(self):
        # The file is rewritten with the same fetch time and a new modification
        # time, so the reader really reads it again.
        self.write_stamped(cache_document(fetched=NOW - timedelta(hours=2)),
                           NOW.timestamp() - 100)
        reader = self.reader()
        self.refresh(reader)
        for index in range(4):
            self.write_stamped(cache_document(fetched=NOW - timedelta(hours=2)),
                               NOW.timestamp() - 90 + index)
            result = self.refresh(reader)
        self.assertEqual(result["refreshes"], [iso(NOW - timedelta(hours=2))])

    def test_a_new_fetch_time_is_appended_newest_last(self):
        reader = self.reader()
        self.write_stamped(cache_document(fetched=NOW - timedelta(hours=2)),
                           NOW.timestamp() - 100)
        self.refresh(reader)
        self.write_stamped(cache_document(fetched=NOW - timedelta(hours=1)),
                           NOW.timestamp() - 50)
        result = self.refresh(reader)
        self.assertEqual(result["refreshes"], [iso(NOW - timedelta(hours=2)),
                                              iso(NOW - timedelta(hours=1))])

    def test_an_older_fetch_time_never_moves_the_record_backwards(self):
        reader = self.reader()
        self.write_stamped(cache_document(fetched=NOW - timedelta(hours=1)),
                           NOW.timestamp() - 100)
        self.refresh(reader)
        self.write_stamped(cache_document(fetched=NOW - timedelta(hours=5)),
                           NOW.timestamp() - 50)
        result = self.refresh(reader)
        self.assertEqual(result["refreshes"], [iso(NOW - timedelta(hours=1))])

    def test_the_record_keeps_at_most_fifty_fetch_times(self):
        reader = self.reader()
        for index in range(60):
            self.write_stamped(cache_document(fetched=NOW - timedelta(hours=60 - index)),
                               NOW.timestamp() - 600 + index)
            self.refresh(reader)
        result = self.refresh(reader)
        self.assertEqual(len(result["refreshes"]), 50)
        self.assertEqual(result["refreshes"][0], iso(NOW - timedelta(hours=50)))
        self.assertEqual(result["refreshes"][-1], iso(NOW - timedelta(hours=1)))

    def test_a_restart_starts_the_record_again(self):
        self.write_stamped(cache_document(fetched=NOW - timedelta(hours=2)),
                           NOW.timestamp() - 100)
        reader = self.reader()
        self.refresh(reader)
        self.write_stamped(cache_document(fetched=NOW - timedelta(hours=1)),
                           NOW.timestamp() - 50)
        self.refresh(reader)
        restarted = self.reader()
        result = self.refresh(restarted)
        self.assertEqual(result["refreshes"], [iso(NOW - timedelta(hours=1))])
        self.assertEqual(result["since"], iso(NOW))

    def test_only_timestamps_enter_the_record(self):
        self.write(cache_document(fetched=NOW - timedelta(hours=2)))
        result = self.refresh()
        for value in result["refreshes"]:
            self.assertIsInstance(value, str)
            datetime.fromisoformat(value)
        text = json.dumps(result)
        for sentinel in SENTINELS:
            self.assertNotIn(sentinel, text)

    def test_a_later_problem_keeps_the_record(self):
        self.write(cache_document(fetched=NOW - timedelta(hours=2)))
        reader = self.reader()
        self.refresh(reader)
        self.write(raw=b"{broken")
        result = self.refresh(reader)
        self.assertEqual(result["refreshes"], [iso(NOW - timedelta(hours=2))])


class LogWordingTests(CacheBase):
    """The log messages name the real checkbox and read as sentences."""

    def label(self):
        """The real label of the usage cache checkbox in the admin page."""
        from presto_ai_dash.sources.claude import ClaudeSource
        fields = {field["key"]: field for field in ClaudeSource.config_fields}
        return fields["usage_cache"]["label"]

    def captured(self, action):
        """The messages one action logs, without the level prefix."""
        records = []

        class Capture(logging.Handler):
            def emit(self, record):
                records.append(record.getMessage())

        handler = Capture()
        logger = logging.getLogger(LOGGER_NAME)
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)
        self.addCleanup(logger.removeHandler, handler)
        action()
        return records

    def denied(self):
        """Three failed reads, which is when the problem becomes persistent."""
        real_open = open

        def opener(file, *args, **kwargs):
            if str(file).endswith(".claude.json"):
                raise PermissionError("fixture: locked")
            return real_open(file, *args, **kwargs)

        reader = self.reader()
        with patch("builtins.open", side_effect=opener):
            for _ in range(3):
                self.refresh(reader)

    def changed_account(self):
        self.write(cache_document(account=SENTINEL_UUID))
        reader = self.reader()
        self.refresh(reader)
        self.write(cache_document(account="ANOTHER-ACCOUNT-ID"))
        self.refresh(reader)

    def recovered(self):
        self.write()
        reader = self.reader()
        self.refresh(reader)
        self.write(raw=b"{broken")
        self.refresh(reader)
        self.write()
        self.refresh(reader)

    def catalogue(self):
        """Every message the reader can log, one catalogue entry at a time."""
        messages = []
        # FILE NOT FOUND
        messages += self.captured(
            lambda: self.refresh(self.reader([str(self.root / "absent.json")])))
        # FORMAT CHANGED: a field has the wrong type
        document = cache_document()
        document["cachedUsageUtilization"]["utilization"]["five_hour"]["utilization"] = "12"
        document["cachedUsageUtilization"]["utilization"]["seven_day"]["utilization"] = "66"
        self.write(document)
        messages += self.captured(self.refresh)
        # NO QUOTA READING
        self.write(cache_document(five=None, weekly=None))
        messages += self.captured(self.refresh)
        # FILE TOO LARGE
        with open(self.path, "wb") as handle:
            handle.write(b"{}")
        os.truncate(str(self.path), 50 * 1024 * 1024)
        messages += self.captured(self.refresh)
        # NOT A FILE
        directory = self.root / "as-a-directory"
        directory.mkdir()
        messages += self.captured(lambda: self.refresh(self.reader([str(directory)])))
        # ACCESS DENIED
        self.write()
        messages += self.captured(self.denied)
        # ACCOUNT CHANGED
        messages += self.captured(self.changed_account)
        # RECOVERED, the first read, and the transient problem
        self.write()
        messages += self.captured(self.recovered)
        return messages

    def test_the_quoted_phrase_is_the_start_of_the_real_label(self):
        label = self.label()
        self.assertTrue(label.startswith(claude_usage_cache.SILENCE_PHRASE), label)
        self.assertIn(claude_usage_cache.SILENCE_PHRASE, label)

    def test_every_message_that_names_the_checkbox_quotes_the_real_label(self):
        for message in self.catalogue():
            if "Untick" in message:
                with self.subTest(message=message):
                    self.assertIn("'%s'" % claude_usage_cache.SILENCE_PHRASE, message)

    def test_no_message_quotes_the_old_label_or_the_old_capitalisation(self):
        for message in self.catalogue():
            with self.subTest(message=message):
                self.assertNotIn("Read Claude Code usage cache", message)
                self.assertNotIn("needs updating: Untick", message)

    def test_every_catalogue_message_is_one_well_formed_sentence_group(self):
        for message in self.catalogue():
            with self.subTest(message=message):
                self.assertTrue(message.startswith("Claude usage cache: "), message)
                self.assertTrue(message.endswith("."), message)
                self.assertTrue(message.isascii(), message)
                self.assertNotIn("  ", message)
                self.assertNotIn(" .", message)
                self.assertNotIn(" ,", message)
                for sentence in message.split(". ")[1:]:
                    self.assertTrue(sentence[:1].isupper(), (message, sentence))


class NoBackupFilesTests(unittest.TestCase):
    """The reader never looks at Claude Code's own backups of the file."""

    def test_the_module_never_mentions_the_backups(self):
        source = Path(claude_usage_cache.__file__).read_text(encoding="utf-8")
        self.assertNotIn("backups", source)
        self.assertNotIn(".backup", source)


class NoWritesTests(CacheBase):
    """The reader only reads: it writes no file, and keeps the record in memory."""

    def test_the_reader_never_opens_a_file_for_writing(self):
        self.write(cache_document())
        opened = []
        real_open = open

        def opener(file, mode="r", *args, **kwargs):
            opened.append((str(file), mode))
            return real_open(file, mode, *args, **kwargs)

        reader = self.reader()
        with patch("builtins.open", side_effect=opener):
            self.refresh(reader)
        self.assertTrue(opened)
        for name, mode in opened:
            for letter in ("w", "a", "x", "+"):
                self.assertNotIn(letter, mode, (name, mode))

    def test_the_record_lives_only_in_the_reader(self):
        self.write(cache_document(fetched=NOW - timedelta(hours=2)))
        reader = self.reader()
        result = self.refresh(reader)
        self.assertEqual(result["refreshes"], [iso(NOW - timedelta(hours=2))])
        # Nothing else holds the record: no file, no database.
        self.assertEqual(len(os.listdir(self.root)), 1)


if __name__ == "__main__":
    unittest.main()
