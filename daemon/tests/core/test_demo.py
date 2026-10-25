"""The demo scenario: sample data built by the real payload builders.

The demo is what PSL, the daemon's demo mode and the docs page all show, so
it is held to the same contracts as a real daemon: every message is inside
the size budget, the device can draw every message, the loop reaches every
status the daemon can produce, and every registered source type has an
account. Numbers progress in small steps, and nothing here contacts anyone.
"""

from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from presto_ai_dash import demo, models, sources  # noqa: E402

import presto_path  # noqa: F401,E402  (adds presto/ to sys.path)
import net  # noqa: E402
from home import source_rows, home_windows  # noqa: E402
from device.test_device_payload import (BUDGET_BYTES, BUDGET_PER_SOURCE,  # noqa: E402
                                        drawn)

# A fixed moment: the demo is deterministic for a given ``now``.
FIXED = datetime(2026, 10, 23, 12, tzinfo=timezone.utc)
STEPS = range(0, demo.LOOP_SECONDS, demo.STEP_SECONDS)


def size(value):
    return len(json.dumps(value, separators=(",", ":")))


def payloads():
    return [(t, demo.demo_payload(t, now=FIXED)) for t in STEPS]


def claude_first_window(t):
    """The Claude 5h percentage at loop second ``t``, from the snapshot."""
    for row, snapshot in demo.demo_pairs(t, now=FIXED):
        if row["id"] == "demo-claude":
            return snapshot.metrics["quota"]["windows"][0]["used_percent"]
    raise AssertionError("the demo has no Claude account")


class DemoSizeTests(unittest.TestCase):
    def test_every_poll_message_is_inside_the_device_budget(self):
        for t, payload in payloads():
            self.assertEqual(payload["demo"], True)
            self.assertLessEqual(size(payload), BUDGET_BYTES,
                                 "demo payload at %ds is %d bytes" % (t, size(payload)))
            self.assertLessEqual(size(payload) / len(payload["sources"]),
                                 BUDGET_PER_SOURCE)

    def test_every_card_detail_is_inside_its_own_limit(self):
        for t, payload in payloads():
            for card in payload["cards"]:
                detail = demo.demo_card_detail(card["id"], t, now=FIXED)
                self.assertIsNotNone(detail, card["id"])
                self.assertLessEqual(size(detail), net.CARD_RESPONSE_LIMIT,
                                     "%s detail at %ds is %d bytes"
                                     % (card["id"], t, size(detail)))

    def test_an_unknown_card_has_no_detail(self):
        self.assertIsNone(demo.demo_card_detail("demo-missing", 0, now=FIXED))


class DemoCoverageTests(unittest.TestCase):
    def test_the_loop_reaches_every_status(self):
        expected = set(models.STATUS_ORDER) - set(demo.UNREACHABLE_STATUSES)
        on_cards, overall = set(), set()
        for _, payload in payloads():
            overall.add(payload["overall_status"])
            on_cards.update(source["status"] for source in payload["sources"])
        self.assertEqual(expected - on_cards, set(),
                         "the demo never shows these statuses on a card")
        self.assertEqual(expected - overall, set(),
                         "the demo never makes these statuses the overall one")

    def test_every_source_type_has_a_demo_account(self):
        shown = set()
        for _, payload in payloads():
            shown.update(source["type"] for source in payload["sources"])
        missing = [entry["id"] for entry in sources.source_catalog()
                   if entry["id"] not in shown]
        # Adding a source means adding its demo account: this is the test that
        # says so. There is no exemption list, so a new type fails here until
        # demo.py grows an account of that type (see the developer docs).
        self.assertEqual(missing, [])

    def test_the_rule_notices_a_new_type_without_demo_data(self):
        """The check is live: a new source type is reported until demoed."""
        class NewlyAdded(sources.Source):
            id = "newly_added"
            name = "Newly added"

            def collect(self):
                return self.snapshot("ok")

        sources.register(NewlyAdded)
        try:
            shown = {source["type"] for _, payload in payloads()
                     for source in payload["sources"]}
            missing = [entry["id"] for entry in sources.source_catalog()
                       if entry["id"] not in shown]
            self.assertIn("newly_added", missing)
        finally:
            sources._REGISTRY.pop("newly_added", None)
        self.assertNotIn("newly_added",
                         {entry["id"] for entry in sources.source_catalog()})

    def test_every_scene_has_an_account_it_exercises(self):
        # The scene list is the script: every name in it is shown at some step.
        self.assertEqual({demo.scene_at(t) for t in STEPS},
                         {name for name, _ in demo.SCENES})

    def test_jumping_to_each_scene_immediately_shows_its_promised_status(self):
        expected = {"calm": "active", "climbing": "active", "warning": "warning",
                    "near_limit": "near_limit", "critical": "critical",
                    "rate_limited": "rate_limited", "offline": "offline", "recovery": "ok"}
        for index, (name, start) in enumerate(demo.SCENES):
            end = demo.SCENES[index + 1][1] if index + 1 < len(demo.SCENES) else demo.LOOP_SECONDS
            for second in range(start, end):
                with self.subTest(scene=name, second=second):
                    self.assertEqual(demo.demo_payload(second, now=FIXED)["overall_status"], expected[name])


class DemoReadingTests(unittest.TestCase):
    def source(self, scene, source_id):
        payload = demo.demo_payload(demo.scene_second(scene), now=FIXED)
        return next(source for source in payload["sources"] if source["id"] == source_id)

    def test_cloud_has_a_consistent_monthly_meter_separate_from_rolling_activity(self):
        for name, start in demo.SCENES:
            with self.subTest(scene=name):
                pairs = {row["id"]: snapshot for row, snapshot in demo.demo_pairs(start, now=FIXED)}
                report = pairs["demo-ollama-cloud"].metrics["quota"]
                window = report["windows"][0]
                balance = report["cloud_balance"]
                self.assertEqual(window["limit_amount"], 60)
                self.assertEqual(window["currency"], "USD")
                self.assertAlmostEqual(window["used_percent"], 100 * window["used_amount"] / 60)
                self.assertAlmostEqual(balance["included_remaining_usd"] + window["used_amount"], 60)
                self.assertEqual(window["resets_at"], balance["period_until"])
                self.assertLessEqual(datetime.fromisoformat(balance["period_from"]),
                                     datetime.fromisoformat(balance["observed_at"]))
                self.assertLess(datetime.fromisoformat(balance["observed_at"]),
                                datetime.fromisoformat(balance["period_until"]))
                self.assertEqual(balance["purchased_remaining_usd"], 12.5)
                self.assertEqual(report["activity"]["range"], "7d")
                self.assertNotEqual(report["activity"]["from"], balance["period_from"])
                self.assertNotEqual(report["activity"]["usage_usd"], window["used_amount"])
                self.assertEqual(len(demo.demo_card_detail("demo-ollama-cloud", start, now=FIXED)["tiles"]), 6)

    def test_provider_loss_retains_and_marks_cloud_readings_stale_until_recovery(self):
        offline = self.source("offline", "demo-ollama-cloud")
        self.assertEqual(offline["status"], "offline")
        self.assertEqual(offline["quota"]["windows"][0]["state"], "stale")
        self.assertEqual(offline["quota"]["activity"]["state"], "stale")
        last_step = demo.scene_second("offline") - demo.STEP_SECONDS
        last_good = next(source for source in demo.demo_payload(last_step, now=FIXED)["sources"]
                         if source["id"] == "demo-ollama-cloud")
        self.assertEqual(offline["quota"]["windows"][0]["used_percent"],
                         last_good["quota"]["windows"][0]["used_percent"])
        for key in ("request_count", "usage_usd"):
            self.assertEqual(offline["quota"]["activity"][key], last_good["quota"]["activity"][key])
        tiles = demo.demo_card_detail("demo-ollama-cloud", demo.scene_second("offline"), now=FIXED)["tiles"]
        self.assertTrue(all(tile["tone"] == "dim" for tile in tiles))
        self.assertIn("old left", [tile["label"] for tile in tiles])
        recovered = self.source("recovery", "demo-ollama-cloud")
        self.assertEqual(recovered["status"], "ok")
        self.assertEqual(recovered["quota"]["windows"][0]["state"], "available")
        self.assertEqual(recovered["quota"]["activity"]["state"], "available")

    def test_cloud_exhaustion_and_claude_limit_have_different_meanings(self):
        cloud = self.source("critical", "demo-ollama-cloud")
        self.assertEqual(cloud["quota"]["windows"][0]["used_percent"], 100)
        self.assertEqual(cloud["status"], "critical")
        self.assertEqual(self.source("rate_limited", "demo-claude")["status"], "rate_limited")
        refilled = self.source("rate_limited", "demo-ollama-cloud")
        self.assertEqual(refilled["quota"]["windows"][0]["used_percent"], 0)
        self.assertEqual(refilled["quota"]["windows"][0]["state"], "available")
        self.assertNotEqual(refilled["quota"]["windows"][0]["resets_at"], cloud["quota"]["windows"][0]["resets_at"])

    def test_warning_is_a_stale_codex_reading_and_missing_secondary_is_not_zero(self):
        codex = self.source("warning", "demo-codex")
        self.assertEqual(codex["status"], "warning")
        self.assertEqual(codex["quota"]["windows"][0]["state"], "stale")
        self.assertGreater(codex["quota"]["windows"][0]["used_percent"], 0)
        secondary = codex["quota"]["windows"][1]
        self.assertEqual(secondary["state"], "unavailable")
        self.assertIsNone(secondary["used_percent"])
        payload = demo.demo_payload(demo.scene_second("warning"), now=FIXED)
        row = next(row for row in source_rows(payload) if row["id"] == "demo-codex")
        missing = next(window for window in home_windows(row) if window["label"] == "5h")
        self.assertIsNone(missing["percent"])
        self.assertEqual(missing["state"], "unavailable")
        self.assertEqual(codex["quota"]["reset_credits"]["available"], 3)
        self.assertEqual(len(codex["quota"]["reset_credits"]["expires_at"]), 3)

    def test_bells_change_aggregation_without_hiding_a_services_real_reading(self):
        start = demo.scene_second("critical")
        payload = demo.demo_payload(start, now=FIXED, alert_overrides={"demo-claude": False})
        self.assertEqual(payload["overall_status"], "critical")
        claude = next(source for source in payload["sources"] if source["id"] == "demo-claude")
        self.assertEqual(claude["status"], "critical")
        self.assertFalse(claude["alerts"])
        payload = demo.demo_payload(start, now=FIXED,
                                    alert_overrides={"demo-claude": False, "demo-ollama-cloud": False})
        self.assertEqual(payload["overall_status"], "ok")


class DemoIdentityTests(unittest.TestCase):
    def test_every_demo_account_id_is_marked(self):
        for _, payload in payloads():
            for source in payload["sources"]:
                self.assertTrue(source["id"].startswith("demo-"), source["id"])

    def test_every_account_card_id_is_marked(self):
        # "dash" is the Home card's fixed id in the real builder; every other
        # card belongs to an account and carries the demo prefix.
        for _, payload in payloads():
            for card in payload["cards"]:
                if card["id"] == "dash":
                    continue
                self.assertTrue(card["id"].startswith("demo-"), card["id"])

    def test_the_payload_is_deterministic(self):
        first = json.dumps(demo.demo_payload(6, now=FIXED), sort_keys=True)
        second = json.dumps(demo.demo_payload(6, now=FIXED), sort_keys=True)
        self.assertEqual(first, second)

    def test_every_demo_account_is_a_registered_source_type(self):
        """The demo cannot invent a service: each type is one the daemon serves."""
        known = {entry["id"] for entry in sources.source_catalog()}
        for _, payload in payloads():
            for source in payload["sources"]:
                self.assertIn(source["type"], known, source["id"])


class DeviceAcceptsTheDemoTests(unittest.TestCase):
    def test_the_device_draws_every_message(self):
        for t, payload in payloads():
            rows, details, home_view, card_views = drawn(payload)
            self.assertEqual(len(rows), len(payload["sources"]), "at %ds" % t)
            self.assertEqual(len(card_views), len(payload["cards"]), "at %ds" % t)
            self.assertTrue(home_view[0], "Home drew nothing at %ds" % t)


class ProgressionTests(unittest.TestCase):
    def test_the_busy_account_climbs_within_a_scene(self):
        steps = [
            (t, demo.scene_at(t), claude_first_window(t)) for t in STEPS
        ]
        scene_of = {t: scene for t, scene, _ in steps}
        values = {t: value for t, _, value in steps}
        for t, scene, value in steps:
            if scene == "recovery":
                # Recovery is the ramp back down; it is deliberately not rising.
                continue
            later = t + demo.STEP_SECONDS
            if later not in values or scene_of[later] != scene:
                continue
            self.assertGreaterEqual(values[later], value,
                                    "%s dropped from %d%% to %d%%"
                                    % (scene, value, values[later]))

    def test_it_moves_in_small_steps_inside_a_scene(self):
        values = {t: claude_first_window(t) for t in STEPS}
        for t, value in values.items():
            later = t + demo.STEP_SECONDS
            if later not in values or demo.scene_at(later) != demo.scene_at(t):
                continue
            self.assertLessEqual(abs(values[later] - value), 10,
                                 "a %d point jump inside scene %s at %ds"
                                 % (abs(values[later] - value), demo.scene_at(t), t))


if __name__ == "__main__":
    unittest.main()
