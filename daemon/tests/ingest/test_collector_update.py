"""A copy of the collector that is older than the one the daemon serves is noticed.

The collector is one file that users copy to other computers, so a copy can
lag. The daemon knows the version of the file it serves, and every report says
which version sent it. The daemon says so where the owner will see it, as a
chore: the admin page, the Problems list and the Claude card's detail lines. It
never changes a status or a colour, and it never touches the copy itself.

No version is ever deployed at the moment, so these tests move the served
version with a patch instead of waiting for a real second release.
"""

from datetime import timedelta
import logging
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from presto_ai_dash import ingest as ingest_module
from presto_ai_dash.collectors import presto_collector, served_version, update_state
from presto_ai_dash.dashboard import _account_card
from presto_ai_dash.models import SourceSnapshot
from providers.test_claude_hint import NOW, trail_report, window
from ingest.test_ingest_api import envelope
from admin.test_management import ManagementBase


def serving(version):
    """The daemon serves this version of the collector."""
    return patch.object(presto_collector, "COLLECTOR_VERSION", version)


class UpdateStateTests(unittest.TestCase):
    def test_a_copy_is_compared_with_the_version_served(self):
        with serving("3"):
            self.assertEqual(update_state("1"), "available")
            self.assertEqual(update_state("2"), "available")
            self.assertEqual(update_state("3"), "current")
            self.assertEqual(update_state("4"), "newer")

    def test_a_version_that_is_not_a_whole_number_makes_no_claim(self):
        with serving("3"):
            for reported in (None, "", "x", "1.2", "v1", " 1", "-1", "1 ", 1, 1.0, ["1"],
                             "١", "1" * 40):
                with self.subTest(reported=reported):
                    self.assertEqual(update_state(reported), "unknown")

    def test_the_served_version_is_the_collectors_own(self):
        self.assertEqual(served_version(), presto_collector.COLLECTOR_VERSION)
        with serving("9"):
            self.assertEqual(served_version(), "9")


class RemoteAccountBase(ManagementBase):
    def setUp(self):
        super().setUp()
        self.row = self.manager.save({"type": "claude", "name": "Claude (laptop)",
                                      "enabled": True,
                                      "config": {"settings_path": str(Path(self.temp.name) / "settings.json")}})
        ingest_module._WARNED.clear()
        self.addCleanup(ingest_module._WARNED.clear)

    def report(self, version, host="laptop"):
        body = envelope(host=host)
        body["collector"]["version"] = version
        return ingest_module.ingest(self.storage, self.row["id"], body)

    def source(self):
        return next(item for item in self.manager.list_sources()["sources"]
                    if item["id"] == self.row["id"])


class AccountPayloadTests(RemoteAccountBase):
    def test_an_old_copy_is_flagged_on_the_account(self):
        with serving("2"):
            self.assertTrue(self.report("1"))
            ingest = self.source()["ingest"]
        self.assertEqual(ingest["collector_version"], "1")
        self.assertEqual(ingest["collector_latest"], "2")
        self.assertEqual(ingest["collector_update"], "available")

    def test_a_current_copy_is_not(self):
        with serving("2"):
            self.report("2")
            self.assertEqual(self.source()["ingest"]["collector_update"], "current")

    def test_a_copy_newer_than_the_daemon_is_said_to_be_newer(self):
        with serving("2"):
            self.report("5")
            self.assertEqual(self.source()["ingest"]["collector_update"], "newer")

    def test_an_account_that_never_reported_makes_no_claim(self):
        ingest = self.source()["ingest"]
        self.assertIsNone(ingest["collector_version"])
        self.assertEqual(ingest["collector_update"], "unknown")

    def test_the_status_tab_shows_the_version_and_the_state(self):
        with serving("2"):
            self.report("1")
            rows = self.manager.status_view()["collectors"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["version"], "1")
        self.assertEqual(rows[0]["latest"], "2")
        self.assertEqual(rows[0]["state"], "update available")
        self.assertEqual(rows[0]["host"], "laptop")

    def test_the_status_tab_says_reporting_for_a_current_copy(self):
        with serving("2"):
            self.report("2")
            self.assertEqual(self.manager.status_view()["collectors"][0]["state"], "reporting")

    def test_the_status_tab_says_so_for_a_copy_newer_than_the_daemon(self):
        with serving("2"):
            self.report("7")
            self.assertEqual(self.manager.status_view()["collectors"][0]["state"],
                             "newer than this daemon")

    def test_an_account_with_no_reports_yet_is_not_called_out_of_date(self):
        with serving("2"):
            self.assertEqual(self.manager.status_view()["collectors"][0]["state"],
                             "no reports yet")

    def test_the_update_never_changes_the_accounts_status(self):
        with serving("2"):
            self.report("1")
            old = self.source()["status"]
        with serving("1"):
            self.report("1")
            self.assertEqual(self.source()["status"], old)


class ProblemsWarningTests(RemoteAccountBase):
    def test_an_old_copy_is_warned_about_once_with_what_to_do(self):
        with serving("2"):
            with self.assertLogs("presto_ai_dash.ingest", level="WARNING") as logged:
                self.report("1", host="desk-pc")
            text = "\n".join(logged.output)
            self.assertIn("desk-pc", text)
            self.assertIn("version 1", text)
            self.assertIn("version 2", text)
            self.assertIn("replace the file", text)
            with self.assertNoLogs("presto_ai_dash.ingest", level="WARNING"):
                for _ in range(5):
                    self.report("1", host="desk-pc")

    def test_a_different_old_version_is_warned_about_again(self):
        with serving("3"):
            with self.assertLogs("presto_ai_dash.ingest", level="WARNING"):
                self.report("1")
            with self.assertLogs("presto_ai_dash.ingest", level="WARNING"):
                self.report("2")

    def test_a_current_or_newer_copy_is_never_warned_about(self):
        with serving("2"), self.assertNoLogs("presto_ai_dash.ingest", level="WARNING"):
            self.report("2")
            self.report("9")

    def test_the_warning_reaches_the_problems_list(self):
        # The daemon attaches the buffer to the root logger when it starts.
        logging.getLogger().addHandler(self.manager.problems)
        self.addCleanup(logging.getLogger().removeHandler, self.manager.problems)
        with serving("2"):
            self.report("1", host="desk-pc")
            problems = self.manager.status_view()["problems"]
        messages = [problem["message"] for problem in problems]
        self.assertTrue(any("version 1" in message and "desk-pc" in message
                            for message in messages), messages)


class CardDetailTests(unittest.TestCase):
    def card(self, version, host="laptop"):
        windows = [window("five_hour", "5h", 12, NOW - timedelta(minutes=5),
                          NOW + timedelta(hours=4)),
                   window("seven_day", "Weekly", 34, NOW - timedelta(minutes=90),
                          NOW + timedelta(days=1), period=10080)]
        report = trail_report(windows, seen=NOW - timedelta(minutes=10), host=host,
                              progress=NOW - timedelta(minutes=90))
        if version is not None:
            report["collector_version"] = version
        snapshot = SourceSnapshot("c", "ok", NOW, {"quota": report})
        with patch("presto_ai_dash.dashboard.now_utc", return_value=NOW):
            return _account_card({"id": "c", "name": "Claude", "type": "claude"}, snapshot)

    def test_an_old_copy_gets_one_line_after_the_reports_line(self):
        with serving("2"):
            card = self.card("1")
        index = card.detail.index("Reports: laptop 10 min ago")
        self.assertEqual(card.detail[index + 1], "Collector v1 old, v2 ready")

    def test_the_line_fits_the_device_line_width(self):
        with serving("12"):
            card = self.card("9")
        line = next(item for item in card.detail if item.startswith("Collector v"))
        self.assertLessEqual(len(line), 34)
        self.assertTrue(line.isascii())

    def test_no_line_for_a_current_newer_or_unknown_copy(self):
        for version in ("2", "9", None, "2.1.296", "x"):
            with self.subTest(version=version), serving("2"):
                card = self.card(version)
                self.assertFalse(any(item.startswith("Collector v") for item in card.detail))

    def test_the_line_changes_neither_the_status_nor_the_accent(self):
        with serving("2"):
            old = self.card("1")
            current = self.card("2")
        self.assertEqual(old.status, current.status)
        self.assertEqual(old.accent, current.accent)
        self.assertEqual(old.primary, current.primary)


class FileAndVersionAgreeTests(unittest.TestCase):
    def test_what_the_admin_page_offers_is_what_the_daemon_compares_with(self):
        from presto_ai_dash.collectors import collector_info
        self.assertEqual(collector_info()["version"], served_version())
        with serving("6"):
            self.assertEqual(collector_info()["version"], "6")


if __name__ == "__main__":
    unittest.main()
