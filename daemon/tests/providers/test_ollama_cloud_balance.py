"""Documented balances drive quotas; rolling activity never supplies a limit."""

from datetime import datetime, timedelta, timezone
import json
import unittest
from unittest.mock import patch

from presto_ai_dash.card_tiles import tiles_for
from presto_ai_dash.alert_policy import threshold_status
from presto_ai_dash.dashboard import build_instance_payload, device_view
from presto_ai_dash.http_client import HttpError, HttpUnreachable
from presto_ai_dash.quotas import present_quota
from presto_ai_dash.sources.ollama_cloud import OllamaCloudSource
import presto_path  # noqa: F401
from home import source_rows


NOW = datetime(2026, 10, 24, 23, tzinfo=timezone.utc)
RESET = NOW + timedelta(days=14)


def balance(remaining=51.52, allowance=60):
    return {"included": {"balance_usd": remaining, "allowance_usd": allowance,
                         "period": {"from": (NOW - timedelta(days=16)).isoformat(),
                                    "until": RESET.isoformat()}},
            "purchased": {"balance_usd": 12.5}}


def usage():
    return {"range": "7d", "from": (NOW - timedelta(days=7)).isoformat(),
            "until": NOW.isoformat(),
            "totals": {"request_count": 297, "usage_usd": 8.480570,
                       "input_tokens": 22000000, "cached_input_tokens": 1000000,
                       "output_tokens": 231608}}


class OllamaBalanceTests(unittest.TestCase):
    def test_balance_supplies_real_monthly_quota_and_separate_purchased_funds(self):
        data = balance()
        data["email"] = "private@example.test"
        report, limited = OllamaCloudSource().parse_balance(data, NOW)
        window = report["windows"][0]
        self.assertFalse(limited)
        self.assertAlmostEqual(window["used_percent"], 100 * 8.48 / 60)
        self.assertAlmostEqual(window["used_amount"], 8.48)
        self.assertEqual(window["limit_amount"], 60)
        self.assertEqual(window["currency"], "USD")
        self.assertEqual(window["resets_at"], RESET.isoformat())
        self.assertEqual(report["cloud_balance"]["included_remaining_usd"], 51.52)
        self.assertEqual(report["cloud_balance"]["purchased_remaining_usd"], 12.5)
        self.assertNotIn("private@", json.dumps(report))

    def test_zero_exhausted_and_unknown_are_distinct(self):
        source = OllamaCloudSource()
        report, limited = source.parse_balance(balance(60), NOW)
        self.assertEqual(report["windows"][0]["used_percent"], 0)
        self.assertFalse(limited)
        report, limited = source.parse_balance(balance(0), NOW)
        self.assertEqual(report["windows"][0]["used_percent"], 100)
        self.assertFalse(limited)  # Purchased funds still cover usage.
        report, limited = source.parse_balance(balance(0, 0), NOW)
        self.assertIsNone(report["windows"][0]["used_percent"])
        self.assertFalse(limited)
        self.assertIsNone(source.empty_report()["windows"][0]["used_percent"])

    def test_legacy_percent_is_remaining_and_session_duration_is_not_invented(self):
        report, limited = OllamaCloudSource().parse_balance({"included": {
            "session": {"remaining_percent": 75, "resets_at": RESET.isoformat()},
            "weekly": {"remaining_percent": 40, "resets_at": RESET.isoformat()}}}, NOW)
        self.assertEqual([w["used_percent"] for w in report["windows"]], [25, 60])
        self.assertEqual([w["period_minutes"] for w in report["windows"]], [None, 10080])
        self.assertFalse(limited)
        self.assertNotIn("purchased_remaining_usd", report["cloud_balance"])

    def test_malformed_balances_cannot_become_zero_usage(self):
        bad = [None, {}, {"included": {}}, {"included": {"session": {}}}]
        for key in ("balance_usd", "allowance_usd"):
            for value in (None, True, -1, "60", float("nan"), float("inf")):
                data = balance()
                data["included"][key] = value
                bad.append(data)
        data = balance()
        data["included"]["period"]["until"] = data["included"]["period"]["from"]
        bad.extend([data, balance(61), {"included": {"weekly": {"remaining_percent": 101,
                    "resets_at": RESET.isoformat()}}}])
        for data in bad:
            with self.subTest(data=data), self.assertRaises(ValueError):
                OllamaCloudSource().parse_balance(data, NOW)

    def collect(self, source, responses, moment=1000):
        with patch("presto_ai_dash.sources.ollama_cloud.get_json", side_effect=responses) as get, \
             patch("presto_ai_dash.sources.ollama_cloud.now_utc", return_value=NOW), \
             patch("presto_ai_dash.sources.ollama_cloud.time.monotonic", return_value=moment):
            snapshot = source.collect()
        return snapshot, get

    def test_collection_uses_only_authenticated_gets_and_caches_at_least_one_minute(self):
        source = OllamaCloudSource({"api_key": "fixture-secret", "refresh_seconds": 30})
        snapshot, get = self.collect(source, [balance(), usage()])
        self.assertEqual(snapshot.status, "ok")
        self.assertEqual([call.args[0] for call in get.call_args_list],
                         [source.balance_url, source.usage_url + "?range=7d"])
        for call in get.call_args_list:
            self.assertFalse(call.kwargs["allow_redirects"])
            self.assertEqual(call.kwargs["headers"]["Authorization"], "Bearer fixture-secret")
        cached, get = self.collect(source, [], 1059)
        get.assert_not_called()
        self.assertEqual(cached.updated_at, snapshot.updated_at)
        self.assertNotIn("fixture-secret", json.dumps(snapshot.to_dict()))

    def test_partial_failures_retain_each_reading_and_recover_independently(self):
        source = OllamaCloudSource({"api_key": "fixture-secret"})
        first, _ = self.collect(source, [balance(), usage()])
        source._next_probe = 0
        second, _ = self.collect(source, [balance(50), HttpUnreachable("failed")], 1100)
        self.assertEqual(second.status, "ok")
        presented = present_quota(second.metrics["quota"], NOW)
        self.assertEqual(presented["windows"][0]["state"], "available")
        self.assertEqual(presented["activity"]["state"], "stale")
        self.assertEqual(presented["activity"]["request_count"], 297)
        source._next_probe = 0
        third, _ = self.collect(source, [HttpUnreachable("failed"), usage()], 1200)
        presented = present_quota(third.metrics["quota"], NOW)
        self.assertEqual(presented["windows"][0]["state"], "stale")
        self.assertEqual(presented["cloud_balance"]["state"], "stale")
        self.assertEqual(presented["cloud_balance"]["included_remaining_usd"], 50)
        self.assertEqual(presented["activity"]["state"], "available")
        source._next_probe = 0
        recovered, _ = self.collect(source, [balance(), usage()], 1300)
        self.assertEqual(recovered.status, "ok")
        self.assertIsNone(recovered.metrics["quota"]["error"])
        self.assertEqual(first.metrics["quota"]["cloud_balance"]["included_remaining_usd"], 51.52)

    def test_balance_failure_keeps_fresh_activity_even_before_first_balance(self):
        snapshot, _ = self.collect(OllamaCloudSource({"api_key": "fixture"}),
                                   [HttpUnreachable("failed"), usage()])
        report = present_quota(snapshot.metrics["quota"], NOW)
        self.assertEqual(report["windows"][0]["state"], "unavailable")
        self.assertEqual(report["activity"]["state"], "available")
        self.assertEqual(report["activity"]["request_count"], 297)

    def test_request_only_legacy_activity_survives_collection_and_all_presentations(self):
        legacy = {"included": {"weekly": {"remaining_percent": 40,
                                         "resets_at": RESET.isoformat()}}}
        request_only = usage()
        request_only["totals"] = {"request_count": 23}
        source = OllamaCloudSource({"api_key": "fixture"})
        snapshot, _ = self.collect(source, [legacy, request_only])
        self.assertEqual(snapshot.status, "ok")
        report = present_quota(snapshot.metrics["quota"], NOW)
        self.assertIsNone(report["activity"]["usage_usd"])
        self.assertIsNone(report["activity"]["input_tokens"])
        row = {"id": "cloud", "type": "ollama_cloud", "name": "Cloud", "enabled": True}
        payload = build_instance_payload({"cloud": snapshot}, [row], now=NOW)
        self.assertEqual(payload["cards"][1]["primary"], "Weekly: 60%")
        self.assertEqual(source_rows(device_view(payload))[0]["activity"], "23 req / 7d")
        self.assertEqual(tiles_for("ollama_cloud", payload["sources"][0], NOW),
                         [{"label": "req 7d", "value": "23", "tone": "normal"}])
        # Activity-only fallback still renders when the balance endpoint fails.
        failed, _ = self.collect(OllamaCloudSource({"api_key": "fixture"}),
                                [HttpUnreachable("failed"), request_only])
        fallback = build_instance_payload({"cloud": failed}, [row], now=NOW)
        self.assertEqual(fallback["cards"][1]["primary"], "23 requests")
        self.assertEqual(fallback["cards"][1]["secondary"], "")

    def test_purchased_credit_prevents_false_rate_limited_status_but_keeps_exhaustion_alert(self):
        row = {"id": "cloud", "type": "ollama_cloud", "name": "Cloud", "enabled": True}
        for purchased, expected in ((12.5, "critical"), (0, "rate_limited")):
            with self.subTest(purchased=purchased):
                data = balance(0)
                data["purchased"]["balance_usd"] = purchased
                source = OllamaCloudSource({"api_key": "fixture"})
                snapshot, _ = self.collect(source, [data, usage()])
                report = present_quota(snapshot.metrics["quota"], NOW)
                self.assertEqual(report["windows"][0]["used_percent"], 100)
                self.assertEqual(threshold_status(report, now=NOW), expected)
                payload = build_instance_payload({"cloud": snapshot}, [row], now=NOW)
                self.assertEqual(payload["sources"][0]["status"], expected)
                self.assertEqual(payload["overall_status"], expected)
                self.assertEqual(payload["cards"][1]["status"], expected)

    def test_legacy_exhaustion_keeps_rate_limited_status(self):
        report, limited = OllamaCloudSource().parse_balance({"included": {
            "weekly": {"remaining_percent": 0, "resets_at": RESET.isoformat()}},
            "purchased": {"balance_usd": 12.5}}, NOW)
        self.assertTrue(limited)
        self.assertEqual(threshold_status(present_quota(report, NOW), now=NOW), "rate_limited")

    def test_retry_after_defers_calls_and_does_not_expose_provider_error_body(self):
        source = OllamaCloudSource({"api_key": "fixture-secret"})
        snapshot, get = self.collect(source, [HttpError(429, "fixture-secret", "300")])
        self.assertEqual(get.call_count, 1)
        self.assertEqual(source._next_probe, 1300)
        self.assertNotIn("fixture-secret", json.dumps(snapshot.to_dict()))
        _, get = self.collect(source, [], 1299)
        get.assert_not_called()
        recovered, _ = self.collect(source, [balance(), usage()], 1300)
        self.assertEqual(recovered.status, "ok")

    def test_missing_key_does_not_contact_provider(self):
        snapshot, get = self.collect(OllamaCloudSource(), [])
        get.assert_not_called()
        self.assertEqual(snapshot.status, "offline")

    def test_balance_ages_and_expires_without_becoming_fresh_zero(self):
        report, _ = OllamaCloudSource().parse_balance(balance(), NOW)
        for when in (NOW + timedelta(seconds=181), RESET):
            presented = present_quota(report, when)
            self.assertEqual(presented["windows"][0]["state"], "stale")
            self.assertEqual(presented["cloud_balance"]["state"], "stale")
            self.assertGreater(presented["windows"][0]["used_percent"], 14)

    def test_device_receives_quota_and_bounded_credit_tiles_without_activity_overriding_headline(self):
        source = OllamaCloudSource({"api_key": "fixture"})
        snapshot, _ = self.collect(source, [balance(), usage()])
        payload = build_instance_payload({"cloud": snapshot},
            [{"id": "cloud", "type": "ollama_cloud", "name": "Ollama Cloud", "enabled": True}], now=NOW)
        self.assertTrue(payload["cards"][1]["primary"].startswith("Monthly credits:"))
        self.assertIn("Included left: $51.52", payload["cards"][1]["detail"])
        full = source_rows(payload)
        slim = source_rows(device_view(payload))
        self.assertEqual(full, slim)
        self.assertIn("$8.48 / 7d", slim[0]["activity"])
        self.assertAlmostEqual(slim[0]["windows"][0]["percent"], 100 * 8.48 / 60)
        tiles = tiles_for("ollama_cloud", payload["sources"][0], NOW)
        self.assertEqual(len(tiles), 6)
        self.assertIn({"label": "left", "value": "$51.52", "tone": "normal"}, tiles)
        self.assertIn({"label": "$ 7d", "value": "$8.48", "tone": "normal"}, tiles)


if __name__ == "__main__":
    unittest.main()
