"""The two authenticated routes a device downloads its dashboard from.

They serve only the running daemon's own slot bundle, one file per request,
with no redirect, no query and no caller-chosen path. The shared token is not
code authentication: these tests check the transport rules, not trust.
"""

import json
from pathlib import Path
import sys
import unittest
from urllib.error import HTTPError
from urllib.request import Request, build_opener, ProxyHandler

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import presto_path  # noqa: F401,E402
from admin.test_management import ManagementBase  # noqa: E402
from presto_ai_dash.api import ApiServer  # noqa: E402
from presto_ai_dash.device_app import DeviceApp  # noqa: E402


def fake_bundle():
    import hashlib
    files = {"ai_dash_app.py": b"print('dash')\n", "ui.py": b"value = 1\n",
             "revision.txt": b"000000000000\n", "revision.sha256": b"0" * 64 + b"\n"}
    code = {name: hashlib.sha256(data).hexdigest() for name, data in files.items()
            if name in ("ai_dash_app.py", "ui.py")}
    encoded = json.dumps(code, sort_keys=True, separators=(",", ":")).encode()
    revision = hashlib.sha256(encoded).hexdigest()
    return {"scheme": "legacy-v1", "revision": "1" * 64,
            "slot": {"scheme": "slot-v1", "revision": revision, "min_bootstrap": 1,
                     "files": files},
            "bootstrap": {"version": 1}}


def manifest_hash(data):
    import hashlib
    return hashlib.sha256(data).hexdigest()


class DeviceAppRouteTests(ManagementBase):
    def setUp(self):
        super().setUp()
        self.api = ApiServer("127.0.0.1", 0, self.storage, "device-token",
                             status_provider=self.manager.status_payload,
                             device_app=DeviceApp(fake_bundle))
        self.api.start()
        self.addCleanup(self.api.stop)
        self.base_url = "http://127.0.0.1:%d" % self.api.actual_port
        self.opener = build_opener(ProxyHandler({}))
        self.manifest = "/api/v1/device-app/manifest"

    def get(self, path, token="device-token"):
        headers = {"Authorization": "Bearer " + token} if token is not None else {}
        try:
            with self.opener.open(Request(self.base_url + path, headers=headers),
                                  timeout=3) as response:
                return response.status, dict(response.headers), response.read()
        except HTTPError as exc:
            with exc:
                return exc.status, dict(exc.headers), exc.read()

    def test_the_manifest_needs_the_shared_token(self):
        status, _headers, _body = self.get(self.manifest, token=None)
        self.assertEqual(status, 401)
        status, _headers, _body = self.get(self.manifest, token="wrong")
        self.assertEqual(status, 401)

    def test_the_manifest_describes_the_bundle_the_device_will_verify(self):
        status, headers, body = self.get(self.manifest)
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Cache-Control"), "no-store")
        manifest = json.loads(body)
        self.assertEqual(manifest["scheme"], "slot-v1")
        self.assertEqual(manifest["bootstrap"], 1)
        self.assertEqual({entry["path"] for entry in manifest["files"]},
                         {"ai_dash_app.py", "ui.py"})

    def test_a_file_is_served_byte_for_byte_with_its_hash(self):
        status, headers, body = self.get("/api/v1/device-app/file/ui.py")
        self.assertEqual(status, 200)
        self.assertEqual(body, b"value = 1\n")
        self.assertEqual(headers.get("Content-Type"), "application/octet-stream")
        self.assertEqual(headers.get("X-Presto-SHA256"), manifest_hash(b"value = 1\n"))

    def test_generated_and_unknown_names_are_not_served(self):
        for name in ("revision.txt", "revision.sha256", "missing.py", "..%2Fboot.py"):
            status, _headers, _body = self.get("/api/v1/device-app/file/" + name)
            self.assertEqual(status, 404, name)

    def test_a_query_parameter_cannot_redirect_the_route(self):
        status, _headers, _body = self.get(self.manifest + "?path=/main.py")
        self.assertEqual(status, 200)

    def test_an_unverifiable_bundle_answers_bundle_unavailable(self):
        broken = fake_bundle()
        broken["slot"] = dict(broken["slot"], revision="0" * 64)

        def provider():
            return broken
        self.api.stop()
        self.api = ApiServer("127.0.0.1", 0, self.storage, "device-token",
                             status_provider=self.manager.status_payload,
                             device_app=DeviceApp(provider))
        self.api.start()
        self.addCleanup(self.api.stop)
        self.base_url = "http://127.0.0.1:%d" % self.api.actual_port
        status, _headers, body = self.get(self.manifest)
        self.assertEqual(status, 404)
        self.assertEqual(json.loads(body)["error"], "bundle_unavailable")


if __name__ == "__main__":
    unittest.main()
