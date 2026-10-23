"""The ports and endpoints reference says what the daemon does.

The page was wrong once: it called the admin port loopback-only after it had
gained an allowed-hosts list, and it left routes out. This test reads the
routes out of the code, probes every route the page documents against a real
test server, and checks the limits the page states against the constants.
"""

import json
import re
import sys
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from presto_ai_dash import admin as admin_module
from presto_ai_dash import api as api_module
from presto_ai_dash.api import BoundedRequestHandler, _ExclusiveHTTPServer
from admin.test_management import AdminServerBase

ROOT = Path(__file__).resolve().parents[3]
DOC = (ROOT / "docs" / "user" / "reference" / "ports-and-endpoints.md").read_text(encoding="utf-8")
ADMIN_SOURCE = (ROOT / "daemon" / "src" / "presto_ai_dash" / "admin.py").read_text(encoding="utf-8")
ID = r"([A-Za-z0-9_-]{1,80})"
TOKEN = "unit-secret-admin-token"
METHODS = ("GET", "POST", "PUT", "DELETE")


def routes_in_the_code():
    """Every admin path the handler mentions, with ids written {id}."""
    found = set(re.findall(r"""["'](/api/admin/[^"']*)["']""", ADMIN_SOURCE))
    found |= set(admin_module.ASSETS)
    return {path.replace(ID, "{id}") for path in found}


def routes_in_the_docs():
    """The (method, path) pairs of the admin table."""
    rows = re.findall(r"^\| (GET|POST|PUT|DELETE) \| `(/[^`]*)` \|", DOC, re.M)
    return {(method, path) for method, path in rows}


class AdminRoutesMatchTheDocsTests(unittest.TestCase):
    def test_every_route_in_the_code_is_in_the_table(self):
        documented = {path for _, path in routes_in_the_docs()}
        missing = routes_in_the_code() - documented
        self.assertEqual(missing, set(), "routes the page does not mention")

    def test_every_route_in_the_table_is_in_the_code(self):
        documented = {path for _, path in routes_in_the_docs()}
        self.assertEqual(documented - routes_in_the_code(), set(), "routes the code does not have")

    def test_the_table_is_not_empty_or_a_misparse(self):
        self.assertGreaterEqual(len(routes_in_the_docs()), 25)
        self.assertGreaterEqual(len(routes_in_the_code()), 20)


class AdminRoutesAnswerTests(AdminServerBase):
    """Each documented method is routed; each undocumented one is not."""

    def probe(self, method, path):
        request = Request(self.base_url + path.replace("{id}", "abc123"),
                          data=b"{}" if method in ("POST", "PUT", "DELETE") else None,
                          headers={"Authorization": "Bearer " + TOKEN,
                                   "Content-Type": "application/json"}, method=method)
        try:
            with self.opener.open(request, timeout=5) as response:
                return response.status, response.read()
        except HTTPError as exc:
            return exc.code, exc.read()

    def test_every_documented_method_reaches_a_handler(self):
        for method, path in sorted(routes_in_the_docs()):
            with self.subTest(method=method, path=path):
                status, body = self.probe(method, path)
                self.assertNotEqual(status, 501, "the method is not implemented")
                text = body.decode("utf-8", "replace")
                # {"error": "not found"} is the route-level answer for a path
                # and method the handler does not know.
                self.assertNotEqual(json.loads(text).get("error") if text.startswith("{") else None,
                                    "not found", "%s %s is not routed" % (method, path))

    def test_a_method_the_table_does_not_list_is_not_routed(self):
        documented = routes_in_the_docs()
        for path in sorted({path for _, path in documented}):
            for method in METHODS:
                if (method, path) in documented:
                    continue
                with self.subTest(method=method, path=path):
                    status, _ = self.probe(method, path)
                    self.assertIn(status, (404, 405, 501), "%s %s answers %s but the page does "
                                  "not list it" % (method, path, status))


class StatusPortDocsTests(unittest.TestCase):
    def test_every_status_port_route_is_documented(self):
        for path in (api_module.STATUS_PATH, api_module.INGEST_PATH, api_module.HEALTH_PATH):
            self.assertIn(path, DOC)
        self.assertIn("/api/v1/alerts/", DOC)
        self.assertIn("/api/v1/cards/", DOC)

    def test_healthz_is_described_as_the_tokenless_route(self):
        match = re.search(r"GET /healthz\n```\n\n(.*?)\n\n", DOC, re.S)
        self.assertIsNotNone(match, "the page has no /healthz section")
        self.assertIn("no token", match.group(1).lower())


class LimitsDocsTests(unittest.TestCase):
    """The numbers the page states are the numbers in the code."""

    def test_the_request_and_connection_limits(self):
        self.assertIn("within %d seconds" % BoundedRequestHandler.request_deadline, DOC)
        self.assertIn("each read within %d" % BoundedRequestHandler.io_timeout, DOC)
        self.assertIn("At most %d requests" % _ExclusiveHTTPServer.max_handlers, DOC)
        self.assertIn("at most %d from one address" % _ExclusiveHTTPServer.max_per_address, DOC)

    def test_the_body_limits(self):
        self.assertIn("%d KiB on the ingest" % (api_module.INGEST_BODY_LIMIT // 1024), DOC)
        self.assertIn("%d bytes on the alerts" % api_module.ALERT_BODY_LIMIT, DOC)
        self.assertIn("%d KiB on the admin" % (admin_module.MAX_BODY // 1024), DOC)

    def test_the_admin_bind_is_not_called_loopback_only(self):
        row = next(line for line in DOC.splitlines() if line.startswith("| Admin |"))
        self.assertNotIn("only", row)
        self.assertIn("admin.allowed_hosts", DOC)
        self.assertIn("501", DOC)


if __name__ == "__main__":
    unittest.main()
