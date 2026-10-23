"""The admin page's bind address and the names it answers to.

The admin page binds the loopback address by default. A container needs it
on another address, and then it needs two deliberate choices: an admin token
the operator set, and the list of names the page is reached by. Without both
the daemon refuses to start the admin page and says why.

The existing rules stay: a request must come from a name the page knows, the
Origin must belong to the same place, and a change needs the session's own
CSRF token.
"""

import json
from pathlib import Path
import sys
import tempfile
import unittest
from urllib.error import HTTPError
from urllib.request import Request, build_opener, ProxyHandler

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from envutil import clean_env
from presto_ai_dash.admin import AdminServer
from presto_ai_dash.app import Daemon
from presto_ai_dash.config import (ConfigError, admin_bind_error, load_config,
                                   parse_allowed_hosts)
from presto_ai_dash.effective import (DEFAULT, ENVIRONMENT, FILE,
                                      effective_settings)
from admin.test_management import ManagementBase


def config_with(file_text="", environ=None):
    """A loaded configuration for a bootstrap file and an environment."""
    with tempfile.TemporaryDirectory() as name:
        path = Path(name) / "dash.toml"
        path.write_text(file_text, encoding="utf-8")
        with clean_env(**(environ or {})):
            return load_config(str(path))


class ConfigPrecedenceTests(unittest.TestCase):
    """Environment, then the file, then the built-in default."""

    def test_the_admin_binds_loopback_by_default(self):
        config = config_with()
        self.assertEqual(config["admin"]["host"], "127.0.0.1")
        self.assertEqual(config["admin"]["allowed_hosts"], [])

    def test_the_file_sets_the_bind_address_and_the_names(self):
        config = config_with('[admin]\nhost = "0.0.0.0"\n'
                              'allowed_hosts = ["dash.example.com", "localhost:8788"]\n')
        self.assertEqual(config["admin"]["host"], "0.0.0.0")
        self.assertEqual(config["admin"]["allowed_hosts"],
                         ["dash.example.com", "localhost:8788"])

    def test_the_environment_beats_the_file(self):
        config = config_with('[admin]\nhost = "0.0.0.0"\n'
                              'allowed_hosts = ["dash.example.com"]\n',
                              {"DASH_ADMIN_HOST": "127.0.0.1",
                               "DASH_ADMIN_ALLOWED_HOSTS": "localhost:8788,127.0.0.1:8788"})
        self.assertEqual(config["admin"]["host"], "127.0.0.1")
        self.assertEqual(config["admin"]["allowed_hosts"],
                         ["localhost:8788", "127.0.0.1:8788"])

    def test_an_empty_environment_variable_means_no_names(self):
        config = config_with('[admin]\nallowed_hosts = ["dash.example.com"]\n',
                             {"DASH_ADMIN_ALLOWED_HOSTS": ""})
        self.assertEqual(config["admin"]["allowed_hosts"], [])

    def test_unusable_names_are_refused(self):
        for value in (["http://dash.example.com"], ["dash example"], [42],
                      ["a" * 300]):
            with self.subTest(value=value):
                with self.assertRaises(ConfigError):
                    parse_allowed_hosts(value)

    def test_the_same_name_is_only_kept_once(self):
        self.assertEqual(parse_allowed_hosts("Dash.Example.com, dash.example.com"),
                         ["dash.example.com"])


class RefusalTests(unittest.TestCase):
    """A non-loopback bind address needs an explicit token and the names."""

    def admin(self, host, token="", allowed_hosts=()):
        return {"admin": {"enabled": True, "host": host, "token": token,
                          "allowed_hosts": list(allowed_hosts)}}

    def test_a_loopback_bind_needs_nothing(self):
        for host in ("127.0.0.1", "::1", "localhost"):
            with self.subTest(host=host):
                self.assertIsNone(admin_bind_error(self.admin(host)))

    def test_a_non_loopback_bind_without_a_token_is_refused(self):
        problem = admin_bind_error(self.admin("0.0.0.0", allowed_hosts=["localhost:8788"]))
        self.assertIsNotNone(problem)
        self.assertIn("admin token", problem)
        self.assertIn("DASH_ADMIN_TOKEN", problem)

    def test_a_non_loopback_bind_without_names_is_refused(self):
        problem = admin_bind_error(self.admin("0.0.0.0", token="chosen-token"))
        self.assertIsNotNone(problem)
        self.assertIn("allowed_hosts", problem)
        self.assertIn("DASH_ADMIN_ALLOWED_HOSTS", problem)

    def test_a_non_loopback_bind_with_both_is_accepted(self):
        config = self.admin("0.0.0.0", token="chosen-token",
                             allowed_hosts=["dash.example.com"])
        self.assertIsNone(admin_bind_error(config))

    def test_the_daemon_refuses_to_start_the_admin_page(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        database = str(Path(self.temp.name) / "dash.db")
        config = load_config()
        config["daemon"].update(database=database, host="127.0.0.1", port=0,
                                shared_token="device-token")
        config["admin"].update(enabled=True, port=0, host="0.0.0.0", token="")
        daemon = Daemon(config)
        self.addCleanup(daemon.stop)
        with self.assertRaises(ConfigError) as caught:
            daemon.setup()
        self.assertIn("loopback", str(caught.exception))

    def test_the_daemon_starts_with_a_token_and_the_names(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        database = str(Path(self.temp.name) / "dash.db")
        config = load_config()
        config["daemon"].update(database=database, host="127.0.0.1", port=0,
                                shared_token="device-token")
        config["admin"].update(enabled=True, port=0, host="0.0.0.0",
                                token="chosen-token",
                                allowed_hosts=["dash.example.com"])
        daemon = Daemon(config)
        self.addCleanup(daemon.stop)
        daemon.setup()
        self.assertIsNotNone(daemon.admin)


class AdminServerTests(ManagementBase):
    """The names the admin page answers to, and the rules that stay."""

    def setUp(self):
        super().setUp()
        self.admin = AdminServer(0, self.manager, "unit-secret-admin-token",
                                 host="127.0.0.1",
                                 allowed_hosts=["dash.example.com"])
        self.admin.start()
        self.addCleanup(self.admin.stop)
        self.base_url = "http://127.0.0.1:%d" % self.admin.actual_port
        self.opener = build_opener(ProxyHandler({}))

    def request(self, path="/api/admin/sources", method="GET", data=None,
                token="unit-secret-admin-token", headers=None):
        extra = {"Authorization": "Bearer " + token} if token else {}
        if data is not None:
            extra["Content-Type"] = "application/json"
            raw = json.dumps(data).encode("utf-8")
        else:
            raw = None
        extra.update(headers or {})
        request = Request(self.base_url + path, data=raw, headers=extra, method=method)
        try:
            response = self.opener.open(request, timeout=3)
        except HTTPError as exc:
            response = exc
        with response:
            body = response.read()
            return response.status, dict(response.headers), body

    def test_the_loopback_names_still_answer(self):
        self.assertEqual(self.request()[0], 200)

    def test_a_configured_name_answers(self):
        status, _, _ = self.request(headers={"Host": "dash.example.com"})
        self.assertEqual(status, 200)

    def test_an_unlisted_name_is_refused(self):
        status, _, _ = self.request(headers={"Host": "hostile.example"})
        self.assertEqual(status, 403)

    def test_a_hostile_origin_is_refused(self):
        status, _, _ = self.request(headers={"Host": "dash.example.com",
                                             "Origin": "https://hostile.example"})
        self.assertEqual(status, 403)

    def test_the_same_name_over_https_answers(self):
        status, _, _ = self.request(headers={"Host": "dash.example.com",
                                             "Origin": "https://dash.example.com"})
        self.assertEqual(status, 200)

    def test_a_change_needs_the_origin_and_the_csrf_token(self):
        status, headers, body = self.request("/api/admin/session", "POST")
        self.assertEqual(status, 201)
        cookie = headers["Set-Cookie"].split(";", 1)[0]
        session = json.loads(body)
        # No Origin at all: refused, as before.
        headers = {"Cookie": cookie}
        self.assertEqual(self.request("/api/admin/display", "PUT",
                                      {"overflow_rows": 2}, token=None,
                                      headers=headers)[0], 403)
        # A foreign Origin: refused.
        headers = {"Cookie": cookie, "Origin": "https://hostile.example",
                   "X-CSRF-Token": session["csrf_token"]}
        self.assertEqual(self.request("/api/admin/display", "PUT",
                                      {"overflow_rows": 2}, token=None,
                                      headers=headers)[0], 403)
        # The right Origin without the CSRF token: refused.
        headers = {"Cookie": cookie, "Origin": "https://dash.example.com",
                   "Host": "dash.example.com"}
        self.assertEqual(self.request("/api/admin/display", "PUT",
                                      {"overflow_rows": 2}, token=None,
                                      headers=headers)[0], 403)
        # The right Origin and the CSRF token: allowed.
        headers["X-CSRF-Token"] = session["csrf_token"]
        self.assertEqual(self.request("/api/admin/display", "PUT",
                                      {"overflow_rows": 2}, token=None,
                                      headers=headers)[0], 200)

    def test_the_token_is_still_needed(self):
        self.assertEqual(self.request(token=None)[0], 401)
        self.assertEqual(self.request(token="device-token")[0], 401)


class EffectiveRowTests(ManagementBase):
    """The Settings tab says where the bind address and the names came from."""

    def rows(self, file_text="", environ=None):
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / "dash.toml"
            path.write_text(file_text, encoding="utf-8")
            with clean_env(**(environ or {})):
                config = load_config(str(path))
            entries = effective_settings(config, self.storage, str(path), environ or {})
        return {entry["name"]: entry for entry in entries}

    def test_the_default_rows_say_so(self):
        rows = self.rows()
        self.assertEqual(rows["Admin bind address"]["value"], "127.0.0.1")
        self.assertEqual(rows["Admin bind address"]["origin"], DEFAULT)
        self.assertEqual(rows["Admin allowed hosts"]["value"], "none")
        self.assertEqual(rows["Admin allowed hosts"]["origin"], DEFAULT)

    def test_the_file_rows_say_the_file(self):
        rows = self.rows('[admin]\nhost = "0.0.0.0"\n'
                         'allowed_hosts = ["dash.example.com"]\n')
        self.assertEqual(rows["Admin bind address"]["value"], "0.0.0.0")
        self.assertEqual(rows["Admin bind address"]["origin"], FILE)
        self.assertEqual(rows["Admin allowed hosts"]["value"], "dash.example.com")
        self.assertEqual(rows["Admin allowed hosts"]["origin"], FILE)

    def test_the_environment_rows_say_the_environment(self):
        rows = self.rows('[admin]\nhost = "0.0.0.0"\n'
                         'allowed_hosts = ["dash.example.com"]\n',
                         {"DASH_ADMIN_HOST": "127.0.0.1",
                          "DASH_ADMIN_ALLOWED_HOSTS": "localhost:8788"})
        self.assertEqual(rows["Admin bind address"]["value"], "127.0.0.1")
        self.assertEqual(rows["Admin bind address"]["origin"], ENVIRONMENT)
        self.assertEqual(rows["Admin allowed hosts"]["value"], "localhost:8788")
        self.assertEqual(rows["Admin allowed hosts"]["origin"], ENVIRONMENT)


if __name__ == "__main__":
    unittest.main()
