"""OpenCode Go and Ollama Cloud: a bad key, a missing permission and an incomplete reply
are told apart, and the key is never shown.

Every provider answer here is a mock. Nothing contacts a provider or touches an account.
"""

import json
import logging
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from presto_ai_dash.http_client import HttpError, HttpUnreachable
from presto_ai_dash.sources._quota_http import refusal_text
from presto_ai_dash.sources.ollama_cloud import OllamaCloudSource
from presto_ai_dash.sources.opencode_go import OpenCodeGoSource
from core.test_quotas import NOW, RESET, cloud_response, go_response

KEY = "oc_sk_ThisIsASecretKeyValue123"
GO = "presto_ai_dash.sources._quota_http.get_json"
CLOUD = "presto_ai_dash.sources.ollama_cloud.get_json"


def balance():
    return {"included": {"balance_usd": 72.5, "allowance_usd": 100,
                         "period": {"from": "2026-09-15T09:30:00Z",
                                    "until": "2026-10-15T09:30:00Z"}},
            "purchased": {"balance_usd": 25}}


class Logs(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.text = []

    def emit(self, record):
        self.text.append(record.getMessage())


class OpenCodeGoCredentialTests(unittest.TestCase):
    def collect(self, **patch_args):
        source = OpenCodeGoSource({"api_key": KEY})
        with patch(GO, **patch_args):
            return source.collect_safely()

    def test_a_good_key_reads_the_windows(self):
        snapshot = self.collect(return_value=go_response(23))
        self.assertEqual(snapshot.status, "ok")
        self.assertEqual(snapshot.metrics["quota"]["windows"][0]["used_percent"], 23)

    def test_a_bad_key_and_a_missing_permission_say_different_things(self):
        invalid = self.collect(side_effect=HttpError(401, KEY)).error
        denied = self.collect(side_effect=HttpError(403, KEY)).error
        self.assertIn("invalid, expired or revoked", invalid)
        self.assertIn("may not read", denied)
        self.assertNotEqual(invalid, denied)
        self.assertNotIn("master", (invalid + denied).lower())

    def test_other_refusals_are_named(self):
        self.assertIn("not found", self.collect(side_effect=HttpError(404)).error)
        self.assertIn("Rate limited", self.collect(side_effect=HttpError(429)).error)
        self.assertIn("HTTP 500", self.collect(side_effect=HttpError(500)).error)
        self.assertIn("Cannot reach", self.collect(side_effect=HttpUnreachable("refused")).error)

    def test_a_valid_reply_without_the_go_windows_is_not_a_success(self):
        # What a key that cannot see the subscription might get: 200, but no allowances.
        for reply in ({"usage": {}}, {"usage": {"requests": 4}}, {"other": "value"}, {}, []):
            with self.subTest(reply=reply):
                snapshot = self.collect(return_value=reply)
                self.assertEqual(snapshot.status, "offline")
                self.assertIn("no Go allowance windows", snapshot.error)
                self.assertTrue(all(w["state"] == "unavailable"
                                    for w in snapshot.metrics["quota"]["windows"]))

    def test_an_incomplete_window_is_a_changed_format_not_a_zero(self):
        reply = {"usage": {"rolling": {"status": "ok", "percent": 2}}}
        snapshot = self.collect(return_value=reply)
        self.assertIn("format changed", snapshot.error)
        self.assertNotEqual(snapshot.metrics["quota"]["windows"][0]["used_percent"], 0)

    def test_a_missing_window_is_unavailable_and_the_rest_still_read(self):
        reply = go_response(40)
        del reply["usage"]["monthly"]
        windows = self.collect(return_value=reply).metrics["quota"]["windows"]
        self.assertEqual(windows[0]["used_percent"], 40)
        self.assertEqual(windows[2]["state"], "unavailable")

    def test_the_key_never_appears_in_a_snapshot_or_a_log(self):
        handler = Logs()
        logging.getLogger().addHandler(handler)
        old_level = logging.getLogger().level
        logging.getLogger().setLevel(logging.DEBUG)
        try:
            failures = (HttpError(401, "bad key " + KEY), HttpError(403, KEY), HttpUnreachable(KEY),
                        ValueError(KEY), RuntimeError("boom " + KEY))
            for failure in failures:
                with self.subTest(failure=type(failure).__name__):
                    snapshot = self.collect(side_effect=failure)
                    self.assertNotIn(KEY, json.dumps(snapshot.to_dict()))
        finally:
            logging.getLogger().removeHandler(handler)
            logging.getLogger().setLevel(old_level)
        self.assertNotIn(KEY, " ".join(handler.text))

    def test_no_key_makes_no_request(self):
        with patch(GO) as get:
            snapshot = OpenCodeGoSource().collect()
        get.assert_not_called()
        self.assertIn("Add a OpenCode Go API key", snapshot.error)

    def test_refusal_words(self):
        self.assertEqual(refusal_text(403, "usage"), "Key may not read usage (HTTP 403)")


class OllamaCloudCredentialTests(unittest.TestCase):
    def collect(self, responder):
        source = OllamaCloudSource({"api_key": KEY})
        with patch(CLOUD, side_effect=responder):
            return source.collect_safely()

    @staticmethod
    def answer(balance_reply=None, usage_reply=None):
        def respond(url, **kwargs):
            reply = balance_reply if "/balance" in url else usage_reply
            if isinstance(reply, Exception):
                raise reply
            return reply
        return respond

    def test_a_good_key_reads_the_balance_and_the_activity_apart(self):
        report = self.collect(self.answer(balance(), cloud_response())).metrics["quota"]
        self.assertEqual(report["cloud_balance"]["included_remaining_usd"], 72.5)
        self.assertEqual(report["cloud_balance"]["purchased_remaining_usd"], 25)
        self.assertEqual(report["activity"]["request_count"], 2)
        self.assertIsNone(report["error"])

    def test_each_refusal_has_its_own_words(self):
        texts = {}
        for status in (401, 403, 429, 503, 500):
            texts[status] = self.collect(self.answer(HttpError(status, KEY))).error
        self.assertIn("invalid or revoked", texts[401])
        # Ollama keys have no permission levels: a 403 is the account, not a missing scope.
        self.assertIn("Account suspended", texts[403])
        self.assertIn("10 requests a minute", texts[429])
        self.assertIn("temporarily unavailable", texts[503])
        self.assertIn("HTTP 500", texts[500])
        self.assertEqual(len(set(texts.values())), 5)

    def test_a_failed_activity_read_keeps_a_good_balance(self):
        snapshot = self.collect(self.answer(balance(), HttpError(503)))
        report = snapshot.metrics["quota"]
        self.assertEqual(snapshot.status, "ok")
        self.assertEqual(report["cloud_balance"]["included_remaining_usd"], 72.5)
        self.assertIn("Activity", report["note"])

    def test_an_empty_or_incomplete_balance_is_unavailable_not_zero(self):
        for reply in ({}, {"included": {}}, {"purchased": {"balance_usd": 5}}, [],
                      {"included": {"balance_usd": 5}}):
            with self.subTest(reply=reply):
                snapshot = self.collect(self.answer(reply, cloud_response()))
                self.assertEqual(snapshot.status, "offline")
                windows = snapshot.metrics["quota"]["windows"]
                self.assertTrue(all(w["state"] == "unavailable" for w in windows))

    def test_a_legacy_plan_still_reads_its_limits(self):
        legacy = {"included": {"session": {"remaining_percent": 80, "resets_at": RESET},
                               "weekly": {"remaining_percent": 60, "resets_at": RESET}}}
        windows = self.collect(self.answer(legacy, cloud_response())).metrics["quota"]["windows"]
        self.assertEqual([w["used_percent"] for w in windows], [20, 40])

    def test_activity_without_cost_or_tokens_is_left_out_not_zero(self):
        legacy_activity = {"from": cloud_response()["from"], "until": cloud_response()["until"],
                           "totals": {"request_count": 9}}
        report = self.collect(self.answer(balance(), legacy_activity)).metrics["quota"]
        self.assertEqual(report["activity"]["request_count"], 9)
        self.assertIsNone(report["activity"]["usage_usd"])

    def test_the_key_never_appears_in_a_snapshot_or_a_log(self):
        handler = Logs()
        logging.getLogger().addHandler(handler)
        old_level = logging.getLogger().level
        logging.getLogger().setLevel(logging.DEBUG)
        try:
            for failure in (HttpError(401, "bad " + KEY), HttpError(403, KEY), HttpUnreachable(KEY),
                            ValueError(KEY)):
                with self.subTest(failure=type(failure).__name__):
                    snapshot = self.collect(self.answer(failure, failure))
                    self.assertNotIn(KEY, json.dumps(snapshot.to_dict()))
        finally:
            logging.getLogger().removeHandler(handler)
            logging.getLogger().setLevel(old_level)
        self.assertNotIn(KEY, " ".join(handler.text))

    def test_the_reads_are_get_requests_to_the_two_documented_routes_only(self):
        seen = []

        def respond(url, **kwargs):
            seen.append((url, kwargs["headers"]["Authorization"], kwargs["allow_redirects"]))
            return balance() if "/balance" in url else cloud_response()
        with patch(CLOUD, side_effect=respond):
            OllamaCloudSource({"api_key": KEY}).collect()
        self.assertEqual([url for url, _, _ in seen],
                         ["https://ollama.com/api/balance", "https://ollama.com/api/usage?range=7d"])
        self.assertTrue(all(auth == "Bearer " + KEY and not redirects
                            for _, auth, redirects in seen))


if __name__ == "__main__":
    unittest.main()
