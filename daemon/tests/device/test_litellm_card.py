"""A LiteLLM that answers but will not report usage says why, on the Presto too."""

import unittest

import presto_path
from home import source_rows
from ui import Palette, draw_sources
from device.test_ui import Display
from presto_ai_dash.dashboard import build_status_payload, device_view
from presto_ai_dash.models import Alert, SourceSnapshot


def refused(status=403):
    note = {401: "key rejected", 403: "key refused", 404: "no usage route"}[status]
    message = {401: "LiteLLM rejected the API key",
               403: "LiteLLM key lacks permission to read usage",
               404: "LiteLLM daily activity endpoint not found"}[status]
    return SourceSnapshot(
        "litellm", "warning", alerts=[Alert("warning", message).to_dict()],
        metrics={"online": True, "database": "connected", "usage": "unavailable",
                 "usage_note": note, "usage_reason": "route not allowed"})


class LiteLLMCardTests(unittest.TestCase):
    def card(self, snapshot):
        return build_status_payload({"litellm": snapshot})["cards"][2]

    def test_the_card_names_the_problem_and_drops_the_lines_that_say_nothing(self):
        card = self.card(refused())
        self.assertEqual(card["secondary"], "Usage: key refused")
        self.assertEqual(card["detail"][:2], ["Key refused (HTTP 403)", "It may not read usage"])
        self.assertNotIn("Database: connected", card["detail"])  # the tile says it
        self.assertFalse([line for line in card["detail"] if line.endswith("unavailable")])
        for line in card["detail"]:
            self.assertLessEqual(len(line), 28)
            self.assertTrue(line.isascii())

    def test_each_refusal_has_its_own_short_words(self):
        self.assertEqual(self.card(refused(401))["detail"][0], "Key rejected (HTTP 401)")
        self.assertEqual(self.card(refused(404))["detail"][0], "No usage route (404)")

    def test_a_healthy_card_has_only_what_it_knows(self):
        card = self.card(SourceSnapshot("litellm", "ok", metrics={
            "online": True, "requests_today": 12, "tokens_today": 340, "spend_today": 0.5,
            "top_model": "gpt-4o", "errors_today": 0, "database": "connected"}))
        self.assertEqual(card["primary"], "12 requests today")
        self.assertEqual(card["secondary"], "340 tokens · spend $0.50")
        self.assertEqual(card["detail"], ["DB connected"])

    def test_the_home_row_says_why_instead_of_warning(self):
        payload = {"sources": [{"id": "l", "type": "litellm", "name": "LiteLLM",
                                "status": "warning",
                                "metrics": {"usage_note": "key refused"}}]}
        slim = device_view(payload)
        self.assertEqual(slim["sources"][0]["metrics"], {"usage_note": "key refused"})
        row = source_rows(slim)[0]
        self.assertEqual(row["note"], "key refused")
        display = Display()
        draw_sources(display, Palette(display), [row], 0, 2)
        self.assertIn("KEY REFUSED", [r[0] for r in display.rows])

    def test_a_row_without_a_note_still_shows_the_status(self):
        row = source_rows({"sources": [{"id": "l", "type": "litellm", "name": "LiteLLM",
                                        "status": "warning", "metrics": {}}]})[0]
        self.assertIsNone(row["note"])
        display = Display()
        draw_sources(display, Palette(display), [row], 0, 2)
        self.assertIn("WARNING", [r[0] for r in display.rows])

    def test_an_odd_note_is_not_drawn(self):
        for note in (None, "", 5, "x" * 40):
            row = source_rows({"sources": [{"id": "l", "type": "litellm", "name": "L",
                                            "status": "warning",
                                            "metrics": {"usage_note": note}}]})[0]
            self.assertIsNone(row["note"], note)


if __name__ == "__main__":
    unittest.main()
