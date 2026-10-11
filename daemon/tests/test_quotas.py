"""Cloud allowance semantics, authentic failure states, UI/API and device contracts."""

from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from presto_ai_dash.config import load_config, ConfigError
from presto_ai_dash.dashboard import build_instance_payload, build_status_payload
from presto_ai_dash.http_client import get_json, HttpError, HttpUnreachable
from presto_ai_dash.models import SourceSnapshot
from presto_ai_dash.quotas import quota_report, quota_window, present_quota
from presto_ai_dash.sources.opencode_go import OpenCodeGoSource, USAGE_URL
from presto_ai_dash.sources.ollama_cloud import OllamaCloudSource
from test_management import ManagementBase
import presto_path
from home import source_rows, HomeScroll, source_row_height
from ui import Palette, draw_sources, PANEL
from test_ui import Display
import test_home

NOW = datetime(2026, 10, 11, 14, tzinfo=timezone.utc)
RESET = (NOW + timedelta(days=2)).isoformat()


def go_response(percent=12):
    return {"usage": {key: {"status": "ok", "percent": percent + offset, "resetsAt": RESET}
                      for offset, key in enumerate(("rolling", "weekly", "monthly"))}}


def cloud_response():
    return {"from": (NOW - timedelta(days=7)).isoformat(), "until": NOW.isoformat(),
            "totals": {"request_count": 2, "usage_usd": 0.00002,
                       "input_tokens": 52, "cached_input_tokens": 0, "output_tokens": 32}}


class QuotaContractTests(unittest.TestCase):
    def test_zero_missing_unlimited_and_over_limit_are_distinct(self):
        self.assertEqual(quota_window("a", "5h", 0, RESET, NOW)["used_percent"], 0)
        self.assertEqual(quota_window("a", "5h", None, None, NOW)["state"], "unavailable")
        self.assertIsNone(quota_window("a", "5h", 12, None, NOW, state="unlimited")["used_percent"])
        self.assertEqual(quota_window("a", "5h", 125, RESET, NOW)["used_percent"], 125)
        for value in (-1, True, "12", float("nan"), float("inf")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                quota_window("a", "5h", value, RESET, NOW)

    def test_stale_and_reset_do_not_fabricate_fresh_zeroes_or_mutate_history(self):
        report = quota_report("test", "Test", [quota_window("a", "Weekly", 41, RESET, NOW)])
        stale = present_quota(report, NOW + timedelta(seconds=181))
        self.assertEqual(stale["windows"][0]["state"], "stale")
        self.assertEqual(stale["windows"][0]["used_percent"], 41)
        reset = present_quota(report, NOW + timedelta(days=2))
        self.assertIn("reset", reset["windows"][0]["reason"])
        self.assertEqual(report["windows"][0]["state"], "available")
        self.assertEqual(present_quota(report, NOW)["windows"][0]["state"], "available")

    def test_duplicate_windows_and_naive_timestamps_are_rejected(self):
        window = quota_window("a", "Monthly", 30, RESET, NOW)
        with self.assertRaises(ValueError):
            quota_report("test", "test", [window, window])
        with self.assertRaises(ValueError):
            quota_window("a", "Weekly", 1, "2026-10-12T00:00:00", NOW)

    def test_registered_cloud_sources_load_and_demo_exclusivity_is_preserved(self):
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cloud.toml"
            path.write_text('[sources.fake]\nenabled=false\n[sources.opencode_go]\nenabled=true\n', encoding="utf-8")
            self.assertTrue(load_config(path)["sources"]["opencode_go"]["enabled"])
            path.write_text('[sources.opencode_go]\nenabled=true\n', encoding="utf-8")
            with self.assertRaises(ConfigError):
                load_config(path)


class CloudSourceTests(unittest.TestCase):
    def test_go_windows_preserve_labels_provider_reset_and_missing_windows(self):
        source = OpenCodeGoSource()
        report, limited = source.parse(go_response(0), NOW)
        self.assertFalse(limited)
        self.assertEqual([w["label"] for w in report["windows"]], ["5h", "Weekly", "Monthly"])
        self.assertEqual(report["windows"][0]["used_percent"], 0)
        self.assertIsNone(report["windows"][2]["period_minutes"])
        self.assertEqual(report["windows"][0]["resets_at"], RESET)
        data = go_response()
        data["usage"]["rolling"]["status"] = "rate-limited"
        del data["usage"]["monthly"]
        report, limited = source.parse(data, NOW)
        self.assertTrue(limited)
        self.assertEqual(report["windows"][2]["state"], "unavailable")

    def test_go_changed_schema_or_invalid_window_fails_honestly(self):
        for data in ({"usage": {}}, {"other": "value"},
                     {"usage": {"rolling": {"status": "ok", "percent": True, "resetsAt": RESET}}},
                     {"usage": {"rolling": {"status": "ok", "percent": 2}}}):
            with self.subTest(data=data), self.assertRaises(ValueError):
                OpenCodeGoSource().parse(data, NOW)

    def test_fixed_https_read_cache_failures_redaction_and_recovery(self):
        source = OpenCodeGoSource({"api_key": "fixture-key", "refresh_seconds": 60})
        with patch("presto_ai_dash.sources._quota_http.get_json", return_value=go_response()) as get:
            first = source.collect_safely()
            cached = source.collect_safely()
            self.assertEqual(first.updated_at, cached.updated_at)
            self.assertEqual(get.call_count, 1)
            self.assertEqual(get.call_args.args, (USAGE_URL,))
            self.assertFalse(get.call_args.kwargs["allow_redirects"])
            self.assertEqual(get.call_args.kwargs["headers"],
                             {"Authorization": "Bearer fixture-key", "Accept": "application/json",
                              "User-Agent": "presto-ai-dash/0.1"})
        source._next_probe = 0
        with patch("presto_ai_dash.sources._quota_http.get_json", side_effect=HttpError(401, "fixture-key")):
            failed = source.collect_safely()
        self.assertEqual(failed.status, "offline")
        self.assertNotIn("fixture-key", json.dumps(failed.to_dict()))
        report = present_quota(failed.metrics["quota"], NOW)
        self.assertEqual(report["windows"][0]["state"], "stale")
        self.assertEqual(report["windows"][0]["used_percent"], 12)
        source._next_probe = 0
        with patch("presto_ai_dash.sources._quota_http.get_json", return_value=go_response(23)):
            recovered = source.collect_safely()
        self.assertEqual(recovered.status, "ok")
        self.assertIsNone(recovered.metrics["quota"]["error"])
        self.assertEqual(recovered.metrics["quota"]["windows"][0]["used_percent"], 23)

    def test_missing_key_auth_entitlement_network_and_schema_failures(self):
        with patch("presto_ai_dash.sources._quota_http.get_json") as get:
            self.assertEqual(OpenCodeGoSource().collect().status, "offline")
            get.assert_not_called()
        for failure in (HttpError(403), HttpError(429), HttpUnreachable("refused"), ValueError("changed")):
            with self.subTest(failure=type(failure).__name__), \
                    patch("presto_ai_dash.sources._quota_http.get_json", side_effect=failure):
                snapshot = OpenCodeGoSource({"api_key": "key"}).collect()
                self.assertTrue(all(w["state"] == "unavailable" for w in snapshot.metrics["quota"]["windows"]))

    def test_cloud_activity_is_not_a_monthly_allowance_and_private_fields_are_dropped(self):
        data = cloud_response()
        data["email"] = "must-not-be-stored@example.test"
        data["buckets"] = [{"model": "private model name"}]
        report, limited = OllamaCloudSource().parse(data, NOW)
        self.assertFalse(limited)
        self.assertEqual(report["activity"]["usage_usd"], 0.00002)
        self.assertEqual(report["activity"]["request_count"], 2)
        window = report["windows"][0]
        self.assertEqual(window["label"], "Monthly credits")
        self.assertEqual(window["state"], "unavailable")
        self.assertIsNone(window["used_percent"])
        self.assertNotIn("limit_amount", window)
        self.assertNotIn("private model", json.dumps(report))
        self.assertNotIn("@example", json.dumps(report))
        self.assertEqual(present_quota(report, NOW + timedelta(minutes=4))["activity"]["state"], "stale")

    def test_cloud_malformed_or_legacy_limits_are_not_treated_as_current_credit_usage(self):
        data = cloud_response()
        data["totals"]["usage_usd"] = -1
        for value in (data, {"limits": {"weekly": {"usage": 1}}}, {}):
            with self.assertRaises(ValueError):
                OllamaCloudSource().parse(value, NOW)


class QuotaStorageTests(ManagementBase):
    def test_every_enabled_account_has_its_own_card_and_edits_keep_identity(self):
        self.disable_fake()
        rows = [self.manager.save({"type": type_id, "name": name, "enabled": True})
                for type_id, name in (("opencode_go", "Go personal"), ("opencode_go", "Go other"),
                                      ("ollama_cloud", "Cloud"), ("ollama", "Desktop"))]
        for index, row in enumerate(rows):
            if row["type"] == "opencode_go":
                report, _ = OpenCodeGoSource().parse(go_response(index * 20), NOW)
                metrics = {"quota": report}
            elif row["type"] == "ollama_cloud":
                report, _ = OllamaCloudSource().parse(cloud_response(), NOW)
                metrics = {"quota": report}
            else:
                metrics = {"loaded_model": "Observed model"}
            self.storage.save_snapshot(SourceSnapshot(row["id"], "ok", metrics=metrics))
        with patch("presto_ai_dash.quotas.now_utc", return_value=NOW):
            payload = self.manager.status_payload()
            self.assertEqual(payload["card_order"], "accounts")
            self.assertEqual([c["id"] for c in payload["cards"]], ["dash"] + [r["id"] for r in rows])
            self.assertEqual([c["title"] for c in payload["cards"][1:]], [r["name"] for r in rows])
            self.assertEqual(payload["cards"][1]["primary"], "5h: 0%")
            self.assertEqual(payload["cards"][2]["primary"], "5h: 20%")
            self.assertEqual(payload["cards"][3]["primary"], "2 requests")
            self.assertTrue(any("Monthly reset" in line for line in payload["cards"][1]["detail"]))
            self.manager.save({"revision": rows[0]["revision"], "enabled": False}, rows[0]["id"])
            self.manager.save({"revision": rows[1]["revision"], "name": "Renamed Go",
                               "api_key": "replacement"}, rows[1]["id"])
            self.manager.delete(rows[2]["id"], rows[2]["revision"])
            changed = self.manager.status_payload()["cards"]
        self.assertEqual([c["id"] for c in changed], ["dash", rows[1]["id"], rows[3]["id"]])
        self.assertEqual(changed[1]["title"], "Renamed Go")

    def test_new_connector_without_a_special_card_still_appears(self):
        payload = build_instance_payload({"new-account": SourceSnapshot("new-account", "ok")},
            [{"id": "new-account", "type": "new_connector", "name": "New AI", "enabled": True}])
        self.assertEqual([c["title"] for c in payload["cards"]], ["AI Dash", "New AI"])
        self.assertEqual(payload["cards"][1]["primary"], "Usage unavailable")

    def test_separate_accounts_keys_restart_and_public_api_projection(self):
        self.disable_fake()
        rows = [self.manager.save({"type": "opencode_go", "name": name, "enabled": True,
                                   "api_key": key}) for name, key in (("Personal", "key-one"), ("Other", "key-two"))]
        for index, row in enumerate(rows):
            report, _ = OpenCodeGoSource().parse(go_response(index * 30), NOW)
            self.storage.save_snapshot(SourceSnapshot(row["id"], "ok", metrics={"quota": report}))
        with patch("presto_ai_dash.quotas.now_utc", return_value=NOW):
            payload = self.manager.status_payload()
            self.assertEqual(payload["quota_schema_version"], 1)
            self.assertEqual([row["quota"]["windows"][0]["used_percent"] for row in payload["sources"]], [0, 30])
            self.assertNotIn("key-one", json.dumps(payload))
            self.assertEqual(len(payload["cards"]), 3)
        self.storage.close()
        from presto_ai_dash.storage import Storage
        self.storage = Storage(self.database)
        self.addCleanup(self.storage.close)
        from presto_ai_dash.management import SourceConfiguration
        restarted = SourceConfiguration(self.storage, self.config)
        with patch("presto_ai_dash.quotas.now_utc", return_value=NOW + timedelta(minutes=4)):
            aged = restarted.status_payload()
        self.assertEqual(aged["overall_status"], "warning")
        self.assertEqual(aged["sources"][0]["quota"]["windows"][0]["state"], "stale")
        self.assertEqual(self.storage.latest_snapshots()[rows[0]["id"]].metrics["quota"]["windows"][0]["state"], "available")

    def test_unmanaged_cloud_source_also_has_a_status_response(self):
        report, _ = OpenCodeGoSource().parse(go_response(), NOW)
        with patch("presto_ai_dash.quotas.now_utc", return_value=NOW):
            payload = build_status_payload({"opencode_go": SourceSnapshot("opencode_go", "ok", metrics={"quota": report})})
        self.assertEqual(payload["sources"][0]["quota"]["product"], "Go")


class QuotaDeviceTests(unittest.TestCase):
    def test_many_card_indicators_fit_and_keep_selected_indicator_visible(self):
        from ui import _draw_dots, INK
        display = Display()
        circles = []
        display.circle = lambda x, y, radius: circles.append((display.pen, x, y, radius))
        for index in (0, 1, 16, 39):
            circles.clear()
            _draw_dots(display, Palette(display), index, 40, INK, 240, 240)
            self.assertEqual(len(circles), 9)
            self.assertEqual(sum(pen == INK for pen, *_ in circles), 1)
            self.assertTrue(all(8 <= x - r and x + r <= 232 for _, x, _, r in circles))

    def test_all_windows_and_accounts_have_scrollable_rows_with_real_labels(self):
        report, _ = OpenCodeGoSource().parse(go_response(), NOW)
        report = present_quota(report, NOW)
        rows = source_rows({"sources": [{"id": key, "name": key, "type": "opencode_go", "status": "ok", "quota": report}
                                       for key in ("Account one", "Account two", "Account three")]})
        self.assertEqual(len(rows), 3)
        self.assertEqual([w["label"] for w in rows[0]["windows"]], ["5h", "Weekly", "Monthly"])
        self.assertEqual(HomeScroll(len(rows), source_row_height(rows)).limit, 81)
        for offset in (0, 25, 81):
            with self.subTest(offset=offset):
                display = Display()
                draw_sources(display, Palette(display), rows, 0, 3, scroll=offset)
                test_home.HomeLayoutTests().assert_layout(display)
                self.assertTrue(any(text.startswith("3 sources") for text, *_ in display.rows))
                self.assertIn("Monthly 14%", [text for text, *_ in display.rows])
        self.assertTrue(all(w["percent"] is not None for row in rows for w in row["windows"]))

    def test_missing_stale_and_cloud_activity_do_not_draw_fresh_zero_bars(self):
        report, _ = OllamaCloudSource().parse(cloud_response(), NOW)
        rows = source_rows({"sources": [{"id": "local", "type": "ollama", "name": "Local", "status": "ok"},
                                        {"id": "cloud", "type": "ollama_cloud", "name": "Cloud", "status": "ok",
                                         "quota": present_quota(report, NOW)}]})
        self.assertEqual([r["id"] for r in rows], ["cloud", "local"])
        self.assertEqual(rows[0]["windows"][0]["state"], "unavailable")
        self.assertIn("2 req", rows[0]["activity"])
        display = Display()
        draw_sources(display, Palette(display), rows, 0, 3)
        self.assertIn("Monthly credits unavailable", [text for text, *_ in display.rows])
        self.assertFalse(any(args[3] == 5 and color != PANEL for color, args in display.rectangles))


class ReportingRedirectTests(unittest.TestCase):
    def test_read_only_client_does_not_forward_key_to_redirect_target(self):
        received = []
        class Target(BaseHTTPRequestHandler):
            def log_message(self, *_): pass
            def do_GET(self):
                received.append(self.headers.get("Authorization"))
                self.send_response(200); self.end_headers(); self.wfile.write(b'{}')
        target = ThreadingHTTPServer(("127.0.0.1", 0), Target)
        class Redirect(BaseHTTPRequestHandler):
            def log_message(self, *_): pass
            def do_GET(self):
                self.send_response(302)
                self.send_header("Location", "http://127.0.0.1:%d/" % target.server_port)
                self.end_headers()
        redirect = ThreadingHTTPServer(("127.0.0.1", 0), Redirect)
        for server in (target, redirect):
            thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
            self.addCleanup(server.server_close); self.addCleanup(thread.join); self.addCleanup(server.shutdown)
        with self.assertRaises(HttpError) as caught:
            get_json("http://127.0.0.1:%d/" % redirect.server_port,
                     headers={"Authorization": "Bearer fixture-only"}, allow_redirects=False)
        self.assertEqual(caught.exception.status, 302)
        self.assertEqual(received, [])


if __name__ == "__main__":
    unittest.main()
