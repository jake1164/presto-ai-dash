"""A warning on a local service always says why, and what to do about it."""

from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from presto_ai_dash.card_tiles import local_notices
from presto_ai_dash.http_client import HttpError
from presto_ai_dash.sources import litellm
from presto_ai_dash.sources.litellm import LiteLLMSource, refusal_reason

KEY = "sk-secret-1234"


class RefusalReasonTests(unittest.TestCase):
    def test_the_error_message_of_the_gateway_is_used(self):
        body = '{"error": {"message": "Only proxy admin can call this", "type": "auth_error"}}'
        self.assertEqual(refusal_reason(body), "Only proxy admin can call this")

    def test_a_detail_field_and_plain_text_are_used_too(self):
        self.assertEqual(refusal_reason('{"detail": "Not allowed"}'), "Not allowed")
        self.assertEqual(refusal_reason("Forbidden"), "Forbidden")

    def test_the_key_is_masked_if_the_gateway_echoes_it(self):
        reason = refusal_reason('{"error": {"message": "bad key %s here"}}' % KEY, KEY)
        self.assertNotIn(KEY, reason)
        self.assertIn("***", reason)

    def test_html_and_junk_give_nothing_and_text_is_bounded_ascii(self):
        self.assertEqual(refusal_reason("<html>403</html>"), "")
        self.assertEqual(refusal_reason(""), "")
        self.assertEqual(refusal_reason(None), "")
        long = refusal_reason('{"detail": "café %s"}' % ("x" * 500))
        self.assertTrue(long.isascii())
        self.assertLessEqual(len(long), 200)


class NoticeTests(unittest.TestCase):
    def collect(self, status, body):
        source = LiteLLMSource({"base_url": "http://x", "api_key": KEY})

        def fake(url, **kwargs):
            if "daily/activity" in url:
                raise HttpError(status, body)
            return {"db": "connected"}
        with patch.object(litellm, "get_json", side_effect=fake), \
                patch.object(litellm, "get_text", return_value="I'm alive!"):
            return source.collect()

    def test_a_refused_key_is_explained_with_the_gateways_words(self):
        snapshot = self.collect(403, '{"error": {"message": "key not allowed to access route"}}')
        self.assertEqual(snapshot.status, "warning")
        notices = local_notices(snapshot.alerts, snapshot.error, snapshot.metrics)
        self.assertEqual(len(notices), 1)
        self.assertIn("lacks permission", notices[0]["message"])
        self.assertEqual(notices[0]["reason"], "key not allowed to access route")
        self.assertIn("403", notices[0]["hint"])

    def test_a_key_limited_to_model_routes_is_told_so(self):
        snapshot = self.collect(403, '{"error": {"message": "Virtual key is not allowed to call '
                                     "this route. Only allowed to call routes: ['llm_api_routes']."
                                     ' Tried to call route: /user/daily/activity"}}')
        hint = local_notices(snapshot.alerts, snapshot.error, snapshot.metrics)[0]["hint"]
        self.assertIn("/user/daily/activity", hint)
        self.assertIn("limited to certain routes", hint)

    def test_each_refusal_has_a_hint(self):
        for status, start in ((401, "LiteLLM rejected"), (404, "LiteLLM daily activity")):
            snapshot = self.collect(status, "")
            notices = local_notices(snapshot.alerts, snapshot.error, snapshot.metrics)
            self.assertTrue(notices[0]["message"].startswith(start))
            self.assertTrue(notices[0]["hint"])

    def test_no_alerts_and_no_error_means_no_notice(self):
        self.assertEqual(local_notices([], None, {}), [])

    def test_an_error_without_an_alert_is_still_shown(self):
        notices = local_notices([], "cannot reach Ollama at http://x: refused", {})
        self.assertEqual(notices[0]["message"], "cannot reach Ollama at http://x: refused")
        self.assertIn("localhost", notices[0]["hint"])


if __name__ == "__main__":
    unittest.main()
