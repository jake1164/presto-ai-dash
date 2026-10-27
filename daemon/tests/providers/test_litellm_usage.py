"""What the LiteLLM source reads from a day's activity, and how the card shows it."""

from datetime import datetime, timezone
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from presto_ai_dash.card_tiles import compact_count, local_readings, spend_text, tiles_for
from presto_ai_dash.dashboard import build_status_payload
from presto_ai_dash.sources.litellm import LiteLLMSource, _top_model

NOW = datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc)

DAY = {"results": [{"date": "2026-10-27", "metrics": {
    "spend": 0.0, "api_requests": 5, "successful_requests": 5, "failed_requests": 0,
    "prompt_tokens": 8000, "completion_tokens": 2868, "total_tokens": 10868},
    "breakdown": {"models": {
        "tiny": {"metrics": {"spend": 0.0, "api_requests": 1, "total_tokens": 100}},
        "qwen3.5:9b": {"metrics": {"spend": 0.0, "api_requests": 4, "total_tokens": 10768}}}}}],
    "metadata": {"total_spend": 0.0, "total_api_requests": 5, "total_prompt_tokens": 8000,
                 "total_completion_tokens": 2868}}


class CollectTests(unittest.TestCase):
    def collect(self, activity=DAY, readiness=None):
        source = LiteLLMSource({"api_key": "k"})
        with patch.object(source, "_liveliness", return_value="alive"), \
                patch.object(source, "_readiness", return_value=readiness or {
                    "db": "connected", "litellm_version": "1.55.0"}), \
                patch.object(source, "_daily_activity", return_value=activity):
            return source.collect().metrics

    def test_it_reads_the_version_the_split_of_tokens_and_the_models(self):
        metrics = self.collect()
        self.assertEqual(metrics["version"], "1.55.0")
        self.assertEqual((metrics["input_tokens_today"], metrics["output_tokens_today"]),
                         (8000, 2868))
        self.assertEqual(metrics["models_today"], 2)
        self.assertEqual(metrics["tokens_today"], 10868)

    def test_with_everything_free_the_busiest_model_is_the_top_one(self):
        self.assertEqual(self.collect()["top_model"], "qwen3.5:9b")
        self.assertEqual(_top_model({"models": {
            "a": {"metrics": {"spend": 0, "api_requests": 1}},
            "b": {"metrics": {"spend": 0, "api_requests": 9}}}}), "b")

    def test_spend_still_beats_requests(self):
        self.assertEqual(_top_model({"models": {
            "cheap": {"metrics": {"spend": 0.01, "api_requests": 90}},
            "dear": {"metrics": {"spend": 2.0, "api_requests": 1}}}}), "dear")

    def test_an_odd_version_is_left_out(self):
        self.assertNotIn("version", self.collect(readiness={"litellm_version": "\u00e9" * 3}))
        self.assertNotIn("version", self.collect(readiness={"litellm_version": 7}))

    def test_a_row_without_a_split_reports_no_split(self):
        metrics = self.collect({"results": [{"metrics": {"api_requests": 2, "total_tokens": 9}}]})
        self.assertNotIn("input_tokens_today", metrics)
        self.assertEqual(metrics["tokens_today"], 9)


class ShowTests(unittest.TestCase):
    def snapshot(self, **extra):
        from presto_ai_dash.models import SourceSnapshot
        metrics = {"online": True, "database": "connected", "version": "1.55.0",
                   "requests_today": 5, "tokens_today": 10868, "spend_today": 0.0,
                   "errors_today": 0, "top_model": "qwen3.5:9b", "models_today": 2,
                   "input_tokens_today": 8000, "output_tokens_today": 2868}
        metrics.update(extra)
        return SourceSnapshot("litellm", "ok", metrics=metrics)

    def test_counts_and_money_are_short(self):
        self.assertEqual([compact_count(n) for n in (0, 940, 1000, 10868, 99999, 124000, 1_200_000)],
                         ["0", "940", "1.0k", "10.9k", "100.0k", "124k", "1.2M"])
        self.assertEqual([spend_text(v) for v in (0, 0.5, 0.0031, 128.5)],
                         ["$0.00", "$0.50", "$0.0031", "$128.50"])

    def test_the_card_has_six_tiles_that_say_what_the_day_looked_like(self):
        tiles = {t["label"]: t["value"] for t in tiles_for("litellm", {"metrics": self.snapshot().metrics}, NOW)}
        self.assertEqual(tiles, {"requests": "5", "tokens": "10.9k", "spend": "$0.00",
                                 "errors": "0", "model": "qwen3.5:9b", "in/out": "8.0k/2.9k"})

    def test_the_card_lines_are_the_facts_not_unavailable_markers(self):
        card = build_status_payload({"litellm": self.snapshot()})["cards"][2]
        self.assertEqual(card["primary"], "5 requests today")
        self.assertEqual(card["secondary"], "10.9k tokens \u00b7 spend $0.00")
        self.assertEqual(card["detail"], ["Models used: 2", "v1.55.0 - DB connected"])

    def test_a_model_name_the_tile_cut_short_is_given_in_full(self):
        card = build_status_payload({"litellm": self.snapshot(
            top_model="anthropic/claude-sonnet-4")})["cards"][2]
        self.assertEqual(card["detail"][0], "Top model: anthropic/claude-sonnet-4")

    def test_errors_get_a_line_only_when_there_are_some(self):
        card = build_status_payload({"litellm": self.snapshot(errors_today=3)})["cards"][2]
        self.assertIn("Errors today: 3", card["detail"])

    def test_the_admin_page_lists_all_of_it(self):
        rows = dict(local_readings("litellm", self.snapshot().metrics, NOW))
        self.assertEqual(rows["LiteLLM version"], "1.55.0")
        self.assertEqual(rows["Input tokens"], "8,000")
        self.assertEqual(rows["Output tokens"], "2,868")
        self.assertEqual(rows["Spend today"], "$0.00")
        self.assertEqual(rows["Top model today"], "qwen3.5:9b (2 models used)")


if __name__ == "__main__":
    unittest.main()
