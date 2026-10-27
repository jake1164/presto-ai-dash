"""What the Ollama source reads from /api/ps and /api/tags, and how a card shows it."""

from datetime import datetime, timezone
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "presto"))

from presto_ai_dash.card_tiles import gigabytes, local_readings, tiles_for
from presto_ai_dash.sources import ollama
from presto_ai_dash.sources.ollama import OllamaSource, loaded_facts

NOW = datetime(2026, 10, 27, 11, 0, tzinfo=timezone.utc)

LOADED = {"name": "gemma4-12b-32k:latest", "size": 8421166283, "size_vram": 8421166283,
          "context_length": 32768, "expires_at": "2026-10-27T07:18:18.4163581-04:00",
          "details": {"parameter_size": "11.9B", "quantization_level": "Q4_K_M"}}


class LoadedFactsTests(unittest.TestCase):
    def test_a_model_fully_on_the_gpu(self):
        facts = loaded_facts([LOADED])
        self.assertEqual(facts["gpu_percent"], 100)
        self.assertEqual(facts["loaded_memory_bytes"], 8421166283)
        self.assertEqual(facts["loaded_context"], 32768)
        self.assertEqual(facts["loaded_expires_at"], "2026-10-27T11:18:18.416358+00:00")
        self.assertEqual((facts["loaded_params"], facts["loaded_quant"]), ("11.9B", "Q4_K_M"))

    def test_a_partial_offload_and_several_models_share_one_percentage(self):
        facts = loaded_facts([dict(LOADED, size=1000, size_vram=500),
                              dict(LOADED, size=3000, size_vram=3000)])
        self.assertEqual(facts["gpu_percent"], 88)  # 3500 of 4000
        self.assertEqual(facts["loaded_memory_bytes"], 4000)

    def test_a_cpu_only_model_is_zero_percent_not_missing(self):
        self.assertEqual(loaded_facts([dict(LOADED, size_vram=0)])["gpu_percent"], 0)

    def test_what_ollama_did_not_send_is_left_out(self):
        self.assertEqual(loaded_facts([{"name": "x"}]), {})
        self.assertEqual(loaded_facts([]), {})
        facts = loaded_facts([{"size": True, "size_vram": "8", "context_length": -1,
                               "expires_at": "tomorrow", "details": {"parameter_size": "é"}}])
        self.assertEqual(facts, {})


class CollectTests(unittest.TestCase):
    def collect(self, ps, tags):
        source = OllamaSource({"base_url": "http://x"})
        responses = {"/api/version": {"version": "0.40.11"}, "/api/ps": ps, "/api/tags": tags}
        with patch.object(ollama, "get_json", side_effect=lambda url, **kw: responses[
                url[len("http://x"):]]):
            return source.collect()

    def test_disk_use_adds_up_every_model(self):
        snapshot = self.collect({"models": [LOADED]},
                                {"models": [{"size": 2_000_000_000}, {"size": 3_000_000_000}]})
        self.assertEqual(snapshot.metrics["disk_bytes"], 5_000_000_000)
        self.assertEqual(snapshot.metrics["available_models"], 2)
        self.assertEqual(snapshot.status, "active")

    def test_nothing_loaded_reports_no_memory_figures(self):
        snapshot = self.collect({"models": []}, {"models": [{"size": 1}]})
        self.assertNotIn("gpu_percent", snapshot.metrics)
        self.assertEqual(snapshot.status, "ok")


class TileTests(unittest.TestCase):
    def tiles(self, **metrics):
        return {t["label"]: t for t in tiles_for("ollama", {"metrics": metrics}, NOW)}

    def test_the_card_says_where_the_model_runs_and_what_it_holds(self):
        tiles = self.tiles(loaded_model="gemma4", gpu_percent=100, loaded_memory_bytes=8421166283,
                           loaded_context=32768, loaded_expires_at="2026-10-27T11:05:00+00:00",
                           available_models=7)
        self.assertEqual({k: v["value"] for k, v in tiles.items()},
                         {"model": "gemma4", "gpu": "100%", "memory": "8.4 GB",
                          "context": "32k", "unloads": "5m", "models": "7"})

    def test_a_partial_gpu_share_is_a_warning_and_a_pinned_model_is_kept(self):
        tiles = self.tiles(gpu_percent=60, loaded_expires_at="2318-01-01T00:00:00+00:00")
        self.assertEqual(tiles["gpu"]["tone"], "warn")
        self.assertEqual(tiles["unloads"]["value"], "kept")

    def test_sizes(self):
        self.assertEqual(gigabytes(8421166283), "8.4 GB")
        self.assertEqual(gigabytes(58_500_000_000), "58 GB")


class ReadingsTests(unittest.TestCase):
    def test_the_admin_page_lists_what_ollama_reported_in_words(self):
        rows = dict(local_readings("ollama", {
            "version": "0.40.11", "available_models": 7, "disk_bytes": 58_500_000_000,
            "loaded_model": "gemma4-12b-32k:latest", "loaded_params": "11.9B",
            "loaded_quant": "Q4_K_M", "loaded_memory_bytes": 8421166283, "gpu_percent": 100,
            "loaded_context": 32768, "loaded_expires_at": "2026-10-27T11:47:00+00:00"}, NOW))
        self.assertEqual(rows["Models installed"], "7")
        self.assertEqual(rows["Disk used by models"], "58 GB")
        self.assertEqual(rows["Size and quantisation"], "11.9B Q4_K_M")
        self.assertEqual(rows["Memory in use"], "8.4 GB")
        self.assertEqual(rows["On the GPU"], "100%")
        self.assertEqual(rows["Context length"], "32,768 tokens")
        self.assertEqual(rows["Unloads"], "in 47m")

    def test_nothing_loaded_lists_no_memory_rows_and_no_speed_is_not_a_row(self):
        rows = dict(local_readings("ollama", {"available_models": 3, "version": "1"}, NOW))
        self.assertEqual(rows["Loaded model"], "none")
        self.assertNotIn("Memory in use", rows)
        self.assertNotIn("Last measured speed", rows)

    def test_litellm_leaves_out_what_it_could_not_read(self):
        rows = dict(local_readings("litellm", {
            "online": True, "database": "connected", "requests_today": 12,
            "tokens_today": "unavailable", "spend_today": 0.5, "top_model": "unavailable"}, NOW))
        self.assertEqual(rows, {"Proxy": "online", "Database": "connected",
                                "Requests today": "12", "Spend today": "$0.50"})

    def test_another_service_has_no_rows(self):
        self.assertEqual(local_readings("claude", {"online": True}, NOW), [])


if __name__ == "__main__":
    unittest.main()
