"""The envelope validator and source push declarations (plan Phase 1)."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from presto_ai_dash.ingest import SCHEMA_NAME, ingest, validate
from presto_ai_dash.sources import Source, available_sources, source_type
from presto_ai_dash.sources.claude import ClaudeSource
from test_management import ManagementBase

NOW = datetime(2026, 10, 18, 14, 0, 0, tzinfo=timezone.utc)


class OpenSource:
    """A source class that publishes no push window list."""

    push_window_ids = None


def valid_envelope(percent=12.0, now=None):
    now = now or datetime.now(timezone.utc)
    return {
        "schema": SCHEMA_NAME,
        "collector": {"name": "claude-statusline", "version": "1", "host": "laptop"},
        "provider_version": "2.1.296",
        "client_time": now.isoformat(),
        "windows": [
            {"id": "five_hour", "label": "5h", "period_minutes": 300,
             "used_percent": percent, "resets_at": (now + timedelta(hours=5)).isoformat(),
             "state": "available"},
            {"id": "seven_day", "label": "Weekly", "period_minutes": 10080,
             "used_percent": None, "resets_at": None, "state": "unavailable"},
        ],
        "evidence": {"five_hour": "a" * 64, "seven_day": "b" * 64},
        "progress": "c" * 64,
    }


class ValidatorTests(unittest.TestCase):
    def reject(self, envelope, source=None):
        with self.assertRaises(ValueError):
            validate(envelope, source or OpenSource)

    def test_valid_envelope_is_accepted(self):
        validate(valid_envelope(), OpenSource)

    def test_heartbeat_envelope_without_windows_is_accepted(self):
        envelope = valid_envelope()
        for key in ("windows", "evidence", "progress"):
            del envelope[key]
        validate(envelope, OpenSource)

    def test_empty_windows_list_is_rejected(self):
        envelope = valid_envelope()
        envelope["windows"] = []
        with self.assertRaises(ValueError) as raised:
            validate(envelope, OpenSource)
        self.assertIn("omit the key", str(raised.exception))

    def test_non_object_envelope_is_rejected(self):
        for case in ([], "text", 5, None):
            with self.subTest(case=case):
                self.reject(case)

    def test_wrong_schema_is_rejected(self):
        for value in ("presto-ai-dash.observation/2", "presto-ai-dash.observation", None, 5):
            envelope = valid_envelope()
            envelope["schema"] = value
            with self.subTest(value=value):
                self.reject(envelope)

    def test_unknown_top_level_key_is_rejected(self):
        envelope = valid_envelope()
        envelope["extra"] = 1
        self.reject(envelope)

    def test_unknown_window_key_is_rejected(self):
        for key in ("observed_at", "used_amount", "limit_amount", "currency", "note"):
            envelope = valid_envelope()
            envelope["windows"][0][key] = "surprise"
            with self.subTest(key=key):
                self.reject(envelope)

    def test_window_missing_key_is_rejected(self):
        envelope = valid_envelope()
        del envelope["windows"][0]["resets_at"]
        self.reject(envelope)

    def test_bad_window_id_is_rejected(self):
        for value in ("", "x" * 33, "Five_Hour", "five-hour", "five hour", 7, None):
            envelope = valid_envelope()
            envelope["windows"][0]["id"] = value
            with self.subTest(value=value):
                self.reject(envelope)

    def test_duplicate_window_ids_are_rejected(self):
        envelope = valid_envelope()
        envelope["windows"][1] = dict(envelope["windows"][0])
        self.reject(envelope)

    def test_more_than_eight_windows_is_rejected(self):
        envelope = valid_envelope()
        envelope["windows"] = [dict(envelope["windows"][0], id="w%d" % index) for index in range(9)]
        self.reject(envelope)

    def test_bad_used_percent_is_rejected(self):
        for value in (-0.1, 1000.1, "12", True, float("nan"), float("inf")):
            envelope = valid_envelope()
            envelope["windows"][0]["used_percent"] = value
            with self.subTest(value=value):
                self.reject(envelope)

    def test_bad_period_is_rejected(self):
        for value in (0, -300, 527041, 300.5, True, "300"):
            envelope = valid_envelope()
            envelope["windows"][0]["period_minutes"] = value
            with self.subTest(value=value):
                self.reject(envelope)

    def test_bad_resets_at_is_rejected(self):
        now = datetime.now(timezone.utc)
        for value in ("tomorrow", "2026-10-18T18:00:00", 1760800000,
                      (now + timedelta(days=401)).isoformat(),
                      (now - timedelta(days=401)).isoformat()):
            envelope = valid_envelope()
            envelope["windows"][0]["resets_at"] = value
            with self.subTest(value=value):
                self.reject(envelope)

    def test_bad_window_state_is_rejected(self):
        envelope = valid_envelope()
        envelope["windows"][0]["state"] = "plenty"
        self.reject(envelope)

    def test_bad_label_is_rejected(self):
        for value in ("", "x" * 33, "5 hü"):
            envelope = valid_envelope()
            envelope["windows"][0]["label"] = value
            with self.subTest(value=value):
                self.reject(envelope)

    def test_bad_hash_is_rejected(self):
        for field, value in (("progress", "abc"), ("progress", "A" * 64), ("progress", "a" * 63),
                             ("progress", "a" * 65), ("progress", 7)):
            envelope = valid_envelope()
            envelope[field] = value
            with self.subTest(field=field, value=value):
                self.reject(envelope)
        envelope = valid_envelope()
        envelope["evidence"]["five_hour"] = "g" * 64
        self.reject(envelope)
        envelope = valid_envelope()
        envelope["evidence"]["five_hour"] = "a" * 32 + "0" * 32
        validate(envelope, OpenSource)

    def test_bad_strings_are_rejected(self):
        cases = [("host", ""), ("host", "x" * 33), ("host", "läptop"), ("host", 9),
                 ("name", ""), ("name", "x" * 65), ("name", "claude✓"),
                 ("version", ""), ("version", "x" * 33),
                 ("provider_version", "x" * 65), ("provider_version", "2.1 beta ü")]
        for field, value in cases:
            envelope = valid_envelope()
            if field in ("host", "name", "version"):
                envelope["collector"][field] = value
            else:
                envelope[field] = value
            with self.subTest(field=field, value=value):
                self.reject(envelope)

    def test_bad_collector_shape_is_rejected(self):
        for value in (None, "claude-statusline", [],
                      {"name": "claude-statusline", "version": "1"},
                      {"name": "claude-statusline", "version": "1", "host": "laptop", "extra": 1}):
            envelope = valid_envelope()
            envelope["collector"] = value
            with self.subTest(value=value):
                self.reject(envelope)

    def test_bad_evidence_shape_is_rejected(self):
        for value in ([], "x", {"bad id": "a" * 64}, {"five_hour": "a"},
                      {"five_hour": 7}, {"five_hour": "g" * 64}):
            envelope = valid_envelope()
            envelope["evidence"] = value
            with self.subTest(value=value):
                self.reject(envelope)

    def test_bad_client_time_is_rejected(self):
        envelope = valid_envelope()
        envelope["client_time"] = "not-a-time"
        self.reject(envelope)

    def test_oversized_values_are_rejected(self):
        envelope = valid_envelope()
        envelope["collector"]["host"] = "x" * 33
        envelope["windows"][0]["label"] = "y" * 33
        envelope["windows"].extend(
            [dict(envelope["windows"][0], id="w%d" % index) for index in range(7)])
        self.reject(envelope)

    def test_non_ascii_is_rejected(self):
        for value in ("läptop", "5 höur", "claude—statusline"):
            envelope = valid_envelope()
            envelope["collector"]["host"] = value
            with self.subTest(value=value):
                self.reject(envelope)

    def test_boundaries_are_accepted(self):
        envelope = valid_envelope(percent=0.0)
        envelope["windows"][0]["period_minutes"] = 1
        envelope["windows"][0]["resets_at"] = (
            datetime.now(timezone.utc) - timedelta(days=400) + timedelta(minutes=1)).isoformat()
        envelope["collector"]["host"] = "x" * 32
        validate(envelope, OpenSource)
        envelope = valid_envelope(percent=1000.0)
        envelope["windows"][0]["period_minutes"] = 527040
        envelope["windows"][0]["resets_at"] = (
            datetime.now(timezone.utc) + timedelta(days=400) - timedelta(minutes=1)).isoformat()
        envelope["windows"][0]["id"] = "x" * 32
        validate(envelope, OpenSource)
        envelope = valid_envelope()
        envelope["windows"] = [dict(envelope["windows"][0], id="w%d" % index) for index in range(8)]
        envelope["evidence"] = {"w0": None}
        envelope["progress"] = None
        validate(envelope, OpenSource)


class PushDeclarationTests(unittest.TestCase):
    def test_only_claude_and_codex_accept_push(self):
        self.assertTrue(ClaudeSource.accepts_push)
        self.assertEqual(ClaudeSource.push_window_ids, ("five_hour", "seven_day"))
        self.assertFalse(Source.accepts_push)
        self.assertIsNone(Source.push_window_ids)
        for type_id in available_sources():
            if type_id in ("claude", "codex"):
                continue  # asserted above and in test_pushed_readings
            with self.subTest(source=type_id):
                self.assertFalse(source_type(type_id).accepts_push)
                self.assertIsNone(source_type(type_id).push_window_ids)

    def test_push_window_ids_are_enforced(self):
        class Declared:
            push_window_ids = ("five_hour", "seven_day")

        envelope = valid_envelope()
        envelope["windows"][1]["id"] = "monthly"
        with self.assertRaises(ValueError):
            validate(envelope, Declared)
        validate(valid_envelope(), Declared)

    def test_undeclared_ids_pass_without_push_window_ids(self):
        envelope = valid_envelope()
        envelope["windows"][1]["id"] = "monthly"
        validate(envelope, OpenSource)


class IngestSourceTests(ManagementBase):
    def setUp(self):
        super().setUp()
        self.disable_fake()
        self.row = self.manager.save({"type": "claude", "name": "Claude personal", "enabled": True,
                                      "config": {"settings_path": str(Path(self.temp.name) / "settings.json")}})

    def test_report_uses_source_provider_and_product(self):
        self.assertTrue(ingest(self.storage, self.row["id"], valid_envelope(), NOW))
        report = self.storage.observation(self.row["id"])["report"]
        self.assertEqual(report["provider"], "anthropic")
        self.assertEqual(report["product"], "Claude")
        self.assertEqual(report["stale_after_seconds"], 900)
        self.assertIn("terminal export", report["note"])

    def test_claude_collect_reads_observation_and_collector_fields(self):
        sent = valid_envelope(percent=23.5, now=NOW)
        self.assertTrue(ingest(self.storage, self.row["id"], sent, NOW))
        source = ClaudeSource({"stale_after_seconds": 900})
        source.id, source.name = self.row["id"], self.row["name"]
        source.attach_storage(self.storage)
        snapshot = source.collect()
        report = snapshot.metrics["quota"]
        self.assertEqual([window["used_percent"] for window in report["windows"]], [23.5, None])
        self.assertEqual(report["collector_host"], "laptop")
        self.assertEqual(datetime.fromisoformat(report["collector_seen_at"]), NOW)
        self.assertEqual(datetime.fromisoformat(report["last_progress_at"]), NOW)
        self.assertEqual(snapshot.status, "ok")

    def test_awaiting_report_carries_an_empty_collector_trail(self):
        source = ClaudeSource({"stale_after_seconds": 900})
        source.id, source.name = self.row["id"], self.row["name"]
        source.attach_storage(self.storage)
        report = source.collect().metrics["quota"]
        # Rule 1's hint is built at payload time, so the pending report carries
        # the trail as None: no observation has ever arrived.
        self.assertTrue(all(report.get(key) is None for key in
                            ("collector_seen_at", "collector_host",
                             "collector_version", "last_progress_at")))
        self.assertTrue(all(window["state"] == "unavailable" for window in report["windows"]))

    def test_collector_seen_at_is_daemon_time_not_client_clock(self):
        for label, client_time in (("past", (NOW - timedelta(days=30)).isoformat()),
                                   ("future", (NOW + timedelta(days=30)).isoformat())):
            sent = valid_envelope()
            sent["client_time"] = client_time
            with self.subTest(client_time=label):
                self.assertTrue(ingest(self.storage, self.row["id"], sent, NOW))
                row = self.storage.observation(self.row["id"])
                self.assertEqual(datetime.fromisoformat(row["collector_seen_at"]), NOW)

    def test_missing_client_time_still_stamps_daemon_time(self):
        sent = valid_envelope()
        del sent["client_time"]
        self.assertTrue(ingest(self.storage, self.row["id"], sent, NOW))
        row = self.storage.observation(self.row["id"])
        self.assertEqual(datetime.fromisoformat(row["collector_seen_at"]), NOW)

    def test_empty_windows_list_is_rejected_by_ingest_and_stores_nothing(self):
        sent = valid_envelope()
        sent["windows"] = []
        with self.assertRaises(ValueError):
            ingest(self.storage, self.row["id"], sent, NOW)
        self.assertIsNone(self.storage.observation(self.row["id"]))

    def test_windows_key_with_no_entries_is_an_observation_not_a_heartbeat(self):
        """The key's presence decides: an empty list is data, not connectivity."""
        sent = valid_envelope()
        sent["windows"] = []
        with patch("presto_ai_dash.ingest.validate"):
            self.assertTrue(ingest(self.storage, self.row["id"], sent, NOW))
        stored = self.storage.observation(self.row["id"])
        self.assertIsNotNone(stored["report"])
        self.assertEqual(stored["report"]["windows"], [])
        self.assertEqual(stored["report"]["provider"], "anthropic")


if __name__ == "__main__":
    unittest.main()
