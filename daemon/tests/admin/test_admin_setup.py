"""The authenticated **Set up a Presto** routes on the admin endpoint.

The downloads carry a persistent device token, so they are held to the same
rules as every other sensitive admin response: an admin session or bearer
token, the existing Host/Origin checks, CSRF on the mutation, and
``Cache-Control: no-store``. The one-time unlock code is in the POST reply and
in neither file.
"""

import json
from pathlib import Path
import sys
import unittest

import presto_path

SRC = Path(presto_path.REPO_ROOT) / "daemon" / "src"
TOOLS = Path(presto_path.REPO_ROOT) / "tools"
for folder in (SRC, TOOLS):
    if str(folder) not in sys.path:
        sys.path.insert(0, str(folder))

import presto_bundle  # noqa: E402
from presto_ai_dash import device_setup  # noqa: E402
from presto_ai_dash.admin import AdminServer  # noqa: E402
from admin.test_management import ManagementBase  # noqa: E402

BUNDLE = presto_bundle.snapshot()
DEVICE_TOKEN = "device-token-0123456789-abcdefghij"
ADMIN_TOKEN = "unit-secret-admin-token"
SETUP_PATH = "/api/admin/presto-setup"


class SetupServerBase(ManagementBase):
    def setUp(self):
        super().setUp()
        self.service = device_setup.SetupService(
            bundle_provider=lambda: BUNDLE,
            shared_token=DEVICE_TOKEN,
            host="0.0.0.0",
            port=self.config["daemon"]["port"] or 8787,
            release="0.50.0-rc3")
        self.admin = AdminServer(0, self.manager, ADMIN_TOKEN, setup=self.service)
        self.admin.start()
        self.addCleanup(self.admin.stop)
        self.base_url = "http://127.0.0.1:%d" % self.admin.actual_port
        from urllib.error import HTTPError
        from urllib.request import Request, build_opener, ProxyHandler
        self.opener = build_opener(ProxyHandler({}))

        def request(path=SETUP_PATH, method="GET", data=None,
                    token=ADMIN_TOKEN, headers=None, raw=None):
            extra = {"Authorization": "Bearer " + token} if token else {}
            if data is not None:
                extra["Content-Type"] = "application/json"
                raw = json.dumps(data).encode("utf-8")
            extra.update(headers or {})
            message = Request(self.base_url + path, data=raw, headers=extra, method=method)
            try:
                response = self.opener.open(message, timeout=3)
            except HTTPError as exc:
                response = exc
            with response:
                body = response.read()
                content = response.headers.get("Content-Type", "")
                return (response.status, dict(response.headers),
                        json.loads(body) if content == "application/json" else body)
        self.request = request

    def login(self):
        status, headers, session = self.request("/api/admin/session", "POST", token=ADMIN_TOKEN)
        self.assertEqual(status, 201)
        return headers["Set-Cookie"].split(";", 1)[0], session


class SetupStateTests(SetupServerBase):
    def test_it_needs_authorization(self):
        for token in (None, "wrong", DEVICE_TOKEN):
            self.assertEqual(self.request(token=token)[0], 401)

    def test_it_describes_the_running_build(self):
        status, headers, body = self.request()
        self.assertEqual(status, 200)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertTrue(body["available"])
        self.assertEqual(body["revision"], BUNDLE["revision"])
        self.assertEqual(body["label"], BUNDLE["revision"][:12])
        self.assertEqual(body["release"], "0.50.0-rc3")
        self.assertNotIn(DEVICE_TOKEN, json.dumps(body))
        self.assertNotIn('"token"', json.dumps(body))

    def test_host_origin_and_csrf_rules_are_unchanged(self):
        self.assertEqual(self.request(headers={"Origin": "https://hostile.example"})[0], 403)
        self.assertEqual(self.request(headers={"Host": "hostile.example"})[0], 403)
        cookie, session = self.login()
        headers = {"Cookie": cookie, "Origin": self.base_url}
        payload = {"host": "192.168.1.50", "port": 8787}
        self.assertEqual(self.request(SETUP_PATH, "POST", payload, token=None,
                                      headers=headers)[0], 403)
        headers["X-CSRF-Token"] = session["csrf_token"]
        self.assertEqual(self.request(SETUP_PATH, "POST", payload, token=None,
                                      headers=headers)[0], 201)

    def test_it_is_not_available_when_the_build_has_no_app_files(self):
        service = device_setup.SetupService(
            bundle_provider=lambda: (_ for _ in ()).throw(RuntimeError("none")),
            shared_token=DEVICE_TOKEN, host="0.0.0.0", port=8787, release="0.50.0")
        admin = AdminServer(0, self.manager, ADMIN_TOKEN, setup=service)
        admin.start()
        self.addCleanup(admin.stop)
        self.base_url = "http://127.0.0.1:%d" % admin.actual_port
        status, _, body = self.request()
        self.assertEqual(status, 200)
        self.assertFalse(body["available"])
        self.assertEqual(body["reason"], "bundle_unavailable")
        self.assertEqual(self.request(SETUP_PATH, "POST",
                                      {"host": "192.168.1.5", "port": 8787})[0], 409)


class SetupPageTextTests(unittest.TestCase):
    """What the page tells people before their browser does."""

    def page(self):
        folder = Path(device_setup.__file__).parent / "web"
        return (folder / "index.html").read_text(encoding="utf-8")

    def test_it_explains_the_browsers_program_file_warning(self):
        page = self.page()
        warning = page[page.index('id="setup-download-warning"'):]
        warning = warning[:warning.index("</p>")]
        self.assertIn("could harm your device", warning)
        self.assertIn("Keep", warning)
        self.assertIn("Delete", warning)
        # The explanation is on the page that makes the download, above the unlock code.
        self.assertLess(page.index('id="setup-download-warning"'),
                        page.index('id="setup-unlock-code"'))

    def test_the_prepare_button_can_be_hidden_and_brought_back(self):
        page = self.page()
        self.assertIn('id="setup-actions"', page)
        self.assertIn('id="prepare-another-setup"', page)

    def test_prepare_another_makes_the_next_download_in_one_click(self):
        # It used to only reveal the form again, so the second download took two clicks.
        folder = Path(device_setup.__file__).parent / "web"
        script = (folder / "admin.js").read_text(encoding="utf-8")
        body = script[script.index("async function prepareAnotherSetup"):]
        body = body[:body.index("\n  }\n")]
        self.assertIn("await preparePrestoSetup()", body)


class SetupDownloadTests(SetupServerBase):
    def prepare(self):
        status, _, body = self.request(SETUP_PATH, "POST",
                                       {"host": "192.168.1.50", "port": 8787})
        self.assertEqual(status, 201)
        return body

    def test_a_prepared_handoff_exposes_a_code_and_two_downloads(self):
        body = self.prepare()
        self.assertEqual(len(body["handoff_id"]), 32)
        self.assertEqual(len(body["unlock_code"].replace("-", "")), 32)
        self.assertEqual(body["downloads"]["script"],
                         "/api/admin/presto-setup/%s/script" % body["handoff_id"])
        self.assertEqual(body["downloads"]["handoff"],
                         "/api/admin/presto-setup/%s/handoff" % body["handoff_id"])
        self.assertEqual(body["daemon"], {"host": "192.168.1.50", "port": 8787})
        self.assertNotIn(ADMIN_TOKEN, json.dumps(body))

    def test_the_wifi_checkbox_reaches_the_handoff(self):
        for sent, expected in ((None, "set"), (True, "set"), (False, "keep")):
            with self.subTest(sent=sent):
                data = {"host": "192.168.1.50", "port": 8787}
                if sent is not None:
                    data["wifi"] = sent
                status, _, body = self.request(SETUP_PATH, "POST", data)
                self.assertEqual(status, 201)
                self.assertEqual(body["wifi"], sent is not False)
                opened = device_setup.open_handoff(self.service.handoff(body["handoff_id"]),
                                                   body["unlock_code"])
                self.assertEqual(opened["device"]["wifi"], expected)

    def test_a_wifi_choice_that_is_not_a_boolean_is_refused(self):
        for bad in ("no", 0, None, "keep", []):
            with self.subTest(value=bad):
                status, _, _ = self.request(SETUP_PATH, "POST",
                                            {"host": "192.168.1.50", "port": 8787, "wifi": bad})
                self.assertEqual(status, 400)

    def test_bad_bodies_are_refused_without_a_handoff(self):
        path = SETUP_PATH
        self.assertEqual(self.request(path, "POST", {"host": "192.168.1.5"}, raw=b"{}")[0], 400)
        self.assertEqual(self.request(path, "POST", raw=b"{}",
                                      headers={"Content-Type": "text/plain"})[0], 415)
        self.assertEqual(self.request(path, "POST", {"host": "", "port": 8787})[0], 400)
        self.assertEqual(self.request(path, "POST", {"host": "localhost", "port": 8787})[0], 400)
        self.assertEqual(self.request(path, "POST", {"host": "192.168.1.5", "port": 0})[0], 400)
        self.assertEqual(self.request(path, "POST", {"host": "192.168.1.5", "port": 8787,
                                                     "extra": 1})[0], 400)
        self.assertEqual(self.request(path, "POST", {"host": "x" * 500, "port": 8787})[0], 400)
        self.assertEqual(self.request(path, "POST", raw=b"x" * 2000,
                                      headers={"Content-Type": "application/json"})[0], 413)

    def test_both_downloads_are_authenticated_uncached_attachments(self):
        body = self.prepare()
        for kind, filename in (("script", "presto_setup.py"), ("handoff", "presto-setup.enc")):
            url = "/api/admin/presto-setup/%s/%s" % (body["handoff_id"], kind)
            with self.subTest(kind=kind):
                self.assertEqual(self.request(url, token=None)[0], 401)
                status, headers, data = self.request(url)
                self.assertEqual(status, 200)
                self.assertEqual(headers["Cache-Control"], "no-store")
                self.assertIn('filename="%s"' % filename, headers["Content-Disposition"])
                self.assertTrue(data)

    def test_the_code_is_in_neither_download_and_opens_the_handoff(self):
        body = self.prepare()
        code = body["unlock_code"]
        written = code.replace("-", "").encode()
        _status, _headers, script = self.request(
            body["downloads"]["script"].replace("/api/admin/", "/api/admin/"))
        _status, _headers, envelope = self.request(body["downloads"]["handoff"])
        self.assertNotIn(written, script)
        self.assertNotIn(code.encode(), script)
        self.assertNotIn(written, envelope)
        self.assertNotIn(ADMIN_TOKEN.encode(), script)
        self.assertNotIn(ADMIN_TOKEN.encode(), envelope)
        opened = device_setup.open_handoff(envelope, code)
        self.assertEqual(opened["daemon"], {"host": "192.168.1.50", "port": 8787,
                                            "token": DEVICE_TOKEN})
        self.assertIn(b"DEVICE_ID", script)

    def test_an_unknown_download_is_not_served(self):
        missing = "/api/admin/presto-setup/%s/script" % ("0" * 32)
        self.assertEqual(self.request(missing)[0], 404)
        self.assertEqual(self.request("/api/admin/presto-setup/not-an-id/script")[0], 404)

    def test_a_prepared_handoff_can_be_discarded(self):
        body = self.prepare()
        path = "/api/admin/presto-setup/" + body["handoff_id"]
        self.assertEqual(self.request(path, "DELETE")[0], 200)
        self.assertEqual(self.request(path, "DELETE")[0], 404)
        self.assertEqual(self.request(body["downloads"]["handoff"])[0], 404)


class SetupWithoutServiceTests(SetupServerBase):
    def test_the_build_says_so_instead_of_offering_a_download(self):
        admin = AdminServer(0, self.manager, ADMIN_TOKEN)
        admin.start()
        self.addCleanup(admin.stop)
        self.base_url = "http://127.0.0.1:%d" % admin.actual_port
        status, _, body = self.request()
        self.assertEqual(status, 200)
        self.assertFalse(body["available"])
        self.assertEqual(self.request(SETUP_PATH, "POST",
                                      {"host": "192.168.1.5", "port": 8787})[0], 409)


if __name__ == "__main__":
    unittest.main()
