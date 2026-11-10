"""What the daemon and the displays say about the Presto Claude mod.

A reading from the mod is labelled as the mod's, the collector says whether the
mod is working on its computer (only where it was installed), and the freshness
hint and the admin lines name the right fix: reload VS Code for a mod that is
installed but not loaded, use Claude for a mod that is waiting, and never send a
VS Code user to open /usage or a terminal.
"""

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from presto_ai_dash.collectors import claude_mod, presto_collector as collector
from presto_ai_dash.ingest import SCHEMA_NAME, ingest, validate
from presto_ai_dash.quotas import (MAX_HINT_CHARS, claude_hint, claude_provenance, present_quota)
from presto_ai_dash.sources.claude import ClaudeSource
from admin.test_management import ManagementBase
from ingest.test_claude_mod_reader import NOW, when, write_file
from ingest.test_collector_watch import FolderBase, PushBase


def minutes_ago(minutes):
    return datetime.now(timezone.utc).replace(microsecond=0) - timedelta(minutes=minutes)


def watch_report(made, name="claude-watch", host="windows-pc", mod=None, percent=40.0):
    reset = made + timedelta(hours=3)
    body = {"schema": SCHEMA_NAME, "collector": {"name": name, "version": "3", "host": host},
            "client_time": made.isoformat(),
            "windows": [{"id": "five_hour", "label": "5h", "period_minutes": 300,
                         "used_percent": percent, "resets_at": reset.isoformat(),
                         "state": "available"},
                        {"id": "seven_day", "label": "Weekly", "period_minutes": 10080,
                         "used_percent": 20.0, "resets_at": (made + timedelta(days=3)).isoformat(),
                         "state": "available"}],
            "evidence": {"five_hour": "a" * 64, "seven_day": "b" * 64}, "progress": "c" * 64}
    if mod is not None:
        body["mod_status"] = {"state": mod}
    return body


def heartbeat(mod, host="windows-pc", name="claude-watch"):
    return {"schema": SCHEMA_NAME, "collector": {"name": name, "version": "3", "host": host},
            "client_time": datetime.now(timezone.utc).isoformat(), "mod_status": {"state": mod}}


class SourceBase(ManagementBase):
    def setUp(self):
        super().setUp()
        self.row = self.manager.save({"type": "claude", "name": "Claude", "enabled": True,
                                      "config": {"settings_path": str(Path(self.temp.name) / "s.json")}})
        self.codex = self.manager.save({"type": "codex", "name": "Codex", "enabled": True,
                                        "config": {}})
        self.id = self.row["id"]

    def push(self, body, received=None):
        return ingest(self.storage, self.id, body, now=received or datetime.now(timezone.utc))

    def quota(self):
        source = ClaudeSource({"settings_path": str(Path(self.temp.name) / "s.json")})
        source.id = self.id
        source.attach_storage(self.storage)
        return present_quota(source.collect().metrics["quota"], datetime.now(timezone.utc))


class IngestTests(SourceBase):
    def test_a_claude_collector_may_say_what_state_its_mod_is_in(self):
        for state in sorted(claude_mod_states()):
            validate(heartbeat(state), ClaudeSource)

    def test_anything_else_is_refused(self):
        for bad in ({"state": "broken"}, {"state": "working", "path": "C:/x"}, {},
                    "working", None, {"state": 1}):
            body = heartbeat("working")
            body["mod_status"] = bad
            with self.assertRaises(ValueError, msg=repr(bad)):
                validate(body, ClaudeSource)

    def test_a_source_that_has_no_mod_refuses_it(self):
        from presto_ai_dash.sources.codex import CodexSource
        with self.assertRaises(ValueError):
            validate(heartbeat("working"), CodexSource)

    def test_the_state_is_stored_and_survives_a_later_quota_report(self):
        self.push(heartbeat("loaded"))
        self.assertEqual(self.quota()["mod"]["state"], "loaded")
        self.push(watch_report(minutes_ago(1)))                      # windows only, no mod_status
        self.assertEqual(self.quota()["mod"]["state"], "loaded")

    def test_the_state_names_the_computer_that_reported_it(self):
        self.push(heartbeat("idle", host="macbook"))
        self.assertEqual(self.quota()["mod"]["reported_by"], "macbook")

    def test_a_collector_gone_quiet_is_no_longer_believed(self):
        self.push(heartbeat("not_loaded"), received=minutes_ago(11))
        self.assertNotIn("mod", self.quota())


class LabelTests(SourceBase):
    def sources(self):
        return {window["id"]: window.get("source") for window in self.quota()["windows"]}

    def test_windows_from_the_watch_collector_are_the_mods(self):
        self.push(watch_report(minutes_ago(1)))
        self.assertEqual(set(self.sources().values()), {"claude mod"})

    def test_windows_from_the_status_line_are_still_the_terminal_export(self):
        self.push(watch_report(minutes_ago(1), name="claude-statusline"))
        self.assertEqual(set(self.sources().values()), {"terminal export"})

    def test_a_newer_status_line_reading_replaces_the_mods_and_its_label(self):
        self.push(watch_report(minutes_ago(5)))
        self.push(watch_report(minutes_ago(1), name="claude-statusline", percent=45.0))
        windows = {window["id"]: window for window in self.quota()["windows"]}
        self.assertEqual(windows["five_hour"]["source"], "terminal export")
        self.assertEqual(windows["five_hour"]["used_percent"], 45.0)

    def note(self):
        source = ClaudeSource({"settings_path": str(Path(self.temp.name) / "s.json")})
        source.id = self.id
        source.attach_storage(self.storage)
        return source.collect().metrics["quota"]["note"]

    def test_a_reading_from_the_mod_is_not_described_as_terminal_only(self):
        self.push(watch_report(minutes_ago(1)))
        note = self.note()
        self.assertIn("mod", note)
        self.assertNotIn("does not run the status line", note)
        self.assertNotIn("terminal", note)

    def test_a_status_line_reading_keeps_the_terminal_wording(self):
        self.push(watch_report(minutes_ago(1), name="claude-statusline"))
        self.assertIn("terminal export", self.note())


def claude_mod_states():
    from presto_ai_dash.ingest_extras import MOD_STATES
    return MOD_STATES


def report_with(mod, observed_minutes_ago=None, **extra):
    """A Claude report as the display layer sees it, quota age and mod state chosen."""
    now = datetime.now(timezone.utc)
    windows = []
    for key, label, minutes in (("five_hour", "5h", 300), ("seven_day", "Weekly", 10080)):
        if observed_minutes_ago is None:
            windows.append({"id": key, "label": label, "period_minutes": minutes,
                            "used_percent": None, "resets_at": None, "observed_at": now.isoformat(),
                            "state": "unavailable"})
        else:
            windows.append({"id": key, "label": label, "period_minutes": minutes,
                            "used_percent": 30.0, "state": "available",
                            "resets_at": (now + timedelta(hours=3)).isoformat(),
                            "observed_at": (now - timedelta(minutes=observed_minutes_ago)).isoformat()})
    report = {"windows": windows, "stale_after_seconds": 900, "error": None,
              "collector_seen_at": now.isoformat(), "collector_host": "windows-pc"}
    if mod is not None:
        report["mod"] = {"state": mod, "reported_by": "windows-pc"}
    report.update(extra)
    return report


class HintTests(unittest.TestCase):
    def test_a_mod_that_is_installed_but_not_loaded_says_to_reload(self):
        self.assertEqual(claude_hint(report_with("not_loaded", 120)), "Reload VS Code (Claude mod)")
        self.assertEqual(claude_hint(report_with("not_loaded")), "Reload VS Code (Claude mod)")

    def test_unreadable_files_are_named(self):
        self.assertEqual(claude_hint(report_with("unreadable", 120)), "Claude mod files unreadable")

    def test_an_idle_mod_says_to_use_claude_not_to_open_usage_or_a_terminal(self):
        hint = claude_hint(report_with("idle", 120))
        self.assertEqual(hint, "Quota 2h old - use Claude")
        for wrong in ("/usage", "terminal", "run claude"):
            self.assertNotIn(wrong.lower(), hint.lower())

    def test_a_mod_with_no_reading_yet_waits_for_a_reply(self):
        self.assertEqual(claude_hint(report_with("loaded")), "Waiting for a Claude reply")

    def test_a_fresh_reading_needs_no_hint(self):
        self.assertIsNone(claude_hint(report_with("working", 2)))
        self.assertIsNone(claude_hint(report_with("not_loaded", 2)))

    def test_every_hint_fits_the_screen(self):
        for state in sorted(claude_mod_states()):
            for age in (None, 20, 90, 3000, 60 * 24 * 40):
                hint = claude_hint(report_with(state, age))
                self.assertTrue(hint is None or (len(hint) <= MAX_HINT_CHARS and hint.isascii()),
                                (state, age, hint))

    def test_an_account_without_the_mod_is_unchanged(self):
        self.assertEqual(claude_hint(report_with(None, 120)), "Reply in claude to refresh")

    def test_an_expired_window_still_says_awaiting_refresh(self):
        report = report_with("idle", 120)
        for window in report["windows"]:
            window["resets_at"] = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        self.assertEqual(claude_hint(report), "Awaiting refresh")


class ProvenanceTests(unittest.TestCase):
    def lines(self, mod, **windows):
        report = report_with(mod, 5)
        for window in report["windows"]:
            window["source"] = "claude mod"
        return claude_provenance(report, {})

    def test_the_quota_line_names_the_mod_and_the_computer(self):
        self.assertIn("Quota: claude mod", self.lines("working")["quota"])
        self.assertIn("from windows-pc", self.lines("working")["quota"])

    def test_each_state_has_a_plain_line(self):
        for state in sorted(claude_mod_states()):
            line = self.lines(state)["mod"]
            self.assertTrue(line.startswith("Claude mod on windows-pc: "), line)
            self.assertTrue(line.isascii())
        self.assertIn("reload the VS Code window", self.lines("not_loaded")["mod"])
        self.assertIn("claude-mod status", self.lines("unreadable")["mod"])

    def test_no_mod_no_line(self):
        self.assertIsNone(claude_provenance(report_with(None, 5), {})["mod"])


class CardTests(unittest.TestCase):
    """The narrow detail card names the route of the newest reading."""

    def trail(self, source):
        from presto_ai_dash.dashboard import _trail_lines
        report = report_with(None, 5)
        for window in report["windows"]:
            window["source"] = source
        report["last_progress_at"] = None
        return _trail_lines(report, datetime.now(timezone.utc))

    def test_a_mod_reading_says_mod(self):
        self.assertEqual(self.trail("claude mod")[0], "Quota via mod, 5 min old")

    def test_the_other_routes_are_unchanged(self):
        self.assertEqual(self.trail("terminal export")[0], "Quota via export, 5 min old")
        self.assertEqual(self.trail("usage cache")[0], "Quota via cache, 5 min old")


class CollectorStateTests(unittest.TestCase):
    def test_what_the_reader_says_becomes_a_state(self):
        table = {("reading", False): "working", ("waiting for a response", False): "idle",
                 ("loaded, no reading yet", False): "loaded",
                 ("not installed", False): None, ("no sessions yet", False): None,
                 ("unreadable files", False): None,
                 ("not installed", True): "not_loaded", ("no sessions yet", True): "not_loaded",
                 ("unreadable files", True): "unreadable", ("reading", True): "working"}
        for (status, expected), state in table.items():
            self.assertEqual(claude_mod.daemon_state(status, expected), state, (status, expected))

    def test_every_state_the_collector_can_send_is_one_the_daemon_takes(self):
        sent = {claude_mod.daemon_state(status, expected)
                for status in ("reading", "waiting for a response", "loaded, no reading yet",
                               "not installed", "no sessions yet", "unreadable files")
                for expected in (True, False)} - {None}
        self.assertTrue(sent <= claude_mod_states())


class AccountTests(FolderBase):
    def mod_account(self):
        return self.account(activity=False, usage_cache=False, mod=True,
                            mod_folder=str(self.root / ".presto-ai-dash" / "claude-quota"))

    def mark_installed(self):
        record = self.root / ".presto-ai-dash" / "claude-mod" / claude_mod.RECORD_NAME
        record.parent.mkdir(parents=True, exist_ok=True)
        record.write_text("{}")

    def envelope(self):
        return self.mod_account().envelope("laptop", NOW)[0]

    def test_where_the_mod_was_never_installed_nothing_is_said(self):
        self.assertNotIn("mod_status", self.envelope())

    def test_an_installed_mod_that_has_written_nothing_is_not_loaded(self):
        self.mark_installed()
        envelope = self.envelope()
        self.assertEqual(envelope["mod_status"], {"state": "not_loaded"})
        validate(envelope, ClaudeSource)

    def test_a_mod_that_is_writing_is_working_even_without_an_install_record(self):
        write_file(self.root / ".presto-ai-dash" / "claude-quota", seq=2, observed=when(30))
        envelope = self.envelope()
        self.assertEqual(envelope["mod_status"], {"state": "working"})
        self.assertEqual(len(envelope["windows"]), 2)
        validate(envelope, ClaudeSource)

    def test_a_loaded_mod_with_no_reading_yet(self):
        write_file(self.root / ".presto-ai-dash" / "claude-quota", seq=1)
        self.assertEqual(self.envelope()["mod_status"], {"state": "loaded"})

    def test_a_loaded_mod_waiting_for_a_response(self):
        write_file(self.root / ".presto-ai-dash" / "claude-quota", seq=2, observed=when(30))
        account = self.mod_account()
        account.envelope("laptop", NOW)
        account.mod_reader.acknowledge()
        self.assertEqual(account.envelope("laptop", NOW)[0]["mod_status"], {"state": "idle"})


class OlderDaemonTests(FolderBase):
    """A collector newer than its daemon: the daemon has never heard of mod_status."""

    class Daemon:
        """Stands in for a daemon from before the mod: it refuses the key it does not know."""

        def __init__(self):
            self.bodies = []

        def open(self, request, timeout=None):
            import io
            import urllib.error
            body = json.loads(request.data.decode("utf-8"))
            self.bodies.append(body)
            if "mod_status" in body:
                raise urllib.error.HTTPError(
                    request.full_url, 400, "Bad Request", {},
                    io.BytesIO(b'{"error": "unknown top-level key: mod_status"}'))
            return self

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self, size=-1):
            return b""

    def setUp(self):
        super().setUp()
        (self.root / "claude.token").write_text("token", encoding="utf-8")
        write_file(self.root / "claude-quota", seq=2, observed=when(30))
        self.account_settings = dict(
            service="claude", name="claude", token_file=str(self.root / "claude.token"),
            settings_path=str(self.profile / "settings.json"), activity=False, usage_cache=False,
            usage_cache_path="", mod=True, mod_folder=str(self.root / "claude-quota"))
        self.config = {"url": "http://old-daemon/api/v1/ingest", "host": "laptop", "interval": 60}
        self.daemon = self.Daemon()
        self.account_object = collector.Account(self.account_settings)

    def round(self, now):
        import io
        out = io.StringIO()
        sent = collector.run_round(self.config, [self.account_object], now=now, out=out,
                                   opener=self.daemon)
        return sent, out.getvalue()

    def test_the_refusal_is_remembered_and_the_next_round_goes_without_the_key(self):
        sent, output = self.round(NOW)
        self.assertEqual(sent, 0)
        self.assertIn("mod_status", output)
        self.assertTrue(self.account_object.mod_status_refused)
        sent, output = self.round(NOW + 1000)
        self.assertEqual(sent, 1)
        self.assertNotIn("mod_status", self.daemon.bodies[-1])
        self.assertEqual(len(self.daemon.bodies[-1]["windows"]), 2)       # the quota still goes

    def test_the_user_is_told_the_daemon_is_old_and_only_once(self):
        self.round(NOW)
        _, first = self.round(NOW + 1000)
        _, second = self.round(NOW + 2000)
        self.assertIn("older than this collector", first)
        self.assertIn("update the daemon", first)
        self.assertNotIn("older than this collector", second)

    def test_other_refusals_do_not_switch_it_off(self):
        import io
        import urllib.error

        class Refusing(self.Daemon):
            def open(self, request, timeout=None):
                raise urllib.error.HTTPError(request.full_url, 400, "Bad Request", {},
                                             io.BytesIO(b'{"error": "unknown top-level key: other"}'))

        self.daemon = Refusing()
        self.round(NOW)
        self.assertFalse(self.account_object.mod_status_refused)

    def test_a_current_daemon_is_never_asked_to_do_without_it(self):
        class Current(self.Daemon):
            def open(self, request, timeout=None):
                self.bodies.append(json.loads(request.data.decode("utf-8")))
                return self

        self.daemon = Current()
        sent, _ = self.round(NOW)
        self.assertEqual(sent, 1)
        self.assertIn("mod_status", self.daemon.bodies[0])
        self.assertFalse(self.account_object.mod_status_refused)


class UnreachableDaemonTests(OlderDaemonTests):
    """The daemon cannot be reached for a round: the reading is sent when it can be."""

    class Flaky(OlderDaemonTests.Daemon):
        def __init__(self, failures):
            OlderDaemonTests.Daemon.__init__(self)
            self.failures = failures

        def open(self, request, timeout=None):
            import urllib.error
            self.bodies.append(json.loads(request.data.decode("utf-8")))
            if self.failures:
                self.failures -= 1
                raise urllib.error.URLError("unreachable")
            return self

    def test_a_reading_that_could_not_be_sent_is_not_lost(self):
        self.daemon = self.Flaky(failures=1)
        sent, _ = self.round(NOW)
        self.assertEqual(sent, 0)
        sent, _ = self.round(NOW + 1000)
        self.assertEqual(sent, 1)
        self.assertEqual(len(self.daemon.bodies[-1]["windows"]), 2)
        self.assertEqual(self.daemon.bodies[-1]["windows"], self.daemon.bodies[0]["windows"])

    def test_once_delivered_it_is_not_sent_again(self):
        self.daemon = self.Flaky(failures=0)
        self.round(NOW)
        self.round(NOW + 1000)
        self.assertIn("windows", self.daemon.bodies[0])
        self.assertNotIn("windows", self.daemon.bodies[1])           # a heartbeat, nothing new

    # The older-daemon cases above are not repeated for this class.
    test_the_refusal_is_remembered_and_the_next_round_goes_without_the_key = None
    test_the_user_is_told_the_daemon_is_old_and_only_once = None
    test_other_refusals_do_not_switch_it_off = None
    test_a_current_daemon_is_never_asked_to_do_without_it = None


class EndToEndTests(PushBase):
    def run_and_collect(self):
        self.assertEqual(self.run_watch(self.config_for("claude"))[0], 0)
        source = self.claude_source()
        return present_quota(source.collect().metrics["quota"], datetime.now(timezone.utc))

    def test_an_installed_but_silent_mod_tells_the_admin_page_and_the_screen(self):
        record = self.folder / "claude-mod" / claude_mod.RECORD_NAME
        record.parent.mkdir(parents=True)
        record.write_text("{}")
        quota = self.run_and_collect()
        self.assertEqual(quota["mod"]["state"], "not_loaded")
        self.assertEqual(claude_hint(quota), "Reload VS Code (Claude mod)")
        lines = claude_provenance(quota, {})
        self.assertIn("installed but not loaded", lines["mod"])

    def test_a_working_mod_reaches_the_daemon_labelled_and_attributed(self):
        write_file(self.folder / "claude-quota", seq=2, observed=when(60))
        quota = self.run_and_collect()
        self.assertEqual(quota["mod"]["state"], "working")
        self.assertEqual({window["source"] for window in quota["windows"]}, {"claude mod"})
        self.assertIn("Quota: claude mod", claude_provenance(quota, {})["quota"])


if __name__ == "__main__":
    unittest.main()
