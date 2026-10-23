"""The source template stays working, and stays out of the source catalog.

``daemon/templates/source_template.py`` is the starting point for a new
source. It lives outside the importable package on purpose, so the daemon can
never discover it and the admin page can never offer it as a connector. This
test imports it by path, runs its ``collect()`` against a fake response, and
proves that it is not in the catalog.
"""

import importlib.util
from datetime import datetime, timezone
from pathlib import Path
import sys
import unittest

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE.parents[0] / "src"))

from presto_ai_dash.models import SourceSnapshot  # noqa: E402
from presto_ai_dash.sources import source_catalog  # noqa: E402

TEMPLATE = HERE.parents[0] / "templates" / "source_template.py"

NOW = datetime(2026, 10, 20, 12, tzinfo=timezone.utc)

FIXTURE = {
    "usage": {
        "five_hour": {"percent": 12, "resets_at": "2026-10-20T15:00:00+00:00"},
        "seven_day": {"percent": 66, "resets_at": "2026-10-26T04:00:00+00:00"},
    }
}


def load_template():
    """Import the template by path, so discovery never sees it."""
    spec = importlib.util.spec_from_file_location("source_template_under_test",
                                                  TEMPLATE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class SourceTemplateTests(unittest.TestCase):
    def setUp(self):
        self.module = load_template()
        self.source = self.module.TemplateSource({"base_url": "http://localhost:9000",
                                                   "api_key": "secret-key"})

    def test_the_template_is_not_in_the_source_catalog(self):
        """The template can never appear in the admin page's connector list."""
        ids = [entry["id"] for entry in source_catalog()]
        self.assertNotIn(self.module.TemplateSource.id, ids)
        self.assertNotIn("template_example", ids)

    def test_collect_turns_a_fake_response_into_a_quota_report(self):
        self.source.fetch = lambda: FIXTURE
        snapshot = self.source.collect_safely()
        self.assertIsInstance(snapshot, SourceSnapshot)
        self.assertEqual(snapshot.status, "ok")
        report = snapshot.metrics["quota"]
        self.assertEqual(report["provider"], "example")
        self.assertEqual([w["label"] for w in report["windows"]], ["5h", "Weekly"])
        self.assertEqual(report["windows"][0]["used_percent"], 12)
        self.assertEqual(report["windows"][1]["used_percent"], 66)

    def test_a_window_the_provider_did_not_report_is_unavailable(self):
        self.source.fetch = lambda: {"usage": {"five_hour": FIXTURE["usage"]["five_hour"]}}
        snapshot = self.source.collect_safely()
        weekly = snapshot.metrics["quota"]["windows"][1]
        self.assertEqual(weekly["state"], "unavailable")
        self.assertIsNone(weekly["used_percent"])

    def test_an_unexpected_response_is_offline_rather_than_an_exception(self):
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

    def test_the_template_declares_a_secret_and_a_config_field_set(self):
        self.assertEqual(self.module.TemplateSource.secret_field, "api_key")
        keys = [field["key"] for field in self.module.TemplateSource.config_fields]
        self.assertIn("base_url", keys)
        for field in self.module.TemplateSource.config_fields:
            self.assertIn(field["type"], ("string", "url", "int", "bool"))
            self.assertIn("label", field)
            self.assertIn("default", field)

    def test_the_template_test_template_exists(self):
        """The matching test template ships with the source template."""
        self.assertTrue((TEMPLATE.parent / "test_source_template.py").is_file())


if __name__ == "__main__":
    unittest.main()
