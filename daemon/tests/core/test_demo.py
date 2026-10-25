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
