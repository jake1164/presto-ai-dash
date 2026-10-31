"""A saved key is only ever sent to the address it was saved for.

urllib copies every request header, an Authorization key included, onto a
redirected request, whatever host it points at. The daemon therefore follows
a redirect only to the same host and port (or from http to https on that
host), and an edit that moves a source to another address must bring its key
with it. This guards both halves.
"""

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from presto_ai_dash.http_client import (HttpError, get_json, get_text, origin_of,
                                        redirect_allowed)
from presto_ai_dash.sources.litellm import LiteLLMSource
from admin.test_management import ManagementBase

KEY = "fixture-key-0123456789"


class RedirectPolicyTests(unittest.TestCase):
    def test_the_same_address_and_the_same_host_are_followed(self):
        for old, new in (
            ("http://gw.lan:4000/a", "http://gw.lan:4000/b"),
            ("http://gw.lan:4000/a", "HTTP://GW.LAN:4000/b?x=1"),
            ("http://gw.lan/a", "http://gw.lan:80/b"),
            ("https://gw.lan/a", "https://gw.lan:443/b"),
        ):
            with self.subTest(old=old, new=new):
                self.assertTrue(redirect_allowed(old, new))

    def test_http_may_move_to_https_on_the_same_host(self):
        self.assertTrue(redirect_allowed("http://gw.lan/a", "https://gw.lan/a"))
        self.assertTrue(redirect_allowed("http://gw.lan:4000/a", "https://gw.lan:4000/a"))
        self.assertTrue(redirect_allowed("http://gw.lan:4000/a", "https://gw.lan/a"))

    def test_everything_else_is_refused(self):
        for old, new in (
            ("http://gw.lan:4000/", "http://evil.example:4000/"),
            ("http://gw.lan:4000/", "http://gw.lan:4001/"),
            ("http://gw.lan/", "http://gw.lan.evil.example/"),
            ("http://gw.lan/", "http://169.254.169.254/latest/meta-data/"),
            ("https://gw.lan/", "http://gw.lan/"),
            ("http://gw.lan/", "ftp://gw.lan/file"),
            ("http://gw.lan/", "file:///etc/passwd"),
            ("http://gw.lan/", "https://gw.lan:8443/"),
            ("http://gw.lan:4000/", "https://gw.lan:8443/"),
            ("http://gw.lan/", "http:///nohost"),
            ("http://gw.lan/", "http://gw.lan:notaport/"),
        ):
            with self.subTest(old=old, new=new):
                self.assertFalse(redirect_allowed(old, new))

    def test_two_spellings_of_one_address_have_one_origin(self):
        self.assertEqual(origin_of("http://Localhost:4000"), origin_of("http://localhost:4000/path/"))
        self.assertEqual(origin_of("https://gw.lan"), origin_of("https://gw.lan:443/"))
        self.assertNotEqual(origin_of("http://gw.lan"), origin_of("https://gw.lan"))
        self.assertNotEqual(origin_of("http://gw.lan:4000"), origin_of("http://gw.lan:4001"))
        self.assertNotEqual(origin_of("http://gw.lan"), origin_of("http://other.lan"))


class Servers(unittest.TestCase):
    """Loopback servers that record what reaches them."""

    def serve(self, handler):
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, args=(0.02,), daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server

    def target(self):
        seen = []

        class Target(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                seen.append((self.path, self.headers.get("Authorization")))
                body = b'{"ok": true}'
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        server = self.serve(Target)
        return server, seen

    def redirector(self, location):
        class Redirect(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                self.send_response(302)
                self.send_header("Location", location(self.server))
                self.send_header("Content-Length", "0")
                self.end_headers()

        return self.serve(Redirect)


class RedirectBehaviourTests(Servers):
    AUTH = {"Authorization": "Bearer " + KEY}

    def test_a_redirect_to_another_port_never_receives_the_key(self):
        target, seen = self.target()
        gateway = self.redirector(lambda _: "http://127.0.0.1:%d/steal" % target.server_port)
        with self.assertRaises(HttpError) as caught:
            get_json("http://127.0.0.1:%d/x" % gateway.server_port, headers=self.AUTH)
        self.assertEqual(caught.exception.status, 302)
        self.assertEqual(seen, [])

    def test_a_redirect_to_another_host_name_never_receives_the_key(self):
        target, seen = self.target()
        gateway = self.redirector(lambda _: "http://localhost:%d/steal" % target.server_port)
        with self.assertRaises(HttpError):
            get_json("http://127.0.0.1:%d/x" % gateway.server_port, headers=self.AUTH)
        self.assertEqual(seen, [])

    def test_text_requests_follow_the_same_rule(self):
        target, seen = self.target()
        gateway = self.redirector(lambda _: "http://127.0.0.1:%d/" % target.server_port)
        with self.assertRaises(HttpError):
            get_text("http://127.0.0.1:%d/x" % gateway.server_port)
        self.assertEqual(seen, [])

    def test_a_redirect_to_another_scheme_is_refused(self):
        gateway = self.redirector(lambda _: "ftp://127.0.0.1/secret")
        with self.assertRaises(HttpError) as caught:
            get_json("http://127.0.0.1:%d/x" % gateway.server_port, headers=self.AUTH)
        self.assertEqual(caught.exception.status, 302)

    def test_a_redirect_within_the_same_server_still_works_and_keeps_the_key(self):
        class Both(BaseHTTPRequestHandler):
            hits = []

            def log_message(self, *_):
                pass

            def do_GET(self):
                Both.hits.append((self.path, self.headers.get("Authorization")))
                if self.path == "/old":
                    self.send_response(301)
                    self.send_header("Location", "/new")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                else:
                    body = b'{"moved": true}'
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)

        server = self.serve(Both)
        data = get_json("http://127.0.0.1:%d/old" % server.server_port, headers=self.AUTH)
        self.assertEqual(data, {"moved": True})
        self.assertEqual(Both.hits, [("/old", "Bearer " + KEY), ("/new", "Bearer " + KEY)])

    def test_the_litellm_activity_call_cannot_leak_its_key(self):
        target, seen = self.target()
        gateway = self.redirector(lambda _: "http://127.0.0.1:%d/steal" % target.server_port)
        source = LiteLLMSource({"base_url": "http://127.0.0.1:%d" % gateway.server_port,
                                "api_key": KEY})
        with self.assertRaises(HttpError):
            source._daily_activity("2026-10-20", "2026-10-21")
        self.assertEqual(seen, [])


class EditedUrlTests(ManagementBase):
    def setUp(self):
        super().setUp()
        self.row = self.manager.save({"type": "litellm", "name": "Gateway", "api_key": KEY,
                                      "config": {"base_url": "http://gw.lan:4000"}})

    def edit(self, base_url, **extra):
        row = next(item for item in self.manager.list_sources()["sources"]
                   if item["id"] == self.row["id"])
        data = {"revision": row["revision"], "config": {"base_url": base_url}}
        data.update(extra)
        return self.manager.save(data, row["id"])

    def credential(self):
        return next(item for item in self.storage.source_instances()
                    if item["id"] == self.row["id"])["credential"]

    def test_a_new_address_without_the_key_is_refused_and_changes_nothing(self):
        before = self.credential()
        for url in ("http://attacker.example:4000", "http://gw.lan:4001", "https://gw.lan:4000"):
            with self.subTest(url=url), self.assertRaisesRegex(ValueError, "API key again"):
                self.edit(url)
        self.assertEqual(self.credential(), before)
        row = next(item for item in self.manager.list_sources()["sources"]
                   if item["id"] == self.row["id"])
        self.assertEqual(row["config"]["base_url"], "http://gw.lan:4000")

    def test_a_new_address_with_a_new_key_is_accepted(self):
        before = self.credential()
        saved = self.edit("http://other.lan:4000", api_key="replacement-key-0123456789")
        self.assertEqual(saved["config"]["base_url"], "http://other.lan:4000")
        self.assertNotEqual(self.credential(), before)

    def test_a_new_address_with_the_key_removed_is_accepted(self):
        saved = self.edit("http://other.lan:4000", clear_key=True)
        self.assertEqual(saved["config"]["base_url"], "http://other.lan:4000")
        self.assertIsNone(self.credential())

    def test_a_change_that_keeps_the_address_keeps_the_key(self):
        before = self.credential()
        for url in ("http://gw.lan:4000/", "HTTP://GW.LAN:4000/prefix", "http://gw.lan:4000"):
            with self.subTest(url=url):
                self.edit(url)
                self.assertEqual(self.credential(), before)

    def test_a_source_without_a_key_may_move_freely(self):
        keyless = self.manager.save({"type": "litellm", "name": "No key",
                                     "config": {"base_url": "http://gw.lan:4000"}})
        saved = self.manager.save({"revision": keyless["revision"],
                                   "config": {"base_url": "http://elsewhere.lan:4000"}},
                                  keyless["id"])
        self.assertEqual(saved["config"]["base_url"], "http://elsewhere.lan:4000")

    def test_a_source_that_takes_no_key_may_move_freely(self):
        ollama = self.manager.save({"type": "ollama", "name": "Local",
                                    "config": {"base_url": "http://localhost:11434"}})
        saved = self.manager.save({"revision": ollama["revision"],
                                   "config": {"base_url": "http://other.lan:11434"}},
                                  ollama["id"])
        self.assertEqual(saved["config"]["base_url"], "http://other.lan:11434")

    def test_renaming_with_the_address_unchanged_is_not_blocked(self):
        saved = self.edit("http://gw.lan:4000", name="Renamed")
        self.assertEqual(saved["name"], "Renamed")


if __name__ == "__main__":
    unittest.main()
