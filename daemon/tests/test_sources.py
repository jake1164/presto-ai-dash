"""M4/M5 contracts using recorded-shape fixtures and mocked HTTP calls."""

from datetime import datetime, timezone
from pathlib import Path
import json
import sys
import tempfile
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from presto_ai_dash.dashboard import build_status_payload
from presto_ai_dash.http_client import HttpError, HttpUnreachable, get_text
from presto_ai_dash.models import SourceSnapshot
from presto_ai_dash.sources.litellm import LiteLLMSource, UNAVAILABLE
from presto_ai_dash.sources.ollama import OllamaSource, tokens_per_second


class HttpClientTests(unittest.TestCase):
    def test_get_text_accepts_plain_text_health_response(self):
        class Response:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def read(self, size=-1):
                return b"I'm alive!"[:size] if size >= 0 else b"I'm alive!"

            def getcode(self):
                return self.status

        response = Response()
        with patch("presto_ai_dash.http_client._open", return_value=response):
            self.assertEqual(get_text("http://localhost/health/liveliness"),
                             "I'm alive!")


class ProviderHttpSmokeTests(unittest.TestCase):
    def test_sources_collect_through_local_http_without_real_providers(self):
        requests = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                requests.append((self.path, self.headers.get("Authorization")))
                path = urlsplit(self.path).path
                if path == "/api/version":
                    body = {"version": "test-version"}
                elif path == "/api/tags":
                    body = {"models": [{"name": "test-model"}]}
                elif path == "/api/ps":
                    body = {"models": [{"name": "test-model"}]}
                elif path == "/health/liveliness":
                    raw = b"I'm alive!"
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                    return
                elif path == "/health/readiness":
                    body = {"status": "healthy", "db": "connected"}
                elif path == "/user/daily/activity":
                    query = parse_qs(urlsplit(self.path).query)
                    if query.get("start_date") != query.get("end_date"):
                        self.send_error(400, "expected one UTC day")
                        return
                    body = {"results": [{"metrics": {
                        "api_requests": 3, "total_tokens": 50,
                        "spend": 0.25, "failed_requests": 0,
                    }}], "metadata": {"total_api_requests": 3,
                                    "total_spend": 0.25,
                                    "total_prompt_tokens": 40,
                                    "total_completion_tokens": 10}}
                else:
                    self.send_error(404)
                    return
                raw = json.dumps(body).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join)
        self.addCleanup(server.shutdown)
        base_url = "http://127.0.0.1:%d" % server.server_address[1]

        ollama = OllamaSource({"base_url": base_url}).collect_safely()
        litellm = LiteLLMSource({"base_url": base_url,
                                 "api_key": "local-test-key"}).collect_safely()

        self.assertEqual(ollama.status, "active")
        self.assertEqual(ollama.metrics["available_models"], 1)
        self.assertEqual(litellm.metrics["requests_today"], 3)
        self.assertEqual(litellm.metrics["tokens_today"], 50)
        self.assertEqual(litellm.metrics["spend_today"], 0.25)
        daily_path, daily_auth = next(
            (path, auth) for path, auth in requests
            if urlsplit(path).path == "/user/daily/activity")
        query = parse_qs(urlsplit(daily_path).query)
        self.assertEqual(query["start_date"], query["end_date"])
        self.assertEqual(daily_auth, "Bearer local-test-key")


class OllamaTests(unittest.TestCase):
    def test_tokens_per_second_handles_missing_and_zero_duration(self):
        self.assertEqual(tokens_per_second(50, 2_000_000_000), 25.0)
        self.assertIsNone(tokens_per_second(50, 0))
        self.assertIsNone(tokens_per_second(None, 10))

    def test_collects_health_inventory_loaded_model_and_sample(self):
        source = OllamaSource()
        source.samples_file = "sample.json"
        with patch.object(source, "_version", return_value={"version": "0.15.2"}), \
             patch.object(source, "_tags", return_value={"models": [{"name": "llama3"}, {"name": "qwen"}]}), \
             patch.object(source, "_ps", return_value={"models": [{"name": "llama3"}]}), \
             patch.object(source, "read_samples", return_value={
                 "model": "llama3", "eval_count": 90, "eval_duration": 3_000_000_000
             }):
            snapshot = source.collect_safely()
        self.assertEqual(snapshot.status, "active")
        self.assertEqual(snapshot.metrics["version"], "0.15.2")
        self.assertEqual(snapshot.metrics["available_models"], 2)
        self.assertEqual(snapshot.metrics["loaded_model"], "llama3")
        self.assertEqual(snapshot.metrics["last_measured_tokens_per_sec"], 30.0)

    def test_idle_model_is_healthy_and_sample_file_accepts_last_entry(self):
        with tempfile.TemporaryDirectory() as folder:
            sample_path = Path(folder) / "sample.json"
            sample_path.write_text(json.dumps([
                {"eval_count": 1},
                {"model": "qwen", "eval_count": 8, "eval_duration": 2_000_000_000},
            ]), encoding="utf-8")
            source = OllamaSource({"samples_file": str(sample_path)})
            sample = source.read_samples()
        self.assertEqual(sample["model"], "qwen")
        self.assertEqual(sample["eval_count"], 8)
        source = OllamaSource()
        with patch.object(source, "_version", return_value={"version": "1"}), \
             patch.object(source, "_tags", return_value={"models": []}), \
             patch.object(source, "_ps", return_value={"models": []}), \
             patch.object(source, "read_samples", return_value=None):
            snapshot = source.collect()
        self.assertEqual(snapshot.status, "ok")
        self.assertEqual(snapshot.metrics["last_measured"], "unavailable")

    def test_unreachable_is_offline_and_optional_probes_degrade_independently(self):
        source = OllamaSource()
        with patch.object(source, "_version", side_effect=HttpUnreachable("refused")):
            self.assertEqual(source.collect().status, "offline")
        with patch.object(source, "_version", return_value={}), \
             patch.object(source, "_tags", side_effect=HttpError(500, "tags")), \
             patch.object(source, "_ps", side_effect=HttpError(500, "ps")), \
             patch.object(source, "read_samples", return_value=None):
            self.assertEqual(source.collect().status, "ok")


class LiteLLMTests(unittest.TestCase):
    def test_auth_header_and_today_query(self):
        source = LiteLLMSource({"base_url": "http://gateway", "api_key": "secret"})
        seen = {}

        def fake_get(url, timeout, headers=None):
            seen.update(url=url, headers=headers, timeout=timeout)
            return {"results": [], "metadata": {"total_api_requests": 0,
                                                    "total_prompt_tokens": 0,
                                                    "total_completion_tokens": 0,
                                                    "total_spend": 0}}

        class FixedDate(datetime):
            @classmethod
            def now(cls, tz=None):
                return cls(2026, 10, 10, tzinfo=timezone.utc)

        with patch("presto_ai_dash.sources.litellm.get_json", side_effect=fake_get), \
             patch("presto_ai_dash.sources.litellm.datetime", FixedDate), \
             patch.object(source, "_liveliness", return_value="alive"), \
             patch.object(source, "_readiness", return_value={}):
            source.collect()
        self.assertEqual(seen["url"], "http://gateway/user/daily/activity?start_date=2026-10-10&end_date=2026-10-10")
        self.assertEqual(seen["headers"], {"Authorization": "Bearer secret"})

    def test_collection_health_usage_metadata_and_model(self):
        source = LiteLLMSource({"api_key": "secret"})
        with patch.object(source, "_liveliness", return_value="I'm alive!"), \
             patch.object(source, "_readiness", return_value={"status": "healthy", "db": "connected"}), \
             patch.object(source, "_daily_activity", return_value={
                 "results": [{"date": "2026-10-10", "metrics": {
                     "spend": 1.25, "api_requests": 4, "total_tokens": 120,
                     "failed_requests": 1,
                 }, "breakdown": {"models": {"gpt-x": {"metrics": {"spend": 1.25}}}}}],
                 "metadata": {"total_spend": 1.25, "total_api_requests": 4,
                              "total_prompt_tokens": 100, "total_completion_tokens": 20},
             }):
            snapshot = source.collect()
        self.assertEqual(snapshot.status, "ok")
        self.assertEqual(snapshot.alerts, [])
        self.assertEqual(snapshot.metrics["database"], "connected")
        self.assertEqual(snapshot.metrics["requests_today"], 4)
        self.assertEqual(snapshot.metrics["tokens_today"], 120)
        self.assertEqual(snapshot.metrics["spend_today"], 1.25)
        self.assertEqual(snapshot.metrics["top_model"], "gpt-x")
        self.assertEqual(snapshot.metrics["errors_today"], 1)
        card = build_status_payload({"litellm": snapshot})["cards"][2]
        self.assertEqual(card["status"], "ok")
        self.assertEqual(card["accent"], "green")
        self.assertIn("Errors today: 1", card["detail"])
        self.assertIsNone(card["alert"])

    def test_unknown_metrics_are_unavailable_not_fabricated_zero(self):
        source = LiteLLMSource()
        metrics = {}
        source._merge_activity({"results": [{"metrics": {}}]}, metrics, [])
        for key in ("requests_today", "tokens_today", "spend_today", "errors_today", "top_model"):
            self.assertEqual(metrics[key], UNAVAILABLE)

    def test_empty_results_with_metadata_report_real_zeroes(self):
        source = LiteLLMSource()
        metrics = {}
        source._merge_activity({"results": [], "metadata": {
            "total_spend": 0, "total_api_requests": 0,
            "total_prompt_tokens": 0, "total_completion_tokens": 0,
        }}, metrics, [])
        self.assertEqual(metrics["requests_today"], 0)
        self.assertEqual(metrics["tokens_today"], 0)
        self.assertEqual(metrics["spend_today"], 0.0)

    def test_liveness_http_errors_and_activity_auth_failure(self):
        source = LiteLLMSource()
        with patch.object(source, "_liveliness", side_effect=HttpUnreachable("refused")):
            self.assertEqual(source.collect().status, "offline")
        with patch.object(source, "_liveliness", return_value="alive"), \
             patch.object(source, "_readiness", side_effect=HttpUnreachable("no db")), \
             patch.object(source, "_daily_activity", side_effect=HttpError(401, "bad key")):
            snapshot = source.collect()
        self.assertEqual(snapshot.status, "warning")
        self.assertEqual(snapshot.metrics["usage"], UNAVAILABLE)
        self.assertIn("rejected the API key", snapshot.alerts[0]["message"])

    def test_current_permission_warning_clears_with_historical_errors_retained(self):
        source = LiteLLMSource()
        with patch.object(source, "_liveliness", return_value="alive"), \
             patch.object(source, "_readiness", return_value={"db": "connected"}), \
             patch.object(source, "_daily_activity", side_effect=[
                 HttpError(403, "route denied"),
                 {"results": [{"metrics": {"api_requests": 3,
                                            "failed_requests": 2}}]},
             ]):
            denied = source.collect()
            recovered = source.collect()
        self.assertEqual(denied.status, "warning")
        self.assertEqual(denied.metrics["usage"], UNAVAILABLE)
        self.assertIn("lacks permission", denied.alerts[0]["message"])
        self.assertEqual(recovered.status, "ok")
        self.assertEqual(recovered.alerts, [])
        self.assertEqual(recovered.metrics["errors_today"], 2)
        with patch.object(source, "_liveliness", side_effect=HttpError(503, "unhealthy")):
            unhealthy = source.collect()
        self.assertEqual(unhealthy.status, "critical")
        self.assertEqual(unhealthy.alerts[0]["severity"], "critical")


class LiveDashboardTests(unittest.TestCase):
    def test_live_cards_keep_contract_and_disabled_provider_is_explicit(self):
        moment = datetime(2026, 10, 10, tzinfo=timezone.utc)
        ollama = SourceSnapshot("ollama", "active", moment, {
            "loaded_model": "llama3", "available_models": 2,
            "version": "0.15.2", "last_measured_tokens_per_sec": 22.5,
        })
        payload = build_status_payload({"ollama": ollama})
        self.assertEqual(payload["overall_status"], "active")
        self.assertEqual([card["id"] for card in payload["cards"]],
                         ["dash", "ollama", "litellm"])
        self.assertEqual(payload["cards"][1]["primary"], "llama3")
        self.assertEqual(payload["cards"][2]["primary"], "Not configured")
        self.assertEqual(payload["updated_at"], moment.isoformat())


if __name__ == "__main__":
    unittest.main()
