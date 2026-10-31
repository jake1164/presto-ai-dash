"""Standalone Claude status-line collector: envelope, throttle, chaining (plan Phase 2)."""

from datetime import datetime, timedelta, timezone
import base64
import http.server
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from presto_ai_dash.collectors import presto_collector as collector

NOW = datetime(2026, 10, 18, 14, 0, 0, tzinfo=timezone.utc)


def payload(percent=23.5):
    return {"session_id": "private-session-identity", "prompt_id": "private-prompt",
            "transcript_path": "/private/transcript", "version": "2.1.296",
            "cost": {"total_api_duration_ms": 300, "total_duration_ms": 1000},
            "context_window": {"total_input_tokens": 600, "total_output_tokens": 100},
            "rate_limits": {"five_hour": {"used_percentage": percent,
                "resets_at": int((NOW + timedelta(hours=5)).timestamp())},
                "seven_day": {"used_percentage": 41.2,
                "resets_at": int((NOW + timedelta(days=7)).timestamp())}}}


def run_main(args, raw, state_path, output=None):
    """Call the collector's main() with scripted stdin and captured stdout."""
    stdin = io.TextIOWrapper(io.BytesIO(raw if isinstance(raw, bytes) else raw.encode("utf-8")))
    with patch("sys.stdin", stdin), patch("sys.stdout", output or io.StringIO()):
        return collector.main(args, state_path=state_path)


class RecordingServer(object):
    """A local HTTP server recording requests, optionally redirecting or slow."""

    def __init__(self, delay=0.0, redirect=None):
        self.recorded = []
        self.delay = delay
        self.redirect = redirect
        owner = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                owner.recorded.append((dict(self.headers), b""))
                self.send_response(204)
                self.end_headers()

            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length)
                owner.recorded.append((dict(self.headers), body))
                if owner.redirect:
                    self.send_response(owner.redirect[0])
                    self.send_header("Location", owner.redirect[1])
                    self.end_headers()
                    return
                if owner.delay:
                    time.sleep(owner.delay)
                self.send_response(204)
                self.end_headers()

            def log_message(self, *args):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, args=(0.02,), daemon=True)
        self.thread.start()

    @property
    def url(self):
        return "http://127.0.0.1:%d/ingest" % self.server.server_address[1]

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class CollectorBase(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.temp = Path(temp.name)
        self.state = str(self.temp / "state.json")
        self.server = RecordingServer()
        self.addCleanup(self.server.close)
        self.token_file = self.temp / "claude.token"
        self.token_file.write_text("secret-token-value\n", encoding="utf-8")

    def args(self, host="laptop"):
        return ["claude-statusline", "--url", self.server.url, "--token-file", str(self.token_file), "--host", host]


class EnvelopeTests(unittest.TestCase):
    def test_fresh_payload_builds_the_documented_envelope(self):
        envelope = collector.build_envelope(payload(), "laptop", client_time=NOW)
        self.assertEqual(envelope["schema"], "presto-ai-dash.observation/1")
        self.assertEqual(envelope["collector"], {"name": "claude-statusline",
                                                 "version": "1", "host": "laptop"})
        self.assertEqual(envelope["client_time"], NOW.isoformat())
        self.assertEqual(envelope["provider_version"], "2.1.296")
        self.assertEqual([w["id"] for w in envelope["windows"]], ["five_hour", "seven_day"])
        self.assertEqual([w["used_percent"] for w in envelope["windows"]], [23.5, 41.2])
        self.assertEqual(envelope["windows"][0]["resets_at"],
                         (NOW + timedelta(hours=5)).isoformat())
        self.assertEqual(envelope["windows"][0]["period_minutes"], 300)
        self.assertEqual(envelope["windows"][1]["period_minutes"], 10080)
        self.assertEqual(len(envelope["evidence"]["five_hour"]), 64)
        self.assertEqual(len(envelope["progress"]), 64)
        self.assertNotIn("private", json.dumps(envelope))

    def test_no_rate_limits_is_a_heartbeat_not_a_crash(self):
        self.assertIsNone(collector.build_envelope({"version": "2.1.296"}, "laptop"))
        heartbeat = collector.heartbeat_envelope("laptop", client_time=NOW)
        self.assertEqual(heartbeat, {"schema": "presto-ai-dash.observation/1",
                                     "collector": {"name": "claude-statusline",
                                                   "version": "1", "host": "laptop"},
                                     "client_time": NOW.isoformat()})
        self.assertNotIn("windows", heartbeat)

    def test_partial_windows_stay_unavailable(self):
        data = payload()
        data["rate_limits"].pop("seven_day")
        envelope = collector.build_envelope(data, "laptop", client_time=NOW)
        self.assertEqual(len(envelope["windows"]), 2)
        self.assertEqual(envelope["windows"][1]["state"], "unavailable")
        self.assertIsNone(envelope["windows"][1]["used_percent"])
        self.assertIsNone(envelope["windows"][1]["resets_at"])

    def test_missing_or_bad_quota_values_raise(self):
        for name, mutate in (
                ("null percent", lambda d: d["rate_limits"]["five_hour"].update(
                    used_percentage=None)),
                ("string reset", lambda d: d["rate_limits"]["five_hour"].update(
                    resets_at="tomorrow")),
                ("non-dict window", lambda d: d["rate_limits"].update(five_hour=[])),
                ("non-dict limits", lambda d: d.update(rate_limits=[]))):
            data = payload()
            mutate(data)
            with self.subTest(name=name), self.assertRaises((ValueError, TypeError)):
                collector.build_envelope(data, "laptop")

    def test_same_input_produces_identical_evidence(self):
        first = collector.build_envelope(payload(), "laptop", client_time=NOW)
        second = collector.build_envelope(payload(), "laptop", client_time=NOW)
        self.assertEqual(first, second)
        changed = payload()
        changed["cost"]["total_api_duration_ms"] += 1
        third = collector.build_envelope(changed, "laptop", client_time=NOW)
        self.assertNotEqual(first["progress"], third["progress"])
        self.assertNotEqual(first["evidence"], third["evidence"])


class ThrottleRulesTests(unittest.TestCase):
    def test_heartbeats_within_a_minute_are_skipped(self):
        envelope = collector.heartbeat_envelope("laptop", client_time=NOW)
        state = {"heartbeat_at": NOW.timestamp() - 10}
        self.assertFalse(collector.should_post(envelope, state, NOW.timestamp()))
        state = {"heartbeat_at": NOW.timestamp() - 61}
        self.assertTrue(collector.should_post(envelope, state, NOW.timestamp()))

    def test_identical_envelope_within_a_minute_is_skipped(self):
        envelope = collector.build_envelope(payload(), "laptop", client_time=NOW)
        state = collector.state_after(envelope, {}, NOW.timestamp() - 10)
        self.assertFalse(collector.should_post(envelope, state, NOW.timestamp()))
        changed = collector.build_envelope(payload(percent=50.0), "laptop", client_time=NOW)
        self.assertTrue(collector.should_post(changed, state, NOW.timestamp()))
        self.assertTrue(collector.should_post(envelope, state, NOW.timestamp() + 61))

    def test_network_failure_backs_off_every_post_for_five_minutes(self):
        envelope = collector.build_envelope(payload(), "laptop", client_time=NOW)
        state = {"backoff_until": NOW.timestamp() + 100}
        self.assertFalse(collector.should_post(envelope, state, NOW.timestamp()))
        changed = collector.build_envelope(payload(percent=70.0), "laptop", client_time=NOW)
        self.assertFalse(collector.should_post(changed, state, NOW.timestamp()))
        self.assertTrue(collector.should_post(changed, state, NOW.timestamp() + 101))


class RedirectTests(CollectorBase):
    def test_redirect_is_refused_and_counts_as_a_failed_post(self):
        for code in (301, 302, 303, 307, 308):
            with self.subTest(code=code):
                target = RecordingServer()
                self.addCleanup(target.close)
                source = RecordingServer(redirect=(code, target.url))
                self.addCleanup(source.close)
                state = str(self.temp / ("redirect-%d.json" % code))
                args = ["claude-statusline", "--url", source.url, "--token-file", str(self.token_file),
                        "--host", "laptop"]
                output = io.StringIO()
                self.assertEqual(run_main(args, json.dumps(payload()), state, output), 0)
                self.assertIn("5h 23.5%", output.getvalue())
                self.assertEqual(len(source.recorded), 1)
                # Not one request reaches the redirect target, not even a GET.
                self.assertEqual(target.recorded, [])
                stored = json.loads(Path(state).read_text(encoding="utf-8"))
                self.assertGreater(stored["backoff_until"], time.time())


class StateLocationTests(CollectorBase):
    def _environment(self, root):
        if os.name == "nt":
            return patch.dict(os.environ, {"LOCALAPPDATA": str(root)})
        return patch.dict(os.environ, {"XDG_STATE_HOME": str(root)})

    def test_state_file_lives_under_the_per_user_directory(self):
        root = self.temp / "user-state"
        with self._environment(root):
            path = collector._state_path("http://server:8787", "laptop")
        self.assertTrue(str(path).startswith(str(root)))
        self.assertIn("presto-ai-dash", Path(path).parts)
        if os.name != "nt":
            self.assertEqual(os.stat(root / "presto-ai-dash").st_mode & 0o777, 0o700)

    @unittest.skipIf(os.name == "nt", "the HOME fallback exists on POSIX only")
    def test_state_directory_falls_back_to_dot_local_state(self):
        home = self.temp / "home"
        with patch.dict(os.environ, {"HOME": str(home)}):
            os.environ.pop("XDG_STATE_HOME", None)
            path = collector._state_path("http://server:8787", "laptop")
        self.assertEqual(Path(path).parent, home / ".local" / "state" / "presto-ai-dash")

    def test_unusable_state_directory_falls_back_without_raising(self):
        blocker = self.temp / "blocker"
        blocker.write_text("a file, not a directory", encoding="utf-8")
        with self._environment(blocker / "state"):
            self.assertIsNone(collector._state_path("http://server:8787", "laptop"))
            output = io.StringIO()
            self.assertEqual(run_main(self.args(), json.dumps(payload()),
                                      None, output), 0)
        self.assertIn("5h 23.5%", output.getvalue())
        self.assertEqual(len(self.server.recorded), 1)

    def test_missing_state_location_means_no_throttle_without_raising(self):
        self.assertEqual(collector._load_state(None), {})
        collector._save_state(None, {"digest": "x"})
        missing = self.temp / "no-such-dir" / "state.json"
        self.assertEqual(collector._load_state(str(missing)), {})
        collector._save_state(str(missing), {"digest": "x"})

    def test_state_file_symlink_is_not_followed(self):
        target = self.temp / "target.json"
        target.write_text('{"backoff_until": 0}', encoding="utf-8")
        link = self.temp / "link.json"
        try:
            os.symlink(str(target), str(link))
        except (OSError, NotImplementedError):
            # Windows without symlink privilege: emulate a planted symlink so
            # the collector's refusal still runs on this machine.
            link.write_text("{}", encoding="utf-8")
            planted = link.read_text(encoding="utf-8")
            real_islink = os.path.islink
            with patch("os.path.islink", side_effect=lambda path: (
                    True if str(path) == str(link) else real_islink(path))):
                self.assertEqual(run_main(self.args(), json.dumps(payload()), str(link)), 0)
        else:
            planted = link.read_text(encoding="utf-8")
            self.assertEqual(run_main(self.args(), json.dumps(payload()), str(link)), 0)
        self.assertEqual(link.read_text(encoding="utf-8"), planted)
        self.assertEqual(target.read_text(encoding="utf-8"), '{"backoff_until": 0}')
        self.assertEqual(len(self.server.recorded), 1)


class DeliveryTests(CollectorBase):
    def test_posts_the_envelope_with_the_file_token(self):
        self.assertEqual(run_main(self.args(), json.dumps(payload()), self.state), 0)
        self.assertEqual(len(self.server.recorded), 1)
        headers, body = self.server.recorded[0]
        self.assertEqual(headers["Authorization"], "Bearer secret-token-value")
        self.assertEqual(headers["Content-Type"], "application/json")
        envelope = json.loads(body)
        self.assertEqual(envelope["collector"]["host"], "laptop")
        self.assertEqual(envelope["windows"][0]["used_percent"], 23.5)
        self.assertNotIn("secret-token-value", body.decode("utf-8"))

    def test_identical_rerun_is_throttled_but_a_changed_reading_posts(self):
        raw = json.dumps(payload())
        self.assertEqual(run_main(self.args(), raw, self.state), 0)
        self.assertEqual(run_main(self.args(), raw, self.state), 0)
        self.assertEqual(len(self.server.recorded), 1)
        self.assertEqual(run_main(self.args(), json.dumps(payload(percent=55.0)), self.state), 0)
        self.assertEqual(len(self.server.recorded), 2)

    def test_garbage_or_oversized_stdin_posts_a_heartbeat_and_exits_zero(self):
        cases = ("not json at all", "{", "x" * (collector.MAX_INPUT + 10))
        for raw in cases:
            with self.subTest(raw=raw[:20]):
                self.assertEqual(run_main(self.args(), raw, self.state), 0)
                envelope = json.loads(self.server.recorded[-1][1])
                self.assertNotIn("windows", envelope)
                self.assertEqual(envelope["collector"]["host"], "laptop")

    def test_no_rate_limits_posts_a_heartbeat(self):
        self.assertEqual(run_main(self.args(), json.dumps({"version": "2.1.296"}),
                                  self.state), 0)
        envelope = json.loads(self.server.recorded[0][1])
        self.assertNotIn("windows", envelope)
        self.assertEqual(envelope["schema"], "presto-ai-dash.observation/1")

    def test_missing_token_file_exits_zero_without_posting(self):
        self.token_file.unlink()
        self.assertEqual(run_main(self.args(), json.dumps(payload()), self.state), 0)
        self.assertEqual(self.server.recorded, [])

    def test_unreachable_server_exits_zero_and_backs_off(self):
        args = ["claude-statusline", "--url", "http://127.0.0.1:9/ingest", "--token-file", str(self.token_file),
                "--host", "laptop"]
        started = time.time()
        self.assertEqual(run_main(args, json.dumps(payload()), self.state), 0)
        self.assertLess(time.time() - started, 5)
        state = json.loads(Path(self.state).read_text(encoding="utf-8"))
        self.assertGreater(state["backoff_until"], time.time())
        # While backing off nothing is sent, even for a changed reading.
        run_main(self.args(), json.dumps(payload(percent=90.0)), self.state)
        self.assertEqual(self.server.recorded, [])

    def test_silent_server_times_out_quickly_and_exits_zero(self):
        slow = RecordingServer(delay=10)
        self.addCleanup(slow.close)
        args = ["claude-statusline", "--url", slow.url, "--token-file", str(self.token_file), "--host", "laptop"]
        started = time.time()
        self.assertEqual(run_main(args, json.dumps(payload()), self.state), 0)
        self.assertLess(time.time() - started, 5)
        state = json.loads(Path(self.state).read_text(encoding="utf-8"))
        self.assertGreater(state["backoff_until"], time.time())


class ChainingTests(CollectorBase):
    def test_chained_stdout_is_printed_end_to_end(self):
        script = self.temp / "prior.py"
        script.write_text("import sys,json; print('kept:'+json.load(sys.stdin)['version'])")
        command = [sys.executable, str(Path(collector.__file__).resolve()), "claude-statusline",
                   "--url", self.server.url, "--token-file", str(self.token_file),
                   "--host", "laptop", "--", sys.executable, str(script)]
        result = subprocess.run(command, input=json.dumps(payload()).encode("utf-8"),
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), b"kept:2.1.296")
        self.assertEqual(len(self.server.recorded), 1)

    def test_without_a_previous_command_the_status_text_is_printed(self):
        command = [sys.executable, str(Path(collector.__file__).resolve())] + self.args()
        result = subprocess.run(command, input=json.dumps(payload()).encode("utf-8"),
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(b"5h 23.5%", result.stdout)

    def test_a_failing_previous_command_still_exits_zero_and_posts(self):
        args = self.args() + ["--", str(self.temp / "missing-binary")]
        self.assertEqual(run_main(args, json.dumps(payload()), self.state), 0)
        self.assertEqual(len(self.server.recorded), 1)


class SnippetTests(CollectorBase):
    def test_posix_snippet_quotes_every_path(self):
        snippet = collector.posix_snippet("/opt/presto collector/presto_collector.py",
                                          "http://server:8787", "~/.config/presto token/ct",
                                          "work desk", previous=("bun", "x ccstatusline"))
        self.assertEqual(snippet,
                         "python3 '/opt/presto collector/presto_collector.py'"
                         " claude-statusline --url http://server:8787"
                         " --token-file '~/.config/presto token/ct'"
                         " --host 'work desk' -- bun 'x ccstatusline'")

    def test_powershell_snippet_uses_forward_slashes_and_encoded_command(self):
        snippet = collector.powershell_snippet("C:\\presto tools\\presto_collector.py",
                                               "http://server:8787", "C:\\tok\\ct", "laptop")
        self.assertTrue(snippet.startswith("powershell -NoProfile -NonInteractive -EncodedCommand "))
        script = base64.b64decode(snippet.rsplit(" ", 1)[1]).decode("utf-16-le")
        self.assertIn("'C:/presto tools/presto_collector.py'", script)
        self.assertIn("'--token-file' 'C:/tok/ct'", script)
        self.assertNotIn("\\", script.split("& ", 1)[1])
        self.assertIn("$prestoInput | &", script)

    def test_token_value_never_appears_in_either_snippet(self):
        for snippet in (collector.posix_snippet("s.py", "u", str(self.token_file), "h"),
                        collector.powershell_snippet("s.py", "u", str(self.token_file), "h")):
            self.assertNotIn("secret-token-value", snippet)

    def _shell(self):
        if os.name != "nt":
            return "/bin/sh" if Path("/bin/sh").is_file() else None
        git = shutil.which("git")
        if git:
            for parent in Path(git).resolve().parents[:4]:
                bash = parent / "bin" / "bash.exe"
                if bash.is_file():
                    return str(bash)
        return None

    def test_posix_snippet_runs_end_to_end_when_a_shell_exists(self):
        shell = self._shell()
        if not shell:
            self.skipTest("no POSIX shell available for the snippet")
        prior = str(Path(collector.__file__).resolve()).replace("\\", "/")
        python = sys.executable.replace("\\", "/")
        snippet = collector.posix_snippet(prior, self.server.url, str(self.token_file), "laptop",
                                          previous=(python, "-c",
                                                    "import sys,json; print('kept')"),
                                          python=python)
        result = subprocess.run([shell, "-c", snippet],
                                input=json.dumps(payload()).encode("utf-8"),
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), b"kept")
        self.assertEqual(len(self.server.recorded), 1)

    @unittest.skipUnless(os.name == "nt", "PowerShell snippet runs on Windows")
    def test_powershell_snippet_runs_end_to_end(self):
        snippet = collector.powershell_snippet(str(Path(collector.__file__).resolve()),
                                               self.server.url, str(self.token_file), "laptop",
                                               previous=(sys.executable, "-c",
                                                         "import sys,json; print('kept')"))
        argv = ["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand",
                snippet.rsplit(" ", 1)[1]]
        result = subprocess.run(argv, input=json.dumps(payload()).encode("utf-8"),
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), b"kept")
        self.assertEqual(len(self.server.recorded), 1)


if __name__ == "__main__":
    unittest.main()
