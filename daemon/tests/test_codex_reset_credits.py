"""Phase 9: Codex banked reset credits, read from the reporting call only.

The project never redeems a reset credit; these tests only prove that the
numbers the provider reports are parsed, shown and labelled correctly.
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from presto_ai_dash.dashboard import _account_card, build_instance_payload
from presto_ai_dash.models import SourceSnapshot
from presto_ai_dash.sources.codex import CodexSource

NOW = datetime(2026, 10, 20, 12, tzinfo=timezone.utc)
DAY = timedelta(days=1)
RESET = int((NOW + 7 * DAY).timestamp())
EXPIRES = int((NOW + 4 * DAY).timestamp())


def days_from_now(days):
    """A Unix timestamp the given number of days ahead."""
    return int((NOW + timedelta(days=days)).timestamp())


def credit_row(status="available", expires_at=None, **extra):
    """One reset-credit row as the reporting call returns it."""
    row = {"id": "credit-1", "status": status, "resetType": "codexRateLimits",
           "grantedAt": int((NOW - DAY).timestamp()), "title": "Extra reset"}
    if expires_at is not None:
        row["expiresAt"] = expires_at
    row.update(extra)
    return row


def response(reset_credits=None, balance="0E-10", unlimited=False):
    """A reporting response shaped like the live one."""
    data = {"rateLimitsByLimitId": {"codex": {
        "primary": {"usedPercent": 14, "windowDurationMins": 10080, "resetsAt": RESET},
        "secondary": None,
        "credits": {"hasCredits": False, "unlimited": unlimited, "balance": balance}}}}
    if reset_credits is not None:
        data["rateLimitResetCredits"] = reset_credits
    return data


def parse(data):
    report, _ = CodexSource().parse(data, NOW)
    return report


def card(report, name="Codex personal"):
    snapshot = SourceSnapshot("codex-1", "ok", NOW, {"quota": report})
    with patch("presto_ai_dash.dashboard.now_utc", return_value=NOW):
        return _account_card({"id": "codex-1", "name": name, "type": "codex"},
                             snapshot, NOW)


class ResetCreditParsingTests(unittest.TestCase):
    """The count is the provider's; the rows only supply expiries."""

    def test_count_only_keeps_the_count_and_no_expiry(self):
        report = parse(response({"availableCount": 3}))
        self.assertEqual(report["reset_credits"]["available"], 3)
        self.assertEqual(report["reset_credits"]["expires_at"], [])

    def test_three_available_rows_give_three_expiries_earliest_first(self):
        rows = [credit_row(expires_at=days_from_now(6), id="c"),
                credit_row(expires_at=days_from_now(4), id="b"),
                credit_row(expires_at=days_from_now(5), id="a")]
        report = parse(response({"availableCount": 3, "credits": rows}))
        self.assertEqual(report["reset_credits"]["available"], 3)
        self.assertEqual(report["reset_credits"]["expires_at"],
                         ["2026-10-24T12:00:00+00:00",
                          "2026-10-25T12:00:00+00:00",
                          "2026-10-26T12:00:00+00:00"])

    def test_only_available_rows_supply_expiries(self):
        rows = [credit_row(status="redeemed", expires_at=EXPIRES),
                credit_row(status="redeeming", expires_at=days_from_now(5)),
                credit_row(status="unknown", expires_at=days_from_now(6)),
                credit_row(status="available", expires_at=days_from_now(7))]
        report = parse(response({"availableCount": 4, "credits": rows}))
        self.assertEqual(report["reset_credits"]["expires_at"],
                         ["2026-10-27T12:00:00+00:00"])

    def test_a_row_without_an_expiry_supplies_none(self):
        rows = [credit_row(status="available", expires_at=None)]
        report = parse(response({"availableCount": 1, "credits": rows}))
        self.assertEqual(report["reset_credits"]["available"], 1)
        self.assertEqual(report["reset_credits"]["expires_at"], [])

    def test_the_count_is_authoritative_when_the_rows_disagree(self):
        rows = [credit_row(expires_at=EXPIRES), credit_row(expires_at=days_from_now(5))]
        report = parse(response({"availableCount": 5, "credits": rows}))
        self.assertEqual(report["reset_credits"]["available"], 5)
        self.assertEqual(len(report["reset_credits"]["expires_at"]), 2)

    def test_null_everywhere_means_not_reported_never_zero(self):
        report = parse(response({"availableCount": None, "credits": None}))
        self.assertIsNone(report["reset_credits"]["available"])
        self.assertEqual(report["reset_credits"]["expires_at"], [])
        missing = parse(response())
        self.assertIsNone(missing["reset_credits"]["available"])
        self.assertEqual(missing["reset_credits"]["expires_at"], [])

    def test_a_malformed_count_is_not_reported(self):
        for count in (True, False, "3", 3.0, -1, 100, 1000, [], {}):
            with self.subTest(count=count):
                report = parse(response({"availableCount": count}))
                self.assertIsNone(report["reset_credits"]["available"])

    def test_bad_expiry_values_are_ignored(self):
        for expires in (True, False, "1792693081", 1.5, -5, 0, 10 ** 12):
            with self.subTest(expires=expires):
                rows = [credit_row(expires_at=expires)]
                report = parse(response({"availableCount": 1, "credits": rows}))
                self.assertEqual(report["reset_credits"]["expires_at"], [])

    def test_at_most_eight_expiries_are_kept_earliest_first(self):
        rows = [credit_row(expires_at=days_from_now(index), id="c%d" % index)
                for index in range(12)]
        report = parse(response({"availableCount": 12, "credits": rows}))
        expiries = report["reset_credits"]["expires_at"]
        self.assertEqual(len(expiries), 8)
        self.assertEqual(expiries, sorted(expiries))
        self.assertEqual(expiries[0], "2026-10-20T12:00:00+00:00")

    def test_junk_rows_never_break_the_quota_reading(self):
        for rows in ("not a list", [1, 2, 3], [None], [{"status": "available"}]):
            with self.subTest(rows=rows):
                report = parse(response({"availableCount": 1, "credits": rows}))
                self.assertEqual(report["reset_credits"]["available"], 1)
                self.assertEqual(report["reset_credits"]["expires_at"], [])
                self.assertEqual(report["windows"][0]["used_percent"], 14)


class CreditBalanceTests(unittest.TestCase):
    """The balance is a decimal string, never exponent notation."""

    def test_the_live_balance_formats(self):
        cases = (("0E-10", "0"), ("12.50", "12.5"), ("1E+3", "1000"),
                 ("0", "0"), ("0.000", "0"), ("7", "7"), ("1.5", "1.5"))
        for raw, expected in cases:
            with self.subTest(balance=raw):
                report = parse(response(balance=raw))
                self.assertEqual(report["credits"]["balance"], expected)

    def test_an_unusable_balance_is_not_reported(self):
        for raw in ("garbage", "", "   ", "1" * 100, "1E+1000", "1,5", None, 12.5, True):
            with self.subTest(balance=raw):
                report = parse(response(balance=raw))
                self.assertIsNone(report["credits"]["balance"])

    def test_unlimited_is_only_a_real_boolean(self):
        self.assertIs(parse(response(unlimited=True))["credits"]["unlimited"], True)
        self.assertIs(parse(response(unlimited=False))["credits"]["unlimited"], False)
        for raw in (1, 0, "true", None):
            with self.subTest(unlimited=raw):
                data = response()
                data["rateLimitsByLimitId"]["codex"]["credits"]["unlimited"] = raw
                self.assertIsNone(parse(data)["credits"]["unlimited"])

    def test_a_bucket_without_credits_reports_nothing(self):
        data = response()
        del data["rateLimitsByLimitId"]["codex"]["credits"]
        credits = parse(data)["credits"]
        self.assertIsNone(credits["balance"])
        self.assertIsNone(credits["unlimited"])

    def test_the_legacy_single_bucket_carries_the_same_credits(self):
        bucket = response()["rateLimitsByLimitId"]["codex"]
        report = parse({"rateLimits": bucket})
        self.assertEqual(report["credits"]["balance"], "0")
        self.assertIs(report["credits"]["unlimited"], False)


class ResetCreditCardTests(unittest.TestCase):
    """The Codex card shows the resets, their expiry, the balance and the source."""

    def report(self, available=3, expires=True, balance="0E-10", unlimited=False):
        rows = [credit_row(expires_at=EXPIRES)] if expires else []
        summary = {"availableCount": available, "credits": rows}
        return parse(response(summary, balance=balance, unlimited=unlimited))

    def test_the_new_lines_name_what_they_are(self):
        detail = card(self.report()).detail
        self.assertIn("Resets avail: 3", detail)
        self.assertIn("Next exp: 10-24 12:00Z (4 days)", detail)
        self.assertIn("Credits: 0", detail)
        self.assertIn("Source: local Codex app-server", detail)

    def test_the_window_lines_come_first_and_the_source_line_is_last(self):
        detail = card(self.report()).detail
        self.assertTrue(detail[0].startswith("Weekly reset:"))
        self.assertEqual(detail[-1], "Source: local Codex app-server")

    def test_the_card_never_exceeds_six_lines(self):
        detail = card(self.report()).detail
        self.assertLessEqual(len(detail), 6)
        # The new lines are the ones that go when the card is full.
        self.assertNotIn("Source: local Codex app-server", detail[:1])

    def test_unlimited_credits_say_unlimited(self):
        detail = card(self.report(unlimited=True)).detail
        self.assertIn("Credits: unlimited", detail)

    def test_nothing_is_shown_when_the_provider_reports_no_resets(self):
        data = response()
        del data["rateLimitsByLimitId"]["codex"]["credits"]
        detail = card(parse(data)).detail
        self.assertFalse(any(line.startswith("Resets avail") for line in detail), detail)
        self.assertFalse(any(line.startswith("Next exp") for line in detail), detail)
        self.assertFalse(any(line.startswith("Credits") for line in detail), detail)
        self.assertFalse(any(line.startswith("Source:") for line in detail), detail)

    def test_a_reported_balance_is_shown_even_without_a_reset_count(self):
        detail = card(self.report(available=None, expires=False)).detail
        self.assertFalse(any(line.startswith("Resets avail") for line in detail), detail)
        self.assertFalse(any(line.startswith("Next exp") for line in detail), detail)
        # The balance is a separate provider field, so it is not lost.
        self.assertIn("Credits: 0", detail)

    def test_a_zero_count_is_not_the_same_as_no_reporting(self):
        detail = card(self.report(available=0, expires=False)).detail
        self.assertIn("Resets avail: 0", detail)

    def test_other_providers_cards_do_not_change(self):
        from presto_ai_dash.quotas import quota_report, quota_window
        report = quota_report("opencode", "Go", [
            quota_window("weekly", "Weekly", 34, NOW + DAY, NOW)])
        detail = card(report).detail
        self.assertFalse(any("Resets" in line for line in detail), detail)
        self.assertFalse(any(line.startswith("Source:") for line in detail), detail)

    def test_every_new_line_fits_the_detail_width(self):
        preview_dir = Path(__file__).resolve().parents[2] / "tools" / "presto_preview"
        sys.path.insert(0, str(preview_dir))
        from framebuffer import Framebuffer
        display = Framebuffer()
        display.set_font("bitmap6")
        available_width = display.width - (8 + 6) - 8
        for line in card(self.report()).detail:
            with self.subTest(line=line):
                self.assertLessEqual(display.measure_text(line, 1), available_width, line)
                self.assertLessEqual(len(line), 34, line)


class ResetCreditPayloadTests(unittest.TestCase):
    """The numbers reach the status payload unchanged."""

    def test_the_payload_carries_the_reset_credits_and_the_balance(self):
        report = parse(response({"availableCount": 2,
                                 "credits": [credit_row(expires_at=EXPIRES)]}))
        snapshot = SourceSnapshot("codex-1", "ok", NOW, {"quota": report})
        row = {"id": "codex-1", "type": "codex", "name": "Codex", "enabled": True}
        with patch("presto_ai_dash.quotas.now_utc", return_value=NOW), \
                patch("presto_ai_dash.dashboard.now_utc", return_value=NOW):
            payload = build_instance_payload({"codex-1": snapshot}, [row])
        source = payload["sources"][0]
        self.assertEqual(source["quota"]["reset_credits"]["available"], 2)
        self.assertEqual(source["quota"]["reset_credits"]["expires_at"],
                         ["2026-10-24T12:00:00+00:00"])
        self.assertEqual(source["quota"]["credits"]["balance"], "0")


class NoRedeemTests(unittest.TestCase):
    """The project observes; it never spends a banked reset."""

    def test_no_shipped_code_names_the_redeem_method(self):
        root = Path(__file__).resolve().parents[2]
        needles = ("consumeaccountratelimitresetcredit",
                   "consume_account_rate_limit_reset_credit")
        for folder in (root / "daemon" / "src", root / "presto"):
            for path in folder.rglob("*"):
                if not path.is_file() or path.suffix not in (".py", ".js", ".html", ".css"):
                    continue
                text = path.read_text(encoding="utf-8", errors="ignore").lower()
                for needle in needles:
                    self.assertNotIn(needle, text, str(path))


if __name__ == "__main__":
    unittest.main()
