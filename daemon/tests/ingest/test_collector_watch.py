"""The collector's watch mode: configuration, readers and pushing.

Everything runs against temporary folders and a throwaway daemon on loopback.
No real Claude profile, Codex login or token is read. The last class copies the
collector file alone into an empty folder and runs it there, which is what a
remote computer has.
"""

from datetime import datetime, timedelta, timezone
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.request import ProxyHandler, build_opener

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from presto_ai_dash.api import ApiServer
from presto_ai_dash.collectors import presto_collector as collector
from presto_ai_dash.config import ingest_enabled
from presto_ai_dash.management import ManagedCollector
from presto_ai_dash.sources.claude import ClaudeSource
from presto_ai_dash.sources.codex import CodexSource
from providers.test_claude_usage_cache import (SENTINEL_EMAIL, SENTINEL_ORG, SENTINEL_PROJECT,
                                     SENTINEL_TOKEN, SENTINEL_UUID, cache_document)
from providers.test_codex_reset_credits import response as codex_response
from admin.test_management import AdminServerBase

ROOT = Path(__file__).resolve().parents[3]
SENTINEL_PROMPT = "SENTINEL-PROMPT-TEXT"
SENTINEL_SESSION = "SENTINEL-SESSION-IDENTITY"
SENTINEL_CWD = "SENTINEL-C:/private/cwd"
PRIVATE = (SENTINEL_EMAIL, SENTINEL_ORG, SENTINEL_PROJECT, SENTINEL_TOKEN, SENTINEL_UUID,
           SENTINEL_PROMPT, SENTINEL_SESSION, SENTINEL_CWD, "accountUuid", "emailAddress")


def build_tool():
    spec = importlib.util.spec_from_file_location("build_collector",
                                                  ROOT / "tools" / "build_collector.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def session_line(seconds_ago=60, request_id="req-1"):
    """One assistant log line with the private parts a real one holds."""
    moment = datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)
    entry = {"type": "assistant", "timestamp": moment.isoformat().replace("+00:00", "Z"),
             "entrypoint": "claude-vscode", "sessionId": SENTINEL_SESSION, "cwd": SENTINEL_CWD,
             "requestId": request_id,
             "message": {"id": "msg-" + request_id, "model": "claude-test",
                         "content": [{"type": "text", "text": SENTINEL_PROMPT}],
                         "usage": {"input_tokens": 10, "output_tokens": 20,
                                   "cache_creation_input_tokens": 5,
                                   "cache_read_input_tokens": 7}}}
    return json.dumps(entry) + "\n"


class FolderBase(unittest.TestCase):
    """A temporary Claude profile and a temporary collector folder."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.profile = self.root / "profile" / ".claude"
        (self.profile / "projects" / "proj").mkdir(parents=True)
        self.folder = self.root / "folder"
        self.folder.mkdir()
        self.config_path = self.folder / "collector.json"

    def write_session(self, *lines):
        path = self.profile / "projects" / "proj" / "s.jsonl"
        path.write_bytes("".join(lines).encode("utf-8"))

    def write_cache(self, **changes):
        fetched = changes.pop("fetched", datetime.now(timezone.utc) - timedelta(minutes=20))
        (self.profile.parent / ".claude.json").write_text(
            json.dumps(cache_document(fetched=fetched, **changes)), encoding="utf-8")

    def write_config(self, accounts=None, **top):
        data = {"url": "http://127.0.0.1:1/api/v1/ingest", "host": "laptop",
                "accounts": accounts if accounts is not None else [
                    {"service": "claude", "token_file": "claude.token",
                     "settings_path": str(self.profile / "settings.json")}]}
        data.update(top)
        self.config_path.write_text(json.dumps(data), encoding="utf-8")
        return str(self.config_path)

    def account(self, **changes):
        settings = {"service": "claude", "name": "claude", "token_file": "t",
                    "settings_path": str(self.profile / "settings.json"),
                    "activity": True, "usage_cache": True, "usage_cache_path": ""}
        settings.update(changes)
        return collector.Account(settings)


class ConfigTests(FolderBase):
    def load(self, **kwargs):
        return collector.load_config(self.write_config(**kwargs))

    def test_a_valid_file_loads_with_its_defaults(self):
        config = self.load()
        self.assertEqual(config["host"], "laptop")
        self.assertEqual(config["interval"], collector.DEFAULT_INTERVAL_SECONDS)
        account = config["accounts"][0]
        self.assertTrue(account["activity"] and account["usage_cache"])
        # A relative token file is read from the folder the configuration is in.
        self.assertEqual(Path(account["token_file"]), self.folder / "claude.token")

    def test_the_host_defaults_to_this_computers_name_and_is_made_ascii(self):
        config = collector.load_config(self.write_config(host=""))
        self.assertTrue(config["host"])
        config = collector.load_config(self.write_config(host="caf\u00e9-laptop"))
        self.assertTrue(config["host"].isascii())

    def test_a_codex_account(self):
        config = self.load(accounts=[{"service": "codex", "token_file": "codex.token",
                                      "profile_home": "~/codex-home", "timeout": 20}])
        account = config["accounts"][0]
        self.assertEqual(account["timeout"], 20)
        self.assertTrue(os.path.isabs(account["profile_home"]))

    def test_every_mistake_is_named_and_none_is_ignored(self):
        good = {"service": "claude", "token_file": "a"}
        cases = {
            "an unknown top-level setting": dict(accounts=[good], colour="red"),
            "an unknown account setting": dict(accounts=[dict(good, usagecache=True)]),
            "a codex-only setting on claude": dict(accounts=[dict(good, executable="x")]),
            "an unknown service": dict(accounts=[{"service": "gemini", "token_file": "a"}]),
            "no accounts": dict(accounts=[]),
            "too many accounts": dict(accounts=[dict(good, token_file="t%d" % n)
                                                for n in range(collector.MAX_ACCOUNTS + 1)]),
            "no token file": dict(accounts=[{"service": "claude"}]),
            "a bad url": dict(url="ftp://server/ingest"),
            "no url": dict(url=None),
            "a short interval": dict(interval_seconds=5),
            "a long interval": dict(interval_seconds=10 ** 6),
            "a text interval": dict(interval_seconds="60"),
            "a text flag": dict(accounts=[dict(good, activity="yes")]),
            "both readers off": dict(accounts=[dict(good, activity=False, usage_cache=False)]),
            "a bad timeout": dict(accounts=[{"service": "codex", "token_file": "a",
                                             "timeout": 0}]),
            "the same token file twice": dict(accounts=[good, good]),
            "another version": dict(version=2),
        }
        for name, changes in cases.items():
            with self.subTest(name):
                path = self.write_config(**{k: v for k, v in changes.items() if v is not None})
                if changes.get("url", 1) is None:
                    data = json.loads(Path(path).read_text(encoding="utf-8"))
                    del data["url"]
                    Path(path).write_text(json.dumps(data), encoding="utf-8")
                with self.assertRaises(collector.ConfigError):
                    collector.load_config(path)

    def test_a_missing_unreadable_or_oversized_or_broken_file(self):
        with self.assertRaises(collector.ConfigError) as caught:
            collector.load_config(str(self.folder / "absent.json"))
        self.assertIn("no configuration file", str(caught.exception))
        self.config_path.write_text("{not json", encoding="utf-8")
        with self.assertRaises(collector.ConfigError):
            collector.load_config(str(self.config_path))
        self.config_path.write_text("[]", encoding="utf-8")
        with self.assertRaises(collector.ConfigError):
            collector.load_config(str(self.config_path))
        self.config_path.write_bytes(b" " * (collector.MAX_CONFIG_BYTES + 1))
        with self.assertRaises(collector.ConfigError):
            collector.load_config(str(self.config_path))

    def test_a_byte_order_mark_from_a_windows_editor_is_accepted(self):
        path = self.write_config()
        raw = Path(path).read_bytes()
        Path(path).write_bytes(b"\xef\xbb\xbf" + raw)
        self.assertEqual(collector.load_config(path)["host"], "laptop")


class EmbeddedReaderTests(FolderBase):
    def test_the_embedded_block_is_what_the_build_tool_makes(self):
        tool = build_tool()
        current = Path(collector.__file__).read_bytes().decode("utf-8").replace("\r\n", "\n")
        self.assertEqual(tool.rebuilt(current), current,
                         "run: python tools/build_collector.py")

    def test_each_embedded_module_is_its_source_word_for_word(self):
        tool = build_tool()
        for name, path in tool.SECTIONS:
            with self.subTest(name=name):
                self.assertEqual(collector._EMBEDDED[name], tool.source_text(path))

    def test_the_collector_imports_nothing_from_the_package(self):
        text = Path(collector.__file__).read_text(encoding="utf-8")
        outside = text[:text.index(collector_marker("BEGIN"))] + text[
            text.index(collector_marker("END")):]
        self.assertNotIn("presto_ai_dash", outside.replace("presto-ai-dash", ""))

    def test_the_embedded_readers_load_and_work(self):
        self.write_session(session_line())
        reader = collector._embedded("claude_activity").ActivityReader(
            self.profile / "projects")
        summary = reader.refresh()
        self.assertTrue(summary["available"])
        self.assertEqual(summary["requests_5h"], 1)
        rpc = collector._embedded("codex_rpc")
        self.assertTrue(issubclass(rpc.CodexReportingError, Exception))
        self.assertIn("PATH", rpc.child_environment() or {"PATH": 1})
        windows = collector._embedded("codex_report").rate_limit_windows(codex_response())[0]
        self.assertEqual(windows[0]["label"], "Weekly")


def collector_marker(which):
    tool = build_tool()
    return tool.BEGIN if which == "BEGIN" else tool.END


class ReadingTests(FolderBase):
    def test_activity_and_the_usage_cache_are_read_and_nothing_private_is_sent(self):
        self.write_session(session_line())
        self.write_cache()
        envelope, notes, warning = self.account().envelope("laptop", __import__("time").time())
        self.assertIsNone(warning)
        self.assertEqual(envelope["collector"]["name"], "claude-watch")
        self.assertEqual(envelope["activity"]["requests_5h"], 1)
        self.assertTrue(envelope["usage_cache"]["available"])
        self.assertEqual([w["id"] for w in envelope["usage_cache"]["windows"]],
                         ["five_hour", "seven_day"])
        text = json.dumps(envelope)
        for needle in PRIVATE:
            self.assertNotIn(needle, text)
        self.assertNotIn(str(self.root), text)  # no path of this machine leaves it
        self.assertEqual(len(notes), 2)

    def test_the_envelope_passes_the_daemons_own_validator(self):
        from presto_ai_dash.ingest import validate
        self.write_session(session_line())
        self.write_cache()
        envelope = self.account().envelope("laptop", __import__("time").time())[0]
        validate(envelope, ClaudeSource)

    def test_a_missing_usage_cache_is_sent_as_unavailable_with_a_reason_and_no_path(self):
        self.write_session(session_line())
        envelope = self.account().envelope("laptop", __import__("time").time())[0]
        reading = envelope["usage_cache"]
        self.assertFalse(reading["available"])
        self.assertTrue(reading["reason"])
        self.assertNotIn(str(self.root), json.dumps(envelope))

    def test_a_missing_session_folder_is_sent_as_unavailable(self):
        envelope = self.account(activity=True, usage_cache=False,
                                settings_path=str(self.root / "nowhere" / "settings.json")
                                ).envelope("laptop", __import__("time").time())[0]
        self.assertFalse(envelope["activity"]["available"])
        self.assertNotIn("usage_cache", envelope)

    def test_a_reading_the_daemon_would_refuse_for_age_is_sent_as_unavailable(self):
        self.write_cache(fetched=datetime.now(timezone.utc) - timedelta(days=8))
        envelope = self.account(activity=False).envelope("laptop", __import__("time").time())[0]
        self.assertFalse(envelope["usage_cache"]["available"])
        self.assertIn("7 days", envelope["usage_cache"]["reason"])
        self.assertIsNone(envelope["usage_cache"]["fetched_at"])

    def test_a_reader_that_is_switched_off_is_not_run(self):
        self.write_session(session_line())
        envelope = self.account(usage_cache=False).envelope("laptop", __import__("time").time())[0]
        self.assertIn("activity", envelope)
        self.assertNotIn("usage_cache", envelope)

    def test_a_codex_reading_becomes_windows_with_fresh_evidence(self):
        rpc = collector._embedded("codex_rpc")
        account = collector.Account({"service": "codex", "name": "codex", "token_file": "t",
                                     "executable": "", "profile_home": "", "timeout": 5})
        with patch.object(rpc, "find_executable", return_value="codex"), \
                patch.object(rpc, "read_subscription", return_value=codex_response()):
            first, notes, warning = account.envelope("laptop", 0)
            second, _, _ = account.envelope("laptop", 0)
        self.assertIsNone(warning)
        self.assertEqual(first["collector"]["name"], "codex-watch")
        self.assertEqual(first["windows"][0]["used_percent"], 14)
        self.assertEqual(set(first["evidence"]), {w["id"] for w in first["windows"]})
        self.assertIn("reset_credits", first)
        self.assertIn("rate limits ok", notes[0])
        from presto_ai_dash.ingest import validate
        validate(first, CodexSource)
        # Each reading is a new observation: its evidence differs, so its age restarts.
        self.assertNotEqual(first["evidence"], second["evidence"])

    def test_codex_not_readable_sends_a_heartbeat_and_a_warning(self):
        rpc = collector._embedded("codex_rpc")
        account = collector.Account({"service": "codex", "name": "codex", "token_file": "t",
                                     "executable": "", "profile_home": "", "timeout": 5})
        with patch.object(rpc, "find_executable",
                          side_effect=rpc.CodexReportingError("Install Codex")):
            envelope, notes, warning = account.envelope("laptop", 0)
        self.assertNotIn("windows", envelope)
        self.assertIn("Install Codex", warning)

    def test_codex_answering_in_an_unknown_shape_is_a_warning_not_a_crash(self):
        rpc = collector._embedded("codex_rpc")
        account = collector.Account({"service": "codex", "name": "codex", "token_file": "t",
                                     "executable": "", "profile_home": "", "timeout": 5})
        with patch.object(rpc, "find_executable", return_value="codex"), \
                patch.object(rpc, "read_subscription", return_value={"rateLimits": 5}):
            envelope, _, warning = account.envelope("laptop", 0)
        self.assertNotIn("windows", envelope)
        self.assertIn("does not understand", warning)


class PushBase(AdminServerBase, FolderBase):
    """A throwaway daemon with a Claude and a Codex account, and a way to push to it."""

    def setUp(self):
        AdminServerBase.setUp(self)
        self.profile = Path(self.temp.name) / "profile" / ".claude"
        (self.profile / "projects" / "proj").mkdir(parents=True)
        self.folder = Path(self.temp.name) / "folder"
        self.folder.mkdir()
        self.config_path = self.folder / "collector.json"
        self.claude = self.manager.save({"type": "claude", "name": "Claude", "enabled": True,
                                         "config": {"settings_path": str(self.profile / "settings.json")}})
        self.codex = self.manager.save({"type": "codex", "name": "Codex", "enabled": True,
                                        "config": {}})
        self.api = ApiServer(
            "127.0.0.1", 0, self.storage, "device-token",
            status_provider=self.manager.status_payload,
            ingest_enabled=lambda: ingest_enabled(self.config, self.storage.ingest_enabled_setting()))
        self.api.start()
        self.addCleanup(self.api.stop)
        self.url = "http://127.0.0.1:%d/api/v1/ingest" % self.api.actual_port
        self.opener = build_opener(ProxyHandler({}), collector._NoRedirect())
        self.claude_token = self.storage.create_ingest_token(self.claude["id"])
        self.codex_token = self.storage.create_ingest_token(self.codex["id"])
        (self.folder / "claude.token").write_text(self.claude_token, encoding="utf-8")
        (self.folder / "codex.token").write_text(self.codex_token, encoding="utf-8")
        self.set_ingest(True)

    def set_ingest(self, on):
        self.assertEqual(self.request("/api/admin/ingest", "PUT", {"enabled": on})[0], 200)

    def config_for(self, *services, **top):
        entries = []
        if "claude" in services:
            entries.append({"service": "claude", "token_file": "claude.token",
                            "settings_path": str(self.profile / "settings.json")})
        if "codex" in services:
            entries.append({"service": "codex", "token_file": "codex.token"})
        top.setdefault("url", self.url)
        return self.write_config(accounts=entries, **top)

    def run_watch(self, path, **kwargs):
        out = io.StringIO()
        code = collector.watch(path, once=True, out=out, opener=self.opener, **kwargs)
        return code, out.getvalue()

    def claude_source(self):
        source = ClaudeSource({"settings_path": str(self.profile / "settings.json")})
        source.id = self.claude["id"]
        source.attach_storage(self.storage)
        return source

    def codex_source(self):
        source = CodexSource({})
        source.id = self.codex["id"]
        source.attach_storage(self.storage)
        return source


class EndToEndTests(PushBase):
    def test_claude_activity_and_usage_cache_reach_the_daemon(self):
        self.write_session(session_line())
        self.write_cache()
        code, output = self.run_watch(self.config_for("claude"))
        self.assertEqual(code, 0, output)
        self.assertIn("sent:", output)
        quota = self.claude_source().collect().metrics["quota"]
        self.assertTrue(quota["local_activity"]["available"])
        self.assertEqual(quota["local_activity"]["requests_5h"], 1)
        five = next(w for w in quota["windows"] if w["id"] == "five_hour")
        self.assertEqual(five["source"], "usage cache")
        stored = json.dumps(self.storage.observation(self.claude["id"]))
        for needle in PRIVATE:
            self.assertNotIn(needle, stored)

    def test_a_polling_daemon_shows_the_pushed_readings_even_after_an_earlier_empty_poll(self):
        # The poll before the push had nothing, and its snapshot carries the poll's own
        # time; the reading that arrives next is older than that poll. It must win.
        runtime = ManagedCollector(self.manager, self.config, threading.Event())
        runtime.collect_once()
        self.write_session(session_line())
        self.write_cache()
        code, output = self.run_watch(self.config_for("claude"))
        self.assertEqual(code, 0, output)
        runtime.collect_once()
        row = next(item for item in self.manager.list_sources()["sources"]
                   if item["id"] == self.claude["id"])
        quota = row["quota"]
        self.assertTrue(quota["local_activity"]["available"])
        five = next(window for window in quota["windows"] if window["id"] == "five_hour")
        self.assertEqual(five["used_percent"], 12.0)

    def test_codex_reaches_the_daemon_and_is_used_there_without_a_local_codex(self):
        rpc = collector._embedded("codex_rpc")
        with patch.object(rpc, "find_executable", return_value="codex"), \
                patch.object(rpc, "read_subscription", return_value=codex_response()):
            code, output = self.run_watch(self.config_for("codex"))
        self.assertEqual(code, 0, output)
        from presto_ai_dash.sources._codex_rpc import CodexReportingError
        with patch("presto_ai_dash.sources.codex.find_executable",
                   side_effect=CodexReportingError("no codex here")):
            snapshot = self.codex_source().collect()
        self.assertEqual(snapshot.status, "ok")
        self.assertEqual(snapshot.metrics["quota"]["windows"][0]["used_percent"], 14)

    def test_both_accounts_in_one_file(self):
        self.write_session(session_line())
        rpc = collector._embedded("codex_rpc")
        with patch.object(rpc, "find_executable", return_value="codex"), \
                patch.object(rpc, "read_subscription", return_value=codex_response()):
            code, output = self.run_watch(self.config_for("claude", "codex"))
        self.assertEqual(code, 0, output)
        self.assertEqual(output.count("sent:"), 2)

    def test_the_token_is_never_printed(self):
        self.write_session(session_line())
        _, output = self.run_watch(self.config_for("claude"))
        self.assertNotIn(self.claude_token, output)
        self.set_ingest(False)
        _, output = self.run_watch(self.config_for("claude"))
        self.assertNotIn(self.claude_token, output)


class WhenTheDaemonSaysNoTests(PushBase):
    """What the person sees when the collector is set up and the daemon is not."""

    def setUp(self):
        super().setUp()
        self.write_session(session_line())

    def test_remote_reports_off_on_the_daemon_is_explained_in_plain_words(self):
        self.set_ingest(False)
        code, output = self.run_watch(self.config_for("claude"))
        self.assertEqual(code, 1)
        self.assertIn("404", output)
        self.assertIn("switched off", output)
        self.assertIn("Remote reports", output)
        # And nothing was stored.
        self.assertIsNone(self.storage.observation(self.claude["id"]))

    def test_switching_it_on_makes_the_next_round_work(self):
        self.set_ingest(False)
        path = self.config_for("claude")
        config = collector.load_config(path)
        accounts = [collector.Account(s) for s in config["accounts"]]
        out = io.StringIO()
        sent = collector.run_round(config, accounts, now=1000.0, out=out, opener=self.opener)
        self.assertEqual(sent, 0)
        self.set_ingest(True)
        # Still backing off: a round inside the wait does nothing.
        self.assertEqual(collector.run_round(config, accounts, now=1001.0, out=out,
                                             opener=self.opener), 0)
        sent = collector.run_round(config, accounts, now=1000.0 + collector.MAX_BACKOFF_SECONDS + 1,
                                   out=out, opener=self.opener)
        self.assertEqual(sent, 1)
        self.assertIn("reporting again", out.getvalue())

    def test_the_same_complaint_is_made_once_not_every_round(self):
        self.set_ingest(False)
        config = collector.load_config(self.config_for("claude"))
        accounts = [collector.Account(s) for s in config["accounts"]]
        out = io.StringIO()
        for round_number in range(4):
            collector.run_round(config, accounts, now=1000.0 + round_number * 400,
                                out=out, opener=self.opener)
        self.assertEqual(out.getvalue().count("switched off"), 1)

    def test_a_revoked_token(self):
        self.storage.revoke_ingest_token(self.claude["id"])
        code, output = self.run_watch(self.config_for("claude"))
        self.assertEqual(code, 1)
        self.assertIn("401", output)
        self.assertIn("new remote token", output)

    def test_a_missing_token_file(self):
        (self.folder / "claude.token").unlink()
        code, output = self.run_watch(self.config_for("claude"))
        self.assertEqual(code, 1)
        self.assertIn("token file", output)

    def test_a_daemon_that_is_not_there(self):
        path = self.config_for("claude", url="http://127.0.0.1:9/api/v1/ingest")
        code, output = self.run_watch(path)
        self.assertEqual(code, 1)
        self.assertIn("cannot be reached", output)

    def test_a_redirect_is_not_followed(self):
        class Redirect(threading.Thread):
            pass
        from http.server import BaseHTTPRequestHandler, HTTPServer

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                self.send_response(302)
                self.send_header("Location", "http://127.0.0.1:1/elsewhere")
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *args):
                pass

        server = HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        path = self.config_for("claude", url="http://127.0.0.1:%d/x" % server.server_port)
        code, output = self.run_watch(path)
        self.assertEqual(code, 1)
        self.assertIn("redirect", output)

    def test_one_bad_account_does_not_stop_the_other(self):
        rpc = collector._embedded("codex_rpc")
        (self.folder / "codex.token").unlink()
        with patch.object(rpc, "find_executable", return_value="codex"), \
                patch.object(rpc, "read_subscription", return_value=codex_response()):
            code, output = self.run_watch(self.config_for("claude", "codex"))
        self.assertEqual(code, 1)
        self.assertIn("sent:", output)  # Claude went through
        self.assertTrue(self.storage.observation(self.claude["id"]))

    def test_a_reader_that_breaks_is_reported_and_the_watch_goes_on(self):
        config = collector.load_config(self.config_for("claude"))
        accounts = [collector.Account(s) for s in config["accounts"]]
        out = io.StringIO()
        with patch.object(collector.Account, "envelope", side_effect=RuntimeError("secret")):
            sent = collector.run_round(config, accounts, now=1000.0, out=out,
                                       opener=self.opener)
        self.assertEqual(sent, 0)
        self.assertIn("RuntimeError", out.getvalue())
        self.assertNotIn("secret", out.getvalue())

    def test_a_bad_configuration_exits_with_a_message_and_code_2(self):
        out = io.StringIO()
        self.assertEqual(collector.watch(str(self.folder / "absent.json"), once=True, out=out), 2)
        self.assertIn("no configuration file", out.getvalue())

    def test_watching_runs_rounds_until_told_to_stop(self):
        out, naps = io.StringIO(), []
        code = collector.watch(self.config_for("claude"), out=out, opener=self.opener,
                               sleep=naps.append, rounds=3)
        self.assertEqual(code, 0)
        self.assertEqual(naps, [collector.DEFAULT_INTERVAL_SECONDS] * 2)

    def test_ctrl_c_ends_the_watch_quietly(self):
        def interrupt(_seconds):
            raise KeyboardInterrupt
        out = io.StringIO()
        self.assertEqual(collector.watch(self.config_for("claude"), out=out, opener=self.opener,
                                         sleep=interrupt), 0)

    def test_the_command_line(self):
        with patch("sys.stderr", io.StringIO()):
            code = collector.main(["watch", "--config", str(self.folder / "absent.json"), "--once"])
        self.assertEqual(code, 2)


class DocumentedSetupTests(PushBase):
    """What the admin page tells a person to save must be what works."""

    def follow_the_page(self, row):
        from presto_ai_dash.management import INGEST_URL_PLACEHOLDER
        reveal = self.manager.create_ingest_token(row["id"])
        (self.folder / reveal["token_file"]).write_text(reveal["token"], encoding="utf-8")
        text = reveal["collector_config"].replace(INGEST_URL_PLACEHOLDER, self.url)
        self.config_path.write_text(text, encoding="utf-8")
        return reveal

    def test_a_claude_account_gets_the_status_line_and_the_watch_setup(self):
        reveal = self.follow_the_page(self.claude)
        self.assertEqual(reveal["service"], "claude")
        self.assertIn("posix_snippet", reveal)
        self.assertEqual(json.loads(reveal["collector_config"])["accounts"][0]["service"], "claude")
        self.assertTrue(reveal["token_file"].startswith("claude-"))
        # The profile in the file is the default one; point it at the test profile.
        data = json.loads(self.config_path.read_text(encoding="utf-8"))
        data["accounts"][0]["settings_path"] = str(self.profile / "settings.json")
        self.config_path.write_text(json.dumps(data), encoding="utf-8")
        self.write_session(session_line())
        code, output = self.run_watch(str(self.config_path))
        self.assertEqual(code, 0, output)
        self.assertTrue(self.claude_source().collect().metrics["quota"]["local_activity"]["available"])

    def test_a_codex_account_gets_only_the_watch_setup(self):
        reveal = self.follow_the_page(self.codex)
        self.assertEqual(reveal["service"], "codex")
        self.assertNotIn("posix_snippet", reveal)
        self.assertNotIn("powershell_snippet", reveal)
        rpc = collector._embedded("codex_rpc")
        with patch.object(rpc, "find_executable", return_value="codex"), \
                patch.object(rpc, "read_subscription", return_value=codex_response()):
            code, output = self.run_watch(str(self.config_path))
        self.assertEqual(code, 0, output)

    def test_the_token_is_never_inside_the_configuration_text(self):
        reveal = self.follow_the_page(self.claude)
        self.assertNotIn(reveal["token"], reveal["collector_config"])


class AloneOnAnotherComputerTests(PushBase):
    """The file by itself, in an empty folder, with the package out of reach."""

    def test_the_collector_file_alone_reports_to_the_daemon(self):
        self.write_session(session_line())
        self.write_cache()
        lonely = Path(self.temp.name) / "remote"
        lonely.mkdir()
        copy = lonely / "presto_collector.py"
        copy.write_bytes(Path(collector.__file__).read_bytes())
        path = self.config_for("claude")
        environment = {key: value for key, value in os.environ.items()
                       if key.upper() not in ("PYTHONPATH", "HTTP_PROXY", "HTTPS_PROXY",
                                              "ALL_PROXY")}
        environment["NO_PROXY"] = "127.0.0.1"
        result = subprocess.run(
            [sys.executable, "-I", str(copy), "watch", "--config", path, "--once"],
            cwd=str(lonely), env=environment, capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("sent:", result.stderr)
        quota = self.claude_source().collect().metrics["quota"]
        self.assertTrue(quota["local_activity"]["available"])
        for needle in PRIVATE:
            self.assertNotIn(needle, result.stdout + result.stderr)

    def test_the_status_line_mode_still_works_without_the_readers_loading(self):
        # The status line must stay light and safe: it never loads the embedded readers.
        loaded_before = set(collector._EMBEDDED_LOADED)
        with patch("sys.stdin", io.TextIOWrapper(io.BytesIO(b"{}"))), \
                patch("sys.stdout", io.StringIO()):
            collector.main(["claude-statusline", "--url", "http://127.0.0.1:9/x",
                            "--token-file", str(self.folder / "absent")],
                           state_path=str(self.folder / "state.json"))
        self.assertEqual(set(collector._EMBEDDED_LOADED), loaded_before)


if __name__ == "__main__":
    unittest.main()
