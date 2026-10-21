"""The lazy card detail: the tiles, the endpoint and the small summary.

Plan item 1d, tier 3: a card's tiles are built by the daemon and fetched
when the card is opened, so one source's detail can grow without making
every poll bigger. The summary keeps working for a client that does not ask
for lazy cards, and the whole lazy summary still fits the payload budget.
"""

import json
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, build_opener, ProxyHandler

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from presto_ai_dash import card_tiles
from presto_ai_dash.api import ApiServer
from presto_ai_dash.dashboard import build_card_details, device_view
from presto_ai_dash.models import SourceSnapshot
from presto_ai_dash.quotas import quota_report, quota_window
from test_device_payload import (BUDGET_BYTES, claude_report, codex_report,
                                  full_payload, ollama_cloud_report,
                                  opencode_report, size, sources)
from test_management import ManagementBase

NOW = datetime(2026, 10, 20, 12, tzinfo=timezone.utc)


def windows(*specs):
    return [quota_window(key, label, percent, NOW + reset, NOW - timedelta(hours=2),
                          minutes) for key, label, percent, reset, minutes in specs]


class TileShapeTests(unittest.TestCase):
    """Every tile is inside the shape the device can draw."""

    def assert_shape(self, tiles):
        self.assertLessEqual(len(tiles), card_tiles.MAX_TILES)
        for item in tiles:
            self.assertEqual(set(item), {"label", "value", "tone"})
            self.assertLessEqual(len(item["label"]), card_tiles.MAX_LABEL)
            self.assertLessEqual(len(item["value"]), card_tiles.MAX_VALUE)
            self.assertIn(item["tone"], card_tiles.TONES)
            self.assertTrue(item["label"].isascii())
            self.assertTrue(item["value"].isascii())

    def test_one_tile_is_inside_the_limits(self):
        item = card_tiles.tile("a very long label indeed", "a long value here")
        self.assertEqual(item["label"], "a very l")
        self.assertEqual(item["value"], "a long val")
        self.assertEqual(item["tone"], "normal")

    def test_a_junk_tile_is_dropped(self):
        self.assertIsNone(card_tiles.tile("", "value"))
        self.assertIsNone(card_tiles.tile("label", ""))
        self.assertIsNone(card_tiles.tile("label", None))
        self.assertIsNone(card_tiles.tile(None, "value"))

    def test_an_unknown_tone_becomes_normal(self):
        self.assertEqual(card_tiles.tile("a", "b", "chartreuse")["tone"], "normal")

    def test_a_non_ascii_tile_is_dropped(self):
        self.assertIsNone(card_tiles.tile("café", "12"))


class TileContentTests(unittest.TestCase):
    """The tiles of each source type: only what the report really carries."""

    def tiles(self, source_type, source, now=NOW):
        tiles = card_tiles.tiles_for(source_type, source, now)
        self.assertLessEqual(len(tiles), card_tiles.MAX_TILES)
        for item in tiles:
            self.assertLessEqual(len(item["label"]), card_tiles.MAX_LABEL)
            self.assertLessEqual(len(item["value"]), card_tiles.MAX_VALUE)
            self.assertIn(item["tone"], card_tiles.TONES)
        return tiles

    def test_claude_has_the_last_use_the_tokens_and_the_credit(self):
        report = claude_report()
        source = {"type": "claude", "quota": report, "metrics": {}}
        tiles = self.tiles("claude", source)
        labels = [item["label"] for item in tiles]
        self.assertIn("last use", labels)
        self.assertIn("tok 5h", labels)
        self.assertIn("tok 7d", labels)
        self.assertIn("credit", labels)
        values = {item["label"]: item["value"] for item in tiles}
        self.assertEqual(values["tok 5h"], "1.2M")
        self.assertEqual(values["tok 7d"], "19.4M")
        self.assertEqual(values["credit"], "$100/$100")

    def test_a_claude_without_the_readers_has_no_tiles(self):
        report = quota_report("anthropic", "Claude", windows(
            ("five_hour", "5h", 12, timedelta(hours=3), 300)), 900)
        source = {"type": "claude", "quota": report, "metrics": {}}
        self.assertEqual(self.tiles("claude", source), [])

    def test_litellm_has_the_requests_the_tokens_the_spend_and_the_errors(self):
        source = {"type": "litellm", "quota": {}, "metrics": {
            "requests_today": 12, "tokens_today": 340, "spend_today": 0.5,
            "errors_today": 0, "top_model": "gpt-4o"}}
        tiles = self.tiles("litellm", source)
        values = {item["label"]: item["value"] for item in tiles}
        self.assertEqual(values["requests"], "12")
        self.assertEqual(values["tokens"], "340")
        self.assertEqual(values["spend"], "$0.50")
        self.assertEqual(values["errors"], "0")
        self.assertEqual(values["model"], "gpt-4o")

    def test_litellm_without_metrics_has_no_tiles(self):
        source = {"type": "litellm", "quota": {}, "metrics": {}}
        self.assertEqual(self.tiles("litellm", source), [])

    def test_an_error_is_a_bad_tile(self):
        source = {"type": "litellm", "quota": {}, "metrics": {"errors_today": 3}}
        tiles = self.tiles("litellm", source)
        self.assertEqual(tiles[0]["value"], "3")
        self.assertEqual(tiles[0]["tone"], "bad")

    def test_ollama_has_the_model_the_speed_and_the_count(self):
        source = {"type": "ollama", "quota": {}, "metrics": {
            "loaded_model": "llama3", "last_measured_tokens_per_sec": 38.5,
            "available_models": 5}}
        tiles = self.tiles("ollama", source)
        values = {item["label"]: item["value"] for item in tiles}
        self.assertEqual(values["model"], "llama3")
        self.assertEqual(values["speed"], "38 tok/s")
        self.assertEqual(values["models"], "5")

    def test_codex_has_the_resets_the_expiry_and_the_credits(self):
        source = {"type": "codex", "quota": codex_report(), "metrics": {}}
        tiles = self.tiles("codex", source)
        values = {item["label"]: item["value"] for item in tiles}
        self.assertEqual(values["resets"], "3")
        self.assertEqual(values["next exp"], "10-22")
        self.assertEqual(values["credits"], "0")

    def test_opencode_go_has_no_tiles_because_its_windows_are_quota_rows(self):
        source = {"type": "opencode_go", "quota": opencode_report(), "metrics": {}}
        self.assertEqual(self.tiles("opencode_go", source), [])

    def test_ollama_cloud_carries_its_activity(self):
        source = {"type": "ollama_cloud", "quota": ollama_cloud_report(),
                  "metrics": {}}
        tiles = self.tiles("ollama_cloud", source)
        values = {item["label"]: item["value"] for item in tiles}
        self.assertEqual(values["requests"], "2")
        self.assertEqual(values["tokens"], "84")

    def test_an_unavailable_value_is_omitted_and_never_a_zero(self):
        source = {"type": "litellm", "quota": {}, "metrics": {
            "requests_today": None, "tokens_today": 340, "errors_today": None}}
        tiles = self.tiles("litellm", source)
        labels = [item["label"] for item in tiles]
        self.assertNotIn("requests", labels)
        self.assertNotIn("errors", labels)
        self.assertIn("tokens", labels)


class CardDetailTests(unittest.TestCase):
    """Every card's detail: its tiles and its lines, by card id."""

    def details(self):
        pairs = sources()
        return build_card_details({row["id"]: snap for row, snap in pairs},
                                  [row for row, _ in pairs], now=NOW)

    def test_every_card_has_its_detail(self):
        details = self.details()
        for card_id in ("dash", "go", "cx", "cl", "oc", "ol", "li"):
            self.assertIn(card_id, details)
            self.assertEqual(details[card_id]["id"], card_id)
            self.assertIsInstance(details[card_id]["tiles"], list)
            self.assertIsInstance(details[card_id]["lines"], list)

    def test_the_claude_card_keeps_its_lines_and_gains_tiles(self):
        details = self.details()
        self.assertTrue(details["cl"]["tiles"])
        self.assertTrue(any("Quota via" in line for line in details["cl"]["lines"]))

    def test_a_source_with_nothing_to_say_has_no_tiles(self):
        details = self.details()
        self.assertEqual(details["dash"]["tiles"], [])


class LazySummaryTests(unittest.TestCase):
    """The summary without the card's lines, and the old one unchanged."""

    def payload(self, lazy=False):
        pairs = sources()
        full = full_payload(pairs)
        return full, device_view(full)

    def test_the_lazy_summary_has_no_card_lines(self):
        import copy
        full = full_payload()
        lazy = copy.deepcopy(device_view(full))
        for card in lazy["cards"]:
            card.pop("detail", None)
        for card in lazy["cards"]:
            self.assertNotIn("detail", card)

    def test_the_old_status_is_unchanged(self):
        full = full_payload()
        slim = device_view(full)
        for card in slim["cards"]:
            self.assertIn("detail", card)

    def test_the_lazy_summary_is_inside_the_budget(self):
        """The lazy summary must stay under the payload budget."""
        import copy
        full = full_payload()
        lazy = copy.deepcopy(device_view(full))
        for card in lazy["cards"]:
            card.pop("detail", None)
        lazy["display"] = {"overflow_rows": 1}
        lazy["alerts"] = {"quiet": False, "sound": True, "pop_to_top": True,
                          "enabled": True}
        self.assertLessEqual(size(lazy), BUDGET_BYTES,
                             "lazy payload %d bytes" % size(lazy))
        self.assertLessEqual(size(lazy) / len(lazy["sources"]), 1000)

    def test_the_lazy_summary_is_smaller_than_the_full_one(self):
        import copy
        full = full_payload()
        slim = device_view(full)
        lazy = copy.deepcopy(slim)
        for card in lazy["cards"]:
            card.pop("detail", None)
        self.assertLess(size(lazy), size(slim))


class CardEndpointTests(ManagementBase):
    """GET /api/v1/cards/<id>: one card's tiles and lines."""

    def setUp(self):
        super().setUp()
        self.disable_fake()
        self.row = self.manager.save({"type": "litellm", "name": "Gateway",
                                      "enabled": True,
                                      "config": {"base_url": "http://localhost:4000"}})
        self.storage.save_snapshot(SourceSnapshot(
            self.row["id"], "ok", NOW,
            {"requests_today": 12, "tokens_today": 340, "errors_today": 0,
             "spend_today": 0.5, "top_model": "gpt-4o"}))
        self.api = ApiServer("127.0.0.1", 0, self.storage, "device-token",
                             status_provider=self.manager.status_payload,
                             card_provider=self.manager.card_detail)
        self.api.start()
        self.addCleanup(self.api.stop)
        self.base_url = "http://127.0.0.1:%d" % self.api.actual_port
        self.opener = build_opener(ProxyHandler({}))

    def get(self, path, token="device-token"):
        headers = {"Authorization": "Bearer " + token} if token is not None else {}
        request = Request(self.base_url + path, headers=headers)
        try:
            response = self.opener.open(request, timeout=3)
        except HTTPError as exc:
            response = exc
        with response:
            data = response.read()
            try:
                payload = json.loads(data)
            except ValueError:
                payload = data
            return response.status, payload

    def test_it_returns_the_tiles_and_the_lines(self):
        status, body = self.get("/api/v1/cards/" + self.row["id"])
        self.assertEqual(status, 200)
        self.assertEqual(set(body), {"id", "tiles", "lines"})
        self.assertEqual(body["id"], self.row["id"])
        self.assertTrue(body["tiles"])
        self.assertIsInstance(body["lines"], list)

    def test_the_shared_token_is_required(self):
        for token in (None, "", "wrong-token"):
            with self.subTest(token=token):
                status, _ = self.get("/api/v1/cards/" + self.row["id"], token=token)
                self.assertEqual(status, 401)

    def test_an_unknown_card_is_not_found(self):
        self.assertEqual(self.get("/api/v1/cards/nosuchcard")[0], 404)

    def test_the_id_must_be_strict(self):
        for path in ("/api/v1/cards/with%20space", "/api/v1/cards/a/b",
                     "/api/v1/cards/", "/api/v1/cards/" + "x" * 200,
                     "/api/v1/cards/id?x=1"):
            with self.subTest(path=path):
                self.assertEqual(self.get(path)[0], 404)

    def test_it_changes_nothing(self):
        before = json.dumps(self.storage.source_instances(), sort_keys=True)
        self.manager.changed.clear()
        self.get("/api/v1/cards/" + self.row["id"])
        after = json.dumps(self.storage.source_instances(), sort_keys=True)
        self.assertEqual(after, before)
        self.assertFalse(self.manager.changed.is_set(),
                         "reading a card woke the collector up")

    def test_the_status_is_unchanged_without_the_query(self):
        status, body = self.get("/api/v1/status")
        self.assertEqual(status, 200)
        self.assertTrue(body["cards"])
        for card in body["cards"]:
            self.assertIn("detail", card)

    def test_the_lazy_status_has_no_card_lines(self):
        status, body = self.get("/api/v1/status?cards=lazy")
        self.assertEqual(status, 200)
        for card in body["cards"]:
            self.assertNotIn("detail", card)

    def test_the_lazy_status_is_smaller(self):
        _status, full = self.get("/api/v1/status")
        _status, lazy = self.get("/api/v1/status?cards=lazy")
        self.assertLess(size(lazy), size(full))

    def test_the_lazy_status_is_inside_the_budget(self):
        _status, lazy = self.get("/api/v1/status?cards=lazy")
        self.assertLessEqual(size(lazy), BUDGET_BYTES,
                             "lazy payload %d bytes" % size(lazy))


if __name__ == "__main__":
    unittest.main()
