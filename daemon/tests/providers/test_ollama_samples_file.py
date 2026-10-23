"""The Ollama samples file: a small plain file, read once, never past a limit."""

import json
import logging
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from presto_ai_dash.sources import ollama
from presto_ai_dash.sources.ollama import OllamaSource, SAMPLES_FILE_LIMIT


class SamplesFileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)

    def source(self, path):
        return OllamaSource({"samples_file": str(path)})

    def test_an_ordinary_sample_is_read(self):
        path = self.folder / "samples.json"
        path.write_text(json.dumps({"eval_count": 5, "ignored": "x"}), encoding="utf-8")
        sample = self.source(path).read_samples()
        self.assertEqual(sample.get("eval_count"), 5)
        self.assertNotIn("ignored", sample)

    def test_the_last_of_a_list_is_used(self):
        path = self.folder / "samples.json"
        path.write_text(json.dumps([{"eval_count": 1}, {"eval_count": 2}]), encoding="utf-8")
        self.assertEqual(self.source(path).read_samples().get("eval_count"), 2)

    def test_a_file_over_the_limit_is_not_read(self):
        path = self.folder / "big.json"
        path.write_text('{"eval_count": 1, "pad": "' + "x" * SAMPLES_FILE_LIMIT + '"}',
                        encoding="utf-8")
        with self.assertLogs(ollama.log, level=logging.WARNING) as logs:
            with patch("builtins.open", side_effect=AssertionError("must not open")):
                self.assertIsNone(self.source(path).read_samples())
        self.assertIn("larger than", "\n".join(logs.output))

    def test_a_file_just_under_the_limit_is_read(self):
        body = '{"eval_count": 7, "pad": "'
        pad = "x" * (SAMPLES_FILE_LIMIT - len(body) - 2)
        path = self.folder / "edge.json"
        path.write_text(body + pad + '"}', encoding="utf-8")
        self.assertLessEqual(path.stat().st_size, SAMPLES_FILE_LIMIT)
        self.assertEqual(self.source(path).read_samples().get("eval_count"), 7)

    def test_a_directory_is_not_a_samples_file(self):
        with self.assertLogs(ollama.log, level=logging.WARNING) as logs:
            self.assertIsNone(self.source(self.folder).read_samples())
        self.assertIn("not a regular file", "\n".join(logs.output))

    def test_a_missing_file_is_still_quietly_none(self):
        self.assertIsNone(self.source(self.folder / "absent.json").read_samples())

    def test_no_setting_reads_nothing(self):
        self.assertIsNone(OllamaSource({"samples_file": ""}).read_samples())


if __name__ == "__main__":
    unittest.main()
