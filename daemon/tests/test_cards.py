"""Tests for presto/cards.py - the card model and status vocabulary."""

import unittest

import presto_path  # noqa: F401  (adds presto/ to sys.path)

from cards import (
    CARD_ORDER,
    Card,
    accent_for,
    error_card,
    normalise_status,
    offline_card,
    overall_status,
    parse_cards,
    severity,
    truncate,
)


class TestNormaliseStatus(unittest.TestCase):

    def test_canonical_values_pass_through(self):
        for status in ("ok", "active", "warning", "near_limit",
                       "rate_limited", "critical", "offline"):
            self.assertEqual(normalise_status(status), status)

    def test_normal_is_an_alias_for_ok(self):
        self.assertEqual(normalise_status("normal"), "ok")

    def test_case_and_whitespace_are_tolerated(self):
        self.assertEqual(normalise_status("  WARNING "), "warning")

    def test_unknown_becomes_offline(self):
        self.assertEqual(normalise_status("banana"), "offline")

    def test_none_becomes_offline(self):
        self.assertEqual(normalise_status(None), "offline")

    def test_non_string_becomes_offline(self):
        self.assertEqual(normalise_status(42), "offline")


class TestSeverity(unittest.TestCase):

    def test_ordering_is_monotonic(self):
        order = ["ok", "active", "warning", "near_limit",
                 "rate_limited", "critical", "offline"]
        ranks = [severity(s) for s in order]
        self.assertEqual(ranks, sorted(ranks))

    def test_unknown_is_as_bad_as_offline(self):
        self.assertEqual(severity("banana"), severity("offline"))


class TestAccent(unittest.TestCase):

    def test_each_status_has_an_accent(self):
        for status in ("ok", "active", "warning", "near_limit",
                       "rate_limited", "critical", "offline"):
            self.assertTrue(accent_for(status))

    def test_unknown_falls_back_to_red(self):
        self.assertEqual(accent_for("banana"), "red")


class TestOverallStatus(unittest.TestCase):

    def test_empty_is_offline(self):
        self.assertEqual(overall_status([]), "offline")

    def test_worst_wins(self):
        cards = [Card("a", "A", "ok"), Card("b", "B", "warning"),
                 Card("c", "C", "active")]
        self.assertEqual(overall_status(cards), "warning")

    def test_single_card(self):
        self.assertEqual(overall_status([Card("a", "A", "critical")]), "critical")


class TestTruncate(unittest.TestCase):

    def test_short_string_unchanged(self):
        self.assertEqual(truncate("hello", 10), "hello")

    def test_long_string_is_clipped(self):
        result = truncate("abcdefghij", 5)
        self.assertEqual(len(result), 5)
        self.assertTrue(result.endswith("~"))

    def test_none_becomes_empty(self):
        self.assertEqual(truncate(None, 5), "")

    def test_zero_max(self):
        self.assertEqual(truncate("abc", 0), "")


class TestCard(unittest.TestCase):

    def test_from_dict_full(self):
        card = Card.from_dict({
            "id": "ollama", "title": "Ollama", "status": "active",
            "primary": "38 tok/s", "secondary": "qwen",
            "detail": ["a", "b"], "accent": "blue", "alert": None,
        })
        self.assertEqual(card.id, "ollama")
        self.assertEqual(card.status, "active")
        self.assertEqual(card.detail, ["a", "b"])
        self.assertEqual(card.accent, "blue")

    def test_from_dict_normalises_status(self):
        card = Card.from_dict({"id": "x", "title": "X", "status": "normal"})
        self.assertEqual(card.status, "ok")

    def test_from_dict_bad_accent_falls_back(self):
        card = Card.from_dict({"id": "x", "title": "X", "status": "ok",
                               "accent": "chartreuse"})
        self.assertEqual(card.accent, "green")

    def test_from_dict_non_dict(self):
        card = Card.from_dict("nope")
        self.assertEqual(card.status, "offline")

    def test_from_dict_missing_fields(self):
        card = Card.from_dict({"id": "x"})
        self.assertEqual(card.title, "Unknown")
        self.assertEqual(card.primary, "")

    def test_from_dict_bad_detail_is_ignored(self):
        card = Card.from_dict({"id": "x", "title": "X", "status": "ok",
                               "detail": "not a list"})
        self.assertEqual(card.detail, [])

    def test_from_dict_non_dict_alert_is_dropped(self):
        card = Card.from_dict({"id": "x", "title": "X", "status": "ok",
                               "alert": "loud"})
        self.assertIsNone(card.alert)

    def test_detail_lines_pads(self):
        card = Card("x", "X", "ok", detail=["one"])
        self.assertEqual(card.detail_lines(3), ["one", "", ""])

    def test_detail_lines_truncates(self):
        card = Card("x", "X", "ok", detail=["a", "b", "c", "d"])
        self.assertEqual(card.detail_lines(2), ["a", "b"])


class TestParseCards(unittest.TestCase):

    def _payload(self, cards):
        return {"app": "presto-ai-dash", "cards": cards}

    def test_orders_cards_by_plan(self):
        payload = self._payload([
            {"id": "litellm", "title": "LiteLLM", "status": "ok"},
            {"id": "ollama", "title": "Ollama", "status": "active"},
            {"id": "dash", "title": "AI Dash", "status": "ok"},
        ])
        cards, error = parse_cards(payload)
        self.assertIsNone(error)
        self.assertEqual([c.id for c in cards],
                         ["dash", "ollama", "litellm"])

    def test_unknown_ids_go_last(self):
        payload = self._payload([
            {"id": "zzz", "title": "Z", "status": "ok"},
            {"id": "dash", "title": "AI Dash", "status": "ok"},
        ])
        cards, _ = parse_cards(payload)
        self.assertEqual([c.id for c in cards], ["dash", "zzz"])

    def test_non_dict_payload_is_an_error(self):
        cards, error = parse_cards("nope")
        self.assertIsNotNone(error)
        self.assertEqual(cards[0].status, "critical")

    def test_missing_cards_is_an_error(self):
        cards, error = parse_cards({"app": "presto-ai-dash"})
        self.assertIsNotNone(error)
        self.assertEqual(cards[0].status, "critical")

    def test_empty_cards_is_an_error(self):
        cards, error = parse_cards({"cards": []})
        self.assertIsNotNone(error)

    def test_cards_not_a_list_is_an_error(self):
        cards, error = parse_cards({"cards": {"a": 1}})
        self.assertIsNotNone(error)


class TestSpecialCards(unittest.TestCase):

    def test_error_card_is_critical(self):
        self.assertEqual(error_card("boom").status, "critical")

    def test_offline_card_is_offline(self):
        self.assertEqual(offline_card("gone").status, "offline")


if __name__ == "__main__":
    unittest.main()
