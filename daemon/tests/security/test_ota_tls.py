"""Presto app downloads over TLS, on the same port as everything else.

The device trusts only the certificate authority it was given at setup, and
checks the server against a fixed name rather than the daemon's address. These
tests drive the device's own transport (``presto/ota_transport.py``, which runs
on CPython too) against a real daemon server: TLS reaches the app routes and
nothing else, plain HTTP is unchanged on the same port, and the authority
survives a restart.
"""

import os
from pathlib import Path
import socket
import ssl
import sys
import tempfile
import unittest
from urllib.request import ProxyHandler, Request, build_opener

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import presto_path  # noqa: E402
from admin.test_management import ManagementBase  # noqa: E402
from device.test_device_app_routes import fake_bundle  # noqa: E402
from presto_ai_dash import ota_tls  # noqa: E402
from presto_ai_dash.api import ApiServer  # noqa: E402
from presto_ai_dash.device_app import DeviceApp  # noqa: E402

sys.path.insert(0, str(Path(presto_path.REPO_ROOT) / "presto"))
import ota_transport  # noqa: E402

TOKEN = "device-token"
MANIFEST = "/api/v1/device-app/manifest"

# One authority for the whole module: RSA key generation is the slow part.
IDENTITY = ota_tls.OtaTls(*ota_tls._authority(ota_tls._now()))


class OtaTlsServerTests(ManagementBase):
    def setUp(self):
        super().setUp()
        self.api = ApiServer("127.0.0.1", 0, self.storage, TOKEN,
                             status_provider=self.manager.status_payload,
                             device_app=DeviceApp(fake_bundle),
                             tls_context=IDENTITY.context)
        self.api.start()
        self.addCleanup(self.api.stop)
        self.port = self.api.actual_port

    def transport(self, ca=None, hostname=ota_tls.HOSTNAME, token=TOKEN):
        return ota_transport.Transport("127.0.0.1", self.port, hostname,
                                       ca or IDENTITY.ca_der, token)

    def tls_get(self, path):
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_verify_locations(cadata=IDENTITY.ca_der)
        with socket.create_connection(("127.0.0.1", self.port), timeout=5) as raw:
            with context.wrap_socket(raw, server_hostname=ota_tls.HOSTNAME) as stream:
                stream.sendall(("GET %s HTTP/1.0\r\nAuthorization: Bearer %s\r\n\r\n"
                                % (path, TOKEN)).encode())
                data = b""
                while True:
                    chunk = stream.recv(4096)
                    if not chunk:
                        break
                    data += chunk
        return data

    def test_the_device_transport_fetches_the_manifest_over_tls(self):
        text = self.transport().manifest(MANIFEST)
        self.assertIn('"revision"', text)

    def test_the_device_transport_fetches_a_file_over_tls(self):
        data = b"".join(self.transport().stream("ui.py", 1024))
        self.assertEqual(data, b"value = 1\n")

    def test_plain_http_on_the_same_port_is_unchanged(self):
        opener = build_opener(ProxyHandler({}))
        request = Request("http://127.0.0.1:%d/healthz" % self.port)
        with opener.open(request, timeout=3) as response:
            self.assertEqual(response.status, 200)

    def test_tls_reaches_nothing_but_the_app_routes(self):
        for path in ("/api/v1/status", "/healthz", "/api/v1/ingest"):
            with self.subTest(path=path):
                self.assertTrue(self.tls_get(path).startswith(b"HTTP/1.0 404"))

    def test_a_device_trusting_another_authority_refuses_the_daemon(self):
        other = ota_tls.OtaTls(*ota_tls._authority(ota_tls._now()))
        with self.assertRaises(ssl.SSLError):
            self.transport(ca=other.ca_der).manifest(MANIFEST)

    def test_the_certificate_is_checked_against_the_fixed_name(self):
        with self.assertRaises(ssl.SSLError):
            self.transport(hostname="other.invalid").manifest(MANIFEST)

    def test_the_wrong_token_is_refused_over_tls(self):
        with self.assertRaises(ValueError):
            self.transport(token="wrong-token").manifest(MANIFEST)

    def test_a_failed_handshake_is_logged_and_frees_its_slot(self):
        with self.assertLogs("presto_ai_dash.api", level="INFO") as logs:
            with socket.create_connection(("127.0.0.1", self.port), timeout=5) as raw:
                raw.sendall(b"\x16\x03\x01\x00\x05hello")
                raw.settimeout(5)
                try:
                    while raw.recv(4096):
                        pass
                except OSError:
                    pass
            for _ in range(50):
                if any("TLS handshake" in line for line in logs.output):
                    break
                import time
                time.sleep(0.05)
        self.assertTrue(any("TLS handshake" in line for line in logs.output))
        for _ in range(50):
            if not self.api._server._address_counts:
                break
            import time
            time.sleep(0.05)
        self.assertEqual(self.api._server._address_counts, {})


class AuthorityFileTests(unittest.TestCase):
    def test_the_authority_is_made_once_and_kept(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "dash.db" + ota_tls.FILE_SUFFIX)
            first = ota_tls.OtaTls.load_or_create(path)
            second = ota_tls.OtaTls.load_or_create(path)
            self.assertEqual(first.ca_der, second.ca_der)
            self.assertEqual(first.hostname, ota_tls.HOSTNAME)
            with open(path, "rb") as handle:
                text = handle.read()
            self.assertIn(b"PRIVATE KEY", text)

    def test_the_name_never_resolves(self):
        self.assertTrue(ota_tls.HOSTNAME.endswith(".invalid"))


if __name__ == "__main__":
    unittest.main()
