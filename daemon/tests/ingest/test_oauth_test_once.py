"""The one-shot CLI uses synthetic credentials and a loopback daemon only."""

import io
from pathlib import Path
from unittest.mock import patch

from ingest.test_oauth_usage_push import WatchBase, SECRET, REFRESH, Opener, http_error, session_line
from presto_ai_dash.collectors import presto_collector as collector


class OneShotTests(WatchBase):
    def run_once(self, confirm=True, **changes):
        path = self.write_config(accounts=[self.oauth_entry(**changes)], url=self.url)
        self.write_session(session_line(seconds_ago=10))
        output = io.StringIO()
        with patch.object(collector, "_OPENER", self.opener), \
                patch.object(collector.time, "sleep"):
            code = collector.oauth_test_once(path, confirm=confirm, out=output)
        for secret in (SECRET, REFRESH, self.claude_token, "Authorization", "Bearer"):
            self.assertNotIn(secret, output.getvalue())
        return code, output.getvalue()

    def test_confirmation_required_before_even_loading_config(self):
        with patch.object(collector, "load_config", side_effect=AssertionError("read")):
            output = io.StringIO()
            self.assertEqual(collector.oauth_test_once("absent", out=output), 2)
        self.assertEqual(self.fake.requests, [])

    def test_success_reports_http_timing_quotas_source_and_ingestion(self):
        code, output = self.run_once()
        self.assertEqual(code, 0, output)
        self.assertEqual(len(self.fake.requests), 1)
        for text in ("HTTP status: 200", "elapsed:", "five_hour: 34", "seven_day: 53",
                     "selected source (collector estimate): oauth", "ingestion succeeded: yes"):
            self.assertIn(text, output)
        self.assertIsNotNone(self.storage.observation(self.claude["id"])["report"]
                             .get("pushed_oauth_usage"))

    def test_second_invocation_cannot_bypass_cooldown(self):
        self.run_once()
        code, output = self.run_once()
        self.assertEqual(code, 1)
        self.assertIn("oauth cooldown", output)
        self.assertEqual(len(self.fake.requests), 1)

    def test_no_provider_request_without_daemon_acceptance(self):
        self.set_ingest(False)
        code, output = self.run_once()
        self.assertEqual(code, 1)
        self.assertIn("ingestion succeeded: no", output)
        self.assertEqual(self.fake.requests, [])

    def test_old_daemon_without_capability_header_costs_no_request(self):
        with patch.object(collector, "send", return_value=None):
            code, output = self.run_once()
        self.assertEqual(code, 1)
        self.assertIn("did not confirm", output)
        self.assertEqual(self.fake.requests, [])

    def test_off_and_simulated_configuration_refused(self):
        for changes in ({"oauth_usage": False}, {"oauth_usage_dry_run": True}):
            self.assertEqual(self.run_once(**changes)[0], 1)
        self.assertEqual(self.fake.requests, [])

    def test_multiple_accounts_need_explicit_selection(self):
        path = self.write_config(accounts=[self.oauth_entry(name="one"),
            self.oauth_entry(name="two", token_file="other.token")], url=self.url)
        output = io.StringIO()
        self.assertEqual(collector.oauth_test_once(path, True, out=output), 1)
        self.assertIn("select exactly one", output.getvalue())
        self.assertEqual(self.fake.requests, [])

    def test_401_stop_and_429_backoff_survive_invocation(self):
        self.fake.replies = [http_error(429)]
        code, output = self.run_once()
        self.assertEqual(code, 1)
        self.assertIn("HTTP status: 429", output)
        self.assertIn("oauth backoff", self.run_once()[1])
        self.assertEqual(len(self.fake.requests), 1)

    def test_ingestion_failure_does_not_retry_provider(self):
        with patch.object(collector, "send", side_effect=["none", collector.Problem("unavailable")]):
            code, output = self.run_once()
        self.assertEqual(code, 1)
        self.assertIn("ingestion succeeded: no", output)
        self.assertEqual(len(self.fake.requests), 1)

    def test_manual_attestation_requires_its_own_confirmation(self):
        path = self.write_config(accounts=[self.oauth_entry()], url=self.url)
        with patch.object(self.reader, "read_access_token", side_effect=AssertionError("opened")):
            self.assertEqual(collector.oauth_attest(path, account_name="claude", out=io.StringIO()), 2)
        self.assertEqual(collector.oauth_attest(path, True, "claude", io.StringIO()), 0)
        self.assertEqual(self.fake.requests, [])

    def test_dry_run_does_not_create_user_state_or_open_any_token_file(self):
        path = self.write_config(accounts=[self.oauth_entry()], url=self.url)
        state_root = self.folder / "new-state-root"
        output = io.StringIO()
        # Restore the actual state locator, with both OS-specific roots pointed
        # inside this test's temporary folder. No real home directory is used.
        import os
        import runpy
        from unittest.mock import Mock
        clean = runpy.run_path(collector.__file__, run_name="collector_dry_test")
        globals_ = clean["oauth_command"].__globals__
        reader = clean["_embedded"]("claude_oauth_usage")
        with patch.dict(os.environ, {"LOCALAPPDATA": str(state_root),
                                     "XDG_STATE_HOME": str(state_root)}), \
                patch.dict(globals_, {"_read_token": Mock(side_effect=AssertionError("token read"))}), \
                patch.object(reader, "read_access_token", side_effect=AssertionError("credential read")):
            self.assertEqual(clean["oauth_command"]("dry-run", path, output), 0)
        self.assertFalse(state_root.exists())

    def test_remote_token_replacement_suspends_collection(self):
        (self.folder / "claude.token").write_text(self.codex_token)
        code, output = self.run_once()
        self.assertEqual(code, 1)  # wrong source also refuses capability preflight
        self.assertEqual(self.fake.requests, [])
