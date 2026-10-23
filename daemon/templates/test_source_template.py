"""A template for the test that keeps a new source honest.

Copy this file to ``daemon/tests/providers/test_<your_source>.py`` and fill in the
parts marked TODO. The template source it exercises lives in
``daemon/templates/source_template.py``; a real source lives in
``daemon/src/presto_ai_dash/sources/`` and is imported by name instead.

What a source test must cover:

* the happy path: a fixture response becomes the expected quota report,
* a window the provider did not report: *unavailable*, never 0%,
* a response with an unexpected shape: *offline*, never an exception,
* an unreachable service: *offline* with a reason,
* a rejected key: *offline* with a reason, and the key never in the error.

Never mock an external API from assumption. Record a real response from the
official documentation, or from the live service, and test against that.
"""

import os
import sys
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

# Run here in templates/, or after copying into a test group such as providers/.
HERE = os.path.dirname(os.path.abspath(__file__))
if os.path.basename(HERE) != "templates":
    HERE = os.path.dirname(HERE)  # shared tests root
sys.path.insert(0, os.path.join(HERE, "..", "src"))

from presto_ai_dash.models import SourceSnapshot  # noqa: E402

NOW = datetime(2026, 10, 20, 12, tzinfo=timezone.utc)

# A response shaped like the one the provider documents.
FIXTURE = {
    "usage": {
        "five_hour": {"percent": 12, "resets_at": "2026-10-20T15:00:00+00:00"},
        "seven_day": {"percent": 66, "resets_at": "2026-10-26T04:00:00+00:00"},
    }
}


def load_template():
    """Import the template source by path, so it is never auto-discovered."""
    import importlib.util

    path = os.path.join(HERE, "..", "templates", "source_template.py")
    spec = importlib.util.spec_from_file_location("source_template_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TemplateSourceTests(unittest.TestCase):
    def setUp(self):
        self.module = load_template()
        self.source = self.module.TemplateSource({"base_url": "http://localhost:9000",
                                                   "api_key": "secret-key"})
        # A fake response, so the test never touches the network.
        self.source.fetch = lambda: FIXTURE

    def test_the_happy_path_produces_the_expected_windows(self):
        snapshot = self.source.collect_safely()
        self.assertIsInstance(snapshot, SourceSnapshot)
        self.assertEqual(snapshot.status, "ok")
        windows = snapshot.metrics["quota"]["windows"]
        self.assertEqual([w["label"] for w in windows], ["5h", "Weekly"])
        self.assertEqual(windows[0]["used_percent"], 12)
        self.assertEqual(windows[1]["used_percent"], 66)

    def test_a_missing_window_is_unavailable_and_not_zero(self):
        self.source.fetch = lambda: {"usage": {"five_hour": FIXTURE["usage"]["five_hour"]}}
        snapshot = self.source.collect_safely()
        weekly = snapshot.metrics["quota"]["windows"][1]
        self.assertEqual(weekly["state"], "unavailable")
        self.assertIsNone(weekly["used_percent"])

    def test_an_unexpected_response_is_offline_and_does_not_raise(self):
        self.source.fetch = lambda: {"nonsense": True}
        snapshot = self.source.collect_safely()
        self.assertEqual(snapshot.status, "offline")
        self.assertIn("Usage unavailable", snapshot.error)

    def test_an_unreachable_service_is_offline(self):
        def boom():
            raise OSError("connection refused")
        self.source.fetch = boom
        snapshot = self.source.collect_safely()
        self.assertEqual(snapshot.status, "offline")
        self.assertTrue(snapshot.error)

    def test_the_secret_never_appears_in_an_error(self):
        def boom():
            raise ValueError("secret-key was rejected")
        self.source.fetch = boom
        snapshot = self.source.collect_safely()
        self.assertNotIn("secret-key", snapshot.error or "")
        self.assertTrue(snapshot.error)


if __name__ == "__main__":
    unittest.main()
