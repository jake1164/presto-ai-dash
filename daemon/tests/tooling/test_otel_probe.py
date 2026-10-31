"""The OpenTelemetry probe shows what a Claude Code surface emits - and nothing private.

Claude Code puts the account's email, its ids and (when opted in) prompts and
tool inputs on its telemetry. The probe is a diagnostic: it must show metric and
event names, attribute NAMES and a short allowlist of harmless values, and it
must never print or store anything else. Synthetic payloads only.
"""

import gzip
import importlib.util
import json
import os
import sys
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

TOOLS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "..", "..", "tools")


def load_probe():
    spec = importlib.util.spec_from_file_location("otel_probe", os.path.join(TOOLS, "otel_probe.py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules["otel_probe"] = module
    spec.loader.exec_module(module)
    return module


probe = load_probe()

EMAIL = "someone.private@example.com"
ACCOUNT = "9f1c2a7e-aaaa-bbbb-cccc-1234567890ab"
ORG = "org-0000-1111-2222-private"
PROMPT = "please refactor my secret payroll script"
PATH = "C:\\Users\\someone\\secret-project"
TOOL_INPUT = "rm -rf /home/someone/private"
SECRETS = (EMAIL, ACCOUNT, ORG, PROMPT, PATH, TOOL_INPUT, "payroll", "private")


def attr(key, value):
    if isinstance(value, bool):
        return {"key": key, "value": {"boolValue": value}}
    if isinstance(value, int):
        return {"key": key, "value": {"intValue": str(value)}}
    if isinstance(value, float):
        return {"key": key, "value": {"doubleValue": value}}
    return {"key": key, "value": {"stringValue": value}}


def resource():
    return {"attributes": [attr("service.name", "claude-code"),
                           attr("user.email", EMAIL), attr("user.account_uuid", ACCOUNT),
                           attr("organization.id", ORG), attr("workspace.host_paths", PATH)]}


def metrics_payload():
    standard = [attr("session.id", "abc"), attr("user.email", EMAIL),
                attr("user.id", "anon-hash"), attr("organization.id", ORG),
                attr("app.entrypoint", "claude-vscode"), attr("terminal.type", "vscode")]
    return {"resourceMetrics": [{"resource": resource(), "scopeMetrics": [{"metrics": [
        {"name": "claude_code.cost.usage", "unit": "USD", "sum": {"dataPoints": [
            {"attributes": standard + [attr("model", "claude-sonnet-x"),
                                       attr("query_source", "main")], "asDouble": 0.0123},
            {"attributes": standard + [attr("model", "claude-sonnet-x"),
                                       attr("query_source", "subagent")], "asDouble": 0.0077}]}},
        {"name": "claude_code.token.usage", "unit": "tokens", "sum": {"dataPoints": [
            {"attributes": standard + [attr("type", "input"), attr("model", "m")],
             "asInt": "1200"},
            {"attributes": standard + [attr("type", "output"), attr("model", "m")],
             "asInt": "300"}]}},
    ]}]}]}


def logs_payload():
    standard = [attr("session.id", "abc"), attr("user.email", EMAIL),
                attr("user.account_uuid", ACCOUNT), attr("organization.id", ORG)]
    return {"resourceLogs": [{"resource": resource(), "scopeLogs": [{"logRecords": [
        {"body": {"stringValue": "claude_code.user_prompt"},
         "attributes": standard + [attr("event.name", "user_prompt"),
                                   attr("prompt", PROMPT), attr("prompt_length", 39)]},
        {"body": {"stringValue": "claude_code.tool_result"},
         "attributes": standard + [attr("event.name", "tool_result"), attr("tool_name", "Bash"),
                                   attr("tool_input", TOOL_INPUT), attr("success", True)]},
        {"body": {"stringValue": "claude_code.api_request"},
         "attributes": standard + [attr("event.name", "api_request"), attr("model", "claude-x"),
                                   attr("cost_usd", 0.01), attr("duration_ms", 1800),
                                   attr("status_code", 200)]},
    ]}]}]}


def run_summary(kind, payload):
    return "\n".join(probe.summarize(kind, payload))


class SummaryNeverShowsPrivateValues(unittest.TestCase):
    def test_metrics_summary_names_the_metrics_and_totals(self):
        text = run_summary("metrics", metrics_payload())
        self.assertIn("claude_code.cost.usage", text)
        self.assertIn("claude_code.token.usage", text)
        self.assertIn("0.02", text)           # 0.0123 + 0.0077, a harmless total
        self.assertIn("claude-vscode", text)  # the surface: an allowlisted value
        self.assertIn("claude-sonnet-x", text)

    def test_logs_summary_names_the_events(self):
        text = run_summary("logs", logs_payload())
        for name in ("user_prompt", "tool_result", "api_request"):
            self.assertIn(name, text)
        self.assertIn("duration_ms", text)

    def test_no_private_value_appears_anywhere(self):
        for kind, payload in (("metrics", metrics_payload()), ("logs", logs_payload())):
            text = run_summary(kind, payload)
            for secret in SECRETS:
                self.assertNotIn(secret, text, "%s leaked in %s" % (secret, kind))

    def test_sensitive_attributes_are_reported_as_present_but_not_shown(self):
        text = run_summary("logs", logs_payload())
        self.assertIn("user.email", text)
        self.assertIn("PRESENT", text)
        self.assertIn("prompt", text)

    def test_unknown_attribute_values_are_hidden_but_their_names_are_listed(self):
        payload = metrics_payload()
        payload["resourceMetrics"][0]["scopeMetrics"][0]["metrics"][0]["sum"]["dataPoints"][0][
            "attributes"].append(attr("custom.attr", "super-secret-value"))
        text = run_summary("metrics", payload)
        self.assertIn("custom.attr", text)
        self.assertNotIn("super-secret-value", text)

    def test_garbage_shapes_never_raise(self):
        for payload in ({}, [], "x", {"resourceMetrics": "no"}, {"resourceLogs": [None, 1]},
                        {"resourceMetrics": [{"scopeMetrics": [{"metrics": [{"name": 5}]}]}]}):
            for kind in ("metrics", "logs", "traces"):
                probe.summarize(kind, payload)


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.lines = []
        self.server = probe.make_server(0, self.lines.append)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, args=(0.02,), daemon=True)
        self.thread.start()
        self.addCleanup(self.stop)

    def stop(self):
        self.server.shutdown()
        self.server.server_close()

    def post(self, path, body, headers=None, raw=False):
        data = body if raw else json.dumps(body).encode("utf-8")
        request = Request("http://127.0.0.1:%d%s" % (self.port, path), data=data, method="POST",
                          headers={"Content-Type": "application/json", **(headers or {})})
        try:
            with urlopen(request, timeout=5) as response:
                return response.status, response.read()
        except HTTPError as error:
            return error.code, error.read()

    def test_it_listens_on_loopback_only(self):
        self.assertEqual(self.server.server_address[0], "127.0.0.1")

    def test_a_metrics_post_is_answered_and_summarised(self):
        status, body = self.post("/v1/metrics", metrics_payload())
        self.assertEqual((status, json.loads(body)), (200, {}))
        text = "\n".join(self.lines)
        self.assertIn("claude_code.cost.usage", text)
        for secret in SECRETS:
            self.assertNotIn(secret, text)

    def test_a_gzipped_body_is_understood(self):
        body = gzip.compress(json.dumps(logs_payload()).encode("utf-8"))
        status, _ = self.post("/v1/logs", body, {"Content-Encoding": "gzip"}, raw=True)
        self.assertEqual(status, 200)
        self.assertIn("api_request", "\n".join(self.lines))

    def test_a_gzip_bomb_is_refused_and_the_server_carries_on(self):
        # A few kilobytes that expand past the limit.
        bomb = gzip.compress(b" " * (probe.MAX_EXPANDED + 1024))
        self.assertLess(len(bomb), probe.MAX_BODY)
        status, _ = self.post("/v1/metrics", bomb, {"Content-Encoding": "gzip"}, raw=True)
        self.assertEqual(status, 400)
        status, _ = self.post("/v1/metrics", metrics_payload())
        self.assertEqual(status, 200)

    def test_a_body_just_inside_the_limit_is_expanded(self):
        data = probe.inflate(gzip.compress(b"x" * 1000), limit=1000)
        self.assertEqual(len(data), 1000)
        with self.assertRaises(ValueError):
            probe.inflate(gzip.compress(b"x" * 1001), limit=1000)

    def test_a_truncated_gzip_body_is_a_400(self):
        body = gzip.compress(json.dumps(logs_payload()).encode("utf-8"))[:-6]
        status, _ = self.post("/v1/logs", body, {"Content-Encoding": "gzip"}, raw=True)
        self.assertEqual(status, 400)

    def test_control_characters_in_names_never_reach_the_terminal(self):
        escape = chr(27)
        payload = {"resourceMetrics": [{"scopeMetrics": [{"metrics": [
            {"name": "evil" + escape + "[2J" + chr(7) + "name",
             "sum": {"dataPoints": [{"asInt": "1", "attributes": [
                 {"key": "key" + escape + "[31m", "value": {"stringValue": "v"}}]}]}}]}]}]}
        status, _ = self.post("/v1/metrics", payload)
        self.assertEqual(status, 200)
        text = "".join(self.lines)
        self.assertIn("evil", text)
        for character in text:
            self.assertTrue(" " <= character <= "~", repr(character))

    def test_one_line_is_never_longer_than_the_limit(self):
        self.server.out("x" * 5000)
        self.assertEqual(len(self.lines[-1]), probe.MAX_LINE)

    def test_protobuf_is_counted_not_decoded_and_the_hint_names_the_fix(self):
        status, _ = self.post("/v1/metrics", b"\x0a\x00", {"Content-Type": "application/x-protobuf"},
                              raw=True)
        self.assertEqual(status, 200)
        text = "\n".join(self.lines)
        self.assertIn("protobuf", text)
        self.assertIn("http/json", text)

    def test_bad_json_is_a_400_and_does_not_stop_the_server(self):
        status, _ = self.post("/v1/metrics", b"{not json", raw=True)
        self.assertEqual(status, 400)
        status, _ = self.post("/v1/metrics", metrics_payload())
        self.assertEqual(status, 200)

    def test_an_oversized_body_is_refused_before_it_is_read(self):
        import socket
        crlf = chr(13) + chr(10)
        with socket.create_connection(("127.0.0.1", self.port), timeout=5) as sock:
            request = ("POST /v1/metrics HTTP/1.1" + crlf + "Host: x" + crlf +
                       "Content-Type: application/json" + crlf +
                       "Content-Length: %d" % (probe.MAX_BODY + 1) + crlf + crlf)
            sock.sendall(request.encode("ascii"))
            status_line = sock.recv(64).split(crlf.encode("ascii"))[0]
        self.assertIn(b"413", status_line)

    def test_unknown_paths_are_404(self):
        status, _ = self.post("/nope", {})
        self.assertEqual(status, 404)

    def test_the_probe_keeps_no_values_after_the_request(self):
        self.post("/v1/logs", logs_payload())
        for secret in SECRETS:
            self.assertNotIn(secret, repr(self.server.__dict__))


class UnrecognisedContactTests(ServerTests):
    """Any contact must be visible, or silence would look like 'nothing arrived'."""

    def shown(self):
        return chr(10).join(self.lines)

    def get(self, path):
        try:
            with urlopen("http://127.0.0.1:%d%s" % (self.port, path), timeout=5) as response:
                return response.status
        except HTTPError as error:
            return error.code

    def test_a_post_to_an_unknown_path_is_shown_and_counted(self):
        status, _ = self.post("/some/other/path?token=SECRETQUERY", {})
        self.assertEqual(status, 404)
        text = self.shown()
        self.assertIn("contact", text)
        self.assertIn("POST", text)
        self.assertIn("/some/other/path", text)
        self.assertNotIn("SECRETQUERY", text)   # a query string is never echoed
        self.assertEqual(self.server.counts.get("unrecognised"), 1)

    def test_a_get_is_shown_and_counted_not_silently_dropped(self):
        self.get("/")
        text = self.shown()
        self.assertIn("contact", text)
        self.assertIn("GET", text)
        self.assertEqual(self.server.counts.get("unrecognised"), 1)

    def test_a_good_post_is_not_reported_as_an_unrecognised_contact(self):
        self.post("/v1/metrics", metrics_payload())
        self.assertNotIn("unrecognised", self.server.counts)

    def test_the_summary_line_mentions_unrecognised_contacts(self):
        self.get("/")
        self.assertIn("unrecognised", probe.final_summary(self.server.counts))
        self.assertIn("Nothing arrived", probe.final_summary({}))


class PortFallbackTests(unittest.TestCase):
    """Windows reserves some ports (Hyper-V, Docker): the probe must not just crash."""

    def test_a_port_that_cannot_be_bound_falls_back_to_one_that_can(self):
        import socket
        blocker = socket.socket()
        blocker.bind(("127.0.0.1", 0))
        blocker.listen(1)
        busy = blocker.getsockname()[1]
        self.addCleanup(blocker.close)
        server, note = probe.listen(busy, lambda line: None)
        self.addCleanup(server.server_close)
        self.assertNotEqual(server.server_address[1], busy)
        self.assertEqual(server.server_address[0], "127.0.0.1")
        self.assertIn(str(busy), note)
        self.assertIn(str(server.server_address[1]), note)

    def test_a_free_port_is_used_as_asked_with_no_note(self):
        import socket
        probe_socket = socket.socket()
        probe_socket.bind(("127.0.0.1", 0))
        free = probe_socket.getsockname()[1]
        probe_socket.close()
        server, note = probe.listen(free, lambda line: None)
        self.addCleanup(server.server_close)
        self.assertEqual(server.server_address[1], free)
        self.assertEqual(note, "")

if __name__ == "__main__":
    unittest.main()
