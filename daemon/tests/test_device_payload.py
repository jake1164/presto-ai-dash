"""The status payload the Presto fetches carries only what the device reads.

The device has little memory, and "Daemon offline" on the Presto can be a failed
read or parse of a payload that grew. The daemon therefore sends a device view:
the fields presto/ actually uses, each quota report once. These tests prove the
device draws exactly the same screens from the slim payload as from the full
one, that the payload stays inside a size budget, and that the whitelist cannot
drift from what the device code reads.
"""

import ast
from datetime import datetime, timedelta, timezone
import json
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

import presto_path  # noqa: F401  (adds presto/ to sys.path)
from presto_ai_dash.dashboard import (DEVICE_ACTIVITY_KEYS, DEVICE_ALERT_KEYS,
                                      DEVICE_METRICS_KEYS, DEVICE_QUOTA_KEYS,
                                      DEVICE_RESET_KEYS, DEVICE_SOURCE_KEYS,
                                      DEVICE_TOP_KEYS, DEVICE_WINDOW_KEYS,
                                      build_instance_payload, device_view)
from presto_ai_dash.models import SourceSnapshot
from presto_ai_dash.quotas import quota_report, quota_window
import cards
import home
import ui
from test_ui import Display

NOW = datetime(2026, 10, 20, 12, tzinfo=timezone.utc)
DAY = timedelta(days=1)
# What the author's six accounts (both Claude readers on) may cost on the wire.
# The device accepts 65536 bytes but parses them in a few hundred KB of RAM.
BUDGET_BYTES = 5000
BUDGET_PER_SOURCE = 1000

ADMIN_ONLY_QUOTA_KEYS = ("note", "usage_cache", "local_activity", "collector_seen_at",
                         "collector_host", "collector_version", "last_progress_at",
                         "provider", "product", "credits", "stale_after_seconds",
                         "active_seconds", "warn_on_stale")


def size(value):
    return len(json.dumps(value, separators=(",", ":")))


def windows(*specs):
    return [quota_window(key, label, percent, NOW + reset, NOW - timedelta(hours=2),
                         minutes) for key, label, percent, reset, minutes in specs]


def claude_report():
    report = quota_report("anthropic", "Claude", windows(
        ("five_hour", "5h", 12, timedelta(hours=3), 300),
        ("seven_day", "Weekly", 66, 2 * DAY, 10080)), 900)
    report.update(
        note="Claude Code terminal export and its local usage cache; idle readings "
             "become stale. The VS Code panel does not run the status line, so quota "
             "arrives from terminal sessions and from the usage cache.",
        usage_cache={"available": True, "reason": None, "message": "ok",
                     "path": "C:\\Users\\someone\\.claude.json", "fetched_at": NOW.isoformat(),
                     "credit": {"limit": 100, "used": 0, "currency": "USD",
                                "resets_at": (NOW + 16 * DAY).isoformat()},
                     "account_changed": False,
                     "refreshes": [(NOW - timedelta(hours=h)).isoformat() for h in range(20)],
                     "since": NOW.isoformat()},
        local_activity={"available": True, "complete": True,
                        "last_request_at": (NOW - timedelta(minutes=3)).isoformat(),
                        "last_model": "claude-sonnet-5-5", "last_entrypoint": "VS Code",
                        "requests_5h": 62, "tokens_5h": 1201721, "tokens_7d": 19431353},
        collector_seen_at=None, collector_host=None, collector_version=None,
        last_progress_at=None, active_seconds=300, warn_on_stale=False)
    return report


def codex_report():
    report = quota_report("openai", "Codex", windows(
        ("codex:primary", "Weekly", 2, 6 * DAY, 10080)), 180)
    report["windows"].append(quota_window("codex:secondary", "Secondary", None, None,
                                          NOW, None, state="unavailable"))
    report["reset_credits"] = {"available": 3, "expires_at": [
        (NOW + 2 * DAY).isoformat(), (NOW + 9 * DAY).isoformat(), (NOW + 17 * DAY).isoformat()]}
    report["credits"] = {"balance": "0", "unlimited": False}
    return report


def opencode_report():
    return quota_report("opencode", "OpenCode Go", windows(
        ("rolling", "5h", 0, timedelta(hours=3), 300),
        ("weekly", "Weekly", 3, 5 * DAY, 10080),
        ("monthly", "Monthly", 5, 4 * DAY, None)), 180)


def ollama_cloud_report():
    report = quota_report("ollama", "Ollama Cloud", [quota_window(
        "monthly_credits", "Monthly credits", None, None, NOW, None, state="unavailable")], 180)
    report["activity"] = {"from": (NOW - 8 * DAY).isoformat(), "until": NOW.isoformat(),
                          "observed_at": NOW.isoformat(), "request_count": 2,
                          "usage_usd": 2e-05, "input_tokens": 52, "cached_input_tokens": 0,
                          "output_tokens": 32, "state": "available"}
    report["note"] = "The reporting API supplies activity totals, not monthly allowance."
    return report


def sources():
    """Six accounts in the author's saved order: (row, snapshot)."""
    quota = lambda key, report: SourceSnapshot(key, "ok", NOW, {"quota": report})
    return [
        ({"id": "go", "type": "opencode_go", "name": "OpenCode Go", "enabled": True},
         quota("go", opencode_report())),
        ({"id": "cx", "type": "codex", "name": "Codex", "enabled": True},
         quota("cx", codex_report())),
        ({"id": "cl", "type": "claude", "name": "Claude", "enabled": True},
         quota("cl", claude_report())),
        ({"id": "oc", "type": "ollama_cloud", "name": "Ollama Cloud", "enabled": True},
         quota("oc", ollama_cloud_report())),
        ({"id": "ol", "type": "ollama", "name": "Ollama", "enabled": True},
         SourceSnapshot("ol", "ok", NOW, {"version": "0.12.1", "available_models": 5,
                                          "loaded_model": "llama3", "last_measured": None})),
        ({"id": "li", "type": "litellm", "name": "LiteLLM", "enabled": True},
         SourceSnapshot("li", "ok", NOW, {"online": True, "database": True,
                                          "requests_today": 12, "tokens_today": 340,
                                          "errors_today": 0, "top_model": "gpt-4o",
                                          "spend_today": 0.5})),
    ]


def full_payload(pairs=None):
    pairs = pairs if pairs is not None else sources()
    with patch("presto_ai_dash.quotas.now_utc", return_value=NOW), \
            patch("presto_ai_dash.dashboard.now_utc", return_value=NOW):
        return build_instance_payload({row["id"]: snap for row, snap in pairs},
                                      [row for row, _ in pairs])


def drawn(payload):
    """Everything the device would draw for this payload, as plain data."""
    cards_list, error = cards.parse_cards(payload)
    assert error is None
    rows = home.source_rows(payload)
    palette_display = Display()
    ui.draw_sources(palette_display, ui.Palette(palette_display), rows, 0, len(cards_list),
                    status=payload["overall_status"], scroll=0)
    home_view = (list(palette_display.rows), list(palette_display.rectangles))
    card_views = []
    for index, card in enumerate(cards_list):
        display = Display()
        ui.draw_card(display, ui.Palette(display), card, index, len(cards_list))
        card_views.append((list(display.rows), list(display.rectangles)))
    return rows, [repr(card) + repr(card.detail) for card in cards_list], home_view, card_views


class DevicePayloadTests(unittest.TestCase):
    def setUp(self):
        self.full = full_payload()
        self.slim = device_view(self.full)

    def test_the_device_draws_exactly_the_same_screens(self):
        self.assertEqual(drawn(self.slim), drawn(self.full))

    def test_the_payload_is_much_smaller_and_inside_the_budget(self):
        self.assertLessEqual(size(self.slim), BUDGET_BYTES,
                             "device payload %d bytes" % size(self.slim))
        self.assertLessEqual(size(self.slim) / len(self.slim["sources"]), BUDGET_PER_SOURCE)
        self.assertLess(size(self.slim), size(self.full) * 0.6)

    def test_the_payload_with_the_settings_is_inside_the_budget(self):
        """The display and alert settings travel with the payload."""
        payload = dict(self.slim)
        payload["display"] = {"overflow_rows": 1}
        payload["alerts"] = {"quiet": False, "sound": True, "pop_to_top": True,
                             "enabled": True}
        self.assertLessEqual(size(payload), BUDGET_BYTES,
                             "device payload %d bytes" % size(payload))
        self.assertLessEqual(size(payload) / len(payload["sources"]), BUDGET_PER_SOURCE)

    def test_a_quota_report_is_sent_once(self):
        for source in self.slim["sources"]:
            self.assertNotIn("quota", source.get("metrics", {}))
        self.assertTrue(any("quota" in source for source in self.slim["sources"]))

    def test_every_source_says_whether_it_alerts(self):
        """The device needs one boolean per service to draw the bell."""
        for source in self.slim["sources"]:
            self.assertIn("alerts", source)
            self.assertIs(type(source["alerts"]), bool)

    def test_the_device_never_receives_the_thresholds(self):
        """The device switches the alerts; it does not choose the numbers."""
        payload = json.dumps(self.slim)
        self.assertNotIn("warning_percent", payload)
        self.assertNotIn("critical_percent", payload)

    def test_admin_only_fields_are_not_sent(self):
        for source in self.slim["sources"]:
            quota = source.get("quota", {})
            for key in ADMIN_ONLY_QUOTA_KEYS:
                self.assertNotIn(key, quota, "%s sent to the device" % key)
            self.assertEqual(set(source) - set(DEVICE_SOURCE_KEYS), set())
            self.assertEqual(set(quota) - set(DEVICE_QUOTA_KEYS), set())
            for window in quota.get("windows", []):
                self.assertEqual(set(window) - set(DEVICE_WINDOW_KEYS), set())

    def test_everything_the_device_shows_is_still_sent(self):
        by_id = {source["id"]: source for source in self.slim["sources"]}
        self.assertEqual(by_id["cx"]["quota"]["reset_credits"]["available"], 3)
        self.assertIn("hint", by_id["cl"]["quota"])
        self.assertEqual(by_id["oc"]["quota"]["activity"]["request_count"], 2)
        self.assertEqual(by_id["li"]["metrics"]["requests_today"], 12)
        self.assertEqual(by_id["ol"]["metrics"]["loaded_model"], "llama3")
        self.assertEqual(len(by_id["go"]["quota"]["windows"]), 3)

    def test_the_top_level_and_the_cards_are_untouched(self):
        for key in self.full:
            if key != "sources":
                self.assertEqual(self.slim[key], self.full[key], key)

    def test_the_full_payload_is_not_modified(self):
        before = json.dumps(self.full, sort_keys=True)
        device_view(self.full)
        self.assertEqual(json.dumps(self.full, sort_keys=True), before)

    def test_it_copes_with_odd_sources(self):
        odd = {"cards": [], "sources": [None, "x", {"id": "a"}, {"id": "b", "quota": 5,
                                                                   "metrics": "no"},
                                        {"id": "c", "quota": {"windows": "no"}, "metrics": {}}]}
        slim = device_view(odd)  # must not raise
        self.assertEqual([s.get("id") for s in slim["sources"] if isinstance(s, dict)],
                         ["a", "b", "c"])

    def test_a_payload_without_sources_is_left_alone(self):
        self.assertEqual(device_view({"cards": []}), {"cards": []})


class WhitelistMatchesTheDeviceTests(unittest.TestCase):
    """Every payload key the device reads must be on the whitelist."""

    READERS = {"source": DEVICE_SOURCE_KEYS, "quota": DEVICE_QUOTA_KEYS,
               "window": DEVICE_WINDOW_KEYS, "activity": DEVICE_ACTIVITY_KEYS,
               "metrics": DEVICE_METRICS_KEYS, "reset_credits": DEVICE_RESET_KEYS,
               "payload": DEVICE_TOP_KEYS, "alerts": DEVICE_ALERT_KEYS}

    # The device functions that read the payload, and the file they live in.
    SCANNED = (("home.py", "source_rows"), ("home.py", "reset_badge"),
               ("home.py", "overflow_rows"), ("home.py", "alert_settings"),
               ("home.py", "alerts_state"), ("home.py", "alerts_flag"),
               ("main.py", "_apply_alerts"))

    def keys_read(self, scanned=None):
        found = []
        for filename, function_name in (scanned or self.SCANNED):
            path = os.path.join(presto_path.PRESTO_DIR, filename)
            with open(path, encoding="utf-8") as handle:
                tree = ast.parse(handle.read())
            for node in ast.walk(tree):
                if not (isinstance(node, ast.FunctionDef)
                        and node.name == function_name):
                    continue
                for child in ast.walk(node):
                    name = key = None
                    if (isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute)
                            and child.func.attr == "get" and isinstance(child.func.value, ast.Name)
                            and child.args and isinstance(child.args[0], ast.Constant)):
                        name, key = child.func.value.id, child.args[0].value
                    elif (isinstance(child, ast.Subscript) and isinstance(child.value, ast.Name)
                          and isinstance(child.slice, ast.Constant)):
                        name, key = child.value.id, child.slice.value
                    if name in self.READERS and isinstance(key, str):
                        found.append((name, key))
        return found

    def test_every_key_the_device_reads_is_whitelisted(self):
        found = self.keys_read()
        self.assertTrue(found, "the scan found nothing: the device code was restructured")
        missing = sorted({(name, key) for name, key in found if key not in self.READERS[name]
                          and not (name == "window" and key in ("expired",))})
        self.assertEqual(missing, [], "the device reads keys the daemon would not send: add "
                                      "them to the DEVICE_* whitelists in dashboard.py")

    def test_the_scan_really_sees_the_known_keys(self):
        found = set(self.keys_read())
        for pair in (("quota", "windows"), ("window", "used_percent"), ("quota", "hint"),
                     ("reset_credits", "available"), ("metrics", "loaded_model"),
                     ("source", "quota"), ("payload", "sources"), ("payload", "display"),
                     ("payload", "alerts"), ("alerts", "quiet"), ("alerts", "sound"),
                     ("alerts", "pop_to_top"), ("alerts", "enabled"),
                     ("source", "alerts")):
            self.assertIn(pair, found)

    def test_the_alert_settings_are_on_the_whitelist(self):
        for key in ("quiet", "sound", "pop_to_top", "enabled"):
            self.assertIn(key, DEVICE_ALERT_KEYS)
        self.assertIn("alerts", DEVICE_TOP_KEYS)
        self.assertIn("alerts", DEVICE_SOURCE_KEYS)

    def test_metrics_read_through_a_loop_are_whitelisted_too(self):
        """source_rows reads requests_today and tokens_today through a loop over a
        tuple of (key, label) pairs, which the literal-key scan above cannot see."""
        path = os.path.join(presto_path.PRESTO_DIR, "home.py")
        with open(path, encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
        looped = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "source_rows":
                for loop in ast.walk(node):
                    if isinstance(loop, ast.For) and isinstance(loop.iter, ast.Tuple):
                        for pair in loop.iter.elts:
                            if (isinstance(pair, ast.Tuple) and pair.elts
                                    and isinstance(pair.elts[0], ast.Constant)
                                    and isinstance(pair.elts[0].value, str)):
                                looped.add(pair.elts[0].value)
        self.assertIn("requests_today", looped, "the loop pattern changed: update this guard")
        self.assertEqual(sorted(looped - set(DEVICE_METRICS_KEYS)), [])


class ManagerServesTheDeviceViewTests(unittest.TestCase):
    def test_the_managed_status_payload_uses_the_device_view(self):
        import test_management
        base = test_management.ManagementBase("setUp")
        base.setUp()
        self.addCleanup(base.tearDown)
        base.disable_fake()
        row = base.manager.save({"type": "claude", "name": "Claude", "enabled": True,
                                 "config": {"settings_path": os.path.join(base.temp.name, "s.json")}})
        base.storage.save_snapshot(SourceSnapshot(row["id"], "ok", NOW,
                                                  {"quota": claude_report()}))
        payload = base.manager.status_payload()
        source = next(s for s in payload["sources"] if s["id"] == row["id"])
        self.assertNotIn("usage_cache", source["quota"])
        self.assertNotIn("note", source["quota"])
        self.assertNotIn("quota", source.get("metrics", {}))
        # The full payload still carries everything, for tests and the admin page.
        full = next(s for s in base.manager.full_status_payload()["sources"] if s["id"] == row["id"])
        self.assertIn("usage_cache", full["quota"])
        self.assertIn("note", full["quota"])

    def test_the_managed_payload_carries_the_alert_settings(self):
        import test_management
        base = test_management.ManagementBase("setUp")
        base.setUp()
        self.addCleanup(base.tearDown)
        payload = base.manager.status_payload()
        self.assertEqual(payload["alerts"], {"quiet": False, "sound": True,
                                              "pop_to_top": True, "enabled": True})


if __name__ == "__main__":
    unittest.main()
