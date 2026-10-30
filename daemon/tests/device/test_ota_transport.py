"""Provisioned TLS downloads never fall back to HTTP or an unverified peer."""
import io
import json
import asyncio
import tempfile
from pathlib import Path
import sys
import ssl
from types import SimpleNamespace
import unittest
from unittest.mock import patch, Mock

import presto_path
import net
import ota_transport as ota


class Stream(io.BytesIO):
    def write(self, value):
        self.request = value
        return len(value)


class TransportTests(unittest.TestCase):
    def setup_transport(self, response):
        self.raw = Mock()
        self.stream = Stream(response)
        self.context = Mock()
        self.context.wrap_socket.return_value = self.stream
        for name, value in (("socket.socket", self.raw), ("ssl.SSLContext", self.context)):
            p = patch(name, return_value=value)
            p.start()
            self.addCleanup(p.stop)
        p = patch("socket.getaddrinfo", return_value=[(2, 1, 0, '', ('192.0.2.1', 4443))])
        p.start()
        self.addCleanup(p.stop)
        return ota.Transport('192.0.2.1', 4443, 'daemon.example', b'provisioned-ca', 'test-token')

    def test_verified_manifest_has_auth_and_explicit_hostname(self):
        t = self.setup_transport(b'HTTP/1.0 200 OK\r\nContent-Length: 2\r\n\r\n{}')
        self.assertEqual(t.manifest('/api/v1/device-app/manifest'), '{}')
        self.assertEqual(self.context.verify_mode, ssl.CERT_REQUIRED)
        self.context.load_verify_locations.assert_called_once_with(cadata=b'provisioned-ca')
        self.context.wrap_socket.assert_called_once_with(self.raw, server_hostname='daemon.example')
        self.assertIn(b'Authorization: Bearer test-token\r\n', self.stream.request)
        self.assertTrue(self.stream.closed)

    def test_redirect_duplicate_length_and_encoding_are_refused(self):
        for head in (b'HTTP/1.0 302 Found\r\n\r\n',
                     b'HTTP/1.0 200 OK\r\nContent-Length: 2\r\nContent-Length: 2\r\n\r\n',
                     b'HTTP/1.0 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n',
                     b'HTTP/1.0 200 OK\r\nX-Large: ' + b'x' * 4096 + b'\r\n\r\n'):
            with self.subTest(head=head[:30]):
                t = self.setup_transport(head)
                with self.assertRaises(ValueError):
                    t.manifest('/api/v1/device-app/manifest')
                self.assertTrue(self.stream.closed)

    def test_files_stream_in_bounded_chunks_and_close(self):
        t = self.setup_transport(b'HTTP/1.0 200 OK\r\nContent-Length: 5\r\n\r\nhello')
        self.assertEqual(list(t.stream('ui.py', 2)), [b'he', b'll', b'o'])
        self.assertTrue(self.stream.closed)

    def test_truncated_file_is_refused_and_closed(self):
        t = self.setup_transport(b'HTTP/1.0 200 OK\r\nContent-Length: 5\r\n\r\nhi')
        with self.assertRaises(OSError):
            list(t.stream('ui.py', 2))
        self.assertTrue(self.stream.closed)

    def test_private_paths_and_arbitrary_manifest_paths_never_connect(self):
        t = self.setup_transport(b'')
        for name in ('../ui.py', 'config.py', 'secrets.py', 'ui.py/other', 'ui.py\n'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                list(t.stream(name, 1024))
        with self.assertRaises(ValueError):
            t.manifest('/elsewhere')
        self.raw.connect.assert_not_called()


class IdentityTests(unittest.TestCase):
    def test_missing_config_identity_uses_persistent_sidecar(self):
        with patch.object(net, 'DEVICE_ID_OVERRIDE', None), patch.object(net, 'device_setting', return_value=None), \
                patch('builtins.open', return_value=io.StringIO('a' * 32 + '\n')):
            self.assertEqual(net.current_device_id(), 'a' * 32)

    def test_existing_identity_and_preview_override_take_precedence(self):
        for override, configured, result in ((None, 'b' * 32, 'b' * 32), ('', None, None)):
            with patch.object(net, 'DEVICE_ID_OVERRIDE', override), \
                    patch.object(net, 'device_setting', return_value=configured), patch('builtins.open') as opened:
                self.assertEqual(net.current_device_id(), result)
                opened.assert_not_called()


class ProvisioningTests(unittest.TestCase):
    def setUp(self):
        p = patch.object(ota.os, 'uname', return_value=SimpleNamespace(version='tested-firmware'), create=True)
        p.start()
        self.addCleanup(p.stop)

    def profile(self, **changes):
        value = dict(enabled=True, firmware=ota.os.uname().version, host='192.0.2.1',
                     port=4443, hostname='daemon.example', ca_file='/ai_dash_state/ota-ca.der')
        value.update(changes)
        return value

    def configure(self, profile):
        app = SimpleNamespace(journal={}, _store=object(), ota=None,
                              client=SimpleNamespace(host='original', port=8787, token='original'), poll_interval=15)
        def opened(path, mode='r'):
            return io.BytesIO(b'public-ca') if mode == 'rb' else io.StringIO(json.dumps(profile))
        with patch('builtins.open', side_effect=opened), patch.object(ota, 'Transport') as transport, \
                patch('updater.make_updater', return_value=object()), \
                patch.dict(ota.sys.modules, {'machine': SimpleNamespace(WDT=lambda: None)}):
            ota.configure(app)
        return app, transport

    def test_disabled_wrong_firmware_and_expired_acceptance_fail_closed(self):
        for value in (self.profile(enabled=False), self.profile(firmware='different'),
                      self.profile(acceptance={'until': 1})):
            app, transport = self.configure(value)
            self.assertIsNone(app.ota)
            self.assertEqual(app.client.host, 'original')
            transport.assert_not_called()

    def test_normal_provisioning_preserves_poll_destination_and_credentials(self):
        app, transport = self.configure(self.profile())
        self.assertIsNotNone(app.ota)
        self.assertEqual((app.client.host, app.client.port, app.client.token), ('original', 8787, 'original'))
        transport.assert_called_once_with('192.0.2.1', 4443, 'daemon.example', b'public-ca', 'original')


class AsyncTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_async_tls_verifies_ca_and_hostname(self):
        tools = Path(presto_path.REPO_ROOT) / 'tools'
        with patch.object(sys, 'path', list(sys.path) + [str(tools)]):
            from hardware_gate.tls_fixture import fixtures, HOSTNAME
        with tempfile.TemporaryDirectory() as folder, fixtures('127.0.0.1', folder) as settings:
            for name, authority, accepted in ((HOSTNAME, settings['ca_der_hex'], True),
                    ('wrong.invalid', settings['ca_der_hex'], False),
                    (HOSTNAME, settings['other_ca_der_hex'], False)):
                with self.subTest(hostname=name, accepted=accepted):
                    transport = ota.Transport('127.0.0.1', settings['valid_port'], name,
                                              bytes.fromhex(authority), 'test-token')
                    if not accepted:
                        with self.assertRaises((ssl.SSLError, OSError)):
                            await asyncio.wait_for(transport._async_open('/probe', 8192), 3)
                        continue
                    reader, writer, body, length = await asyncio.wait_for(transport._async_open('/probe', 8192), 3)
                    try:
                        body += await reader.read(length - len(body))
                        self.assertEqual(body, b'OK')
                    finally:
                        writer.close()
                        await writer.wait_closed()

    async def test_slow_consumer_bounds_buffer_without_blocking_the_loop(self):
        from unittest.mock import AsyncMock
        reader = asyncio.StreamReader()
        reader.feed_data(b'x' * 10000)
        reader.feed_eof()
        writer = SimpleNamespace(close=Mock(), wait_closed=AsyncMock())
        transport = SimpleNamespace(timeout=1, _async_open=AsyncMock(return_value=(reader, writer, b'', 10000)))
        stream = ota.BufferedStream(transport, 'ui.py', 1024)
        await asyncio.sleep(0.05)
        self.assertLessEqual(sum(map(len, stream.queue)), 4096)
        received = bytearray()
        while True:
            try:
                chunk = next(stream)
            except StopIteration:
                break
            received.extend(chunk)
            await asyncio.sleep(0)
        self.assertEqual(received, b'x' * 10000)
        writer.close.assert_called_once()


class AbandonedConnectTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_timed_out_manifest_connect_releases_its_socket(self):
        tools = Path(presto_path.REPO_ROOT) / 'tools'
        with patch.object(sys, 'path', list(sys.path) + [str(tools)]):
            from hardware_gate.tls_fixture import fixtures, HOSTNAME
        async def never(*args, **kwargs):
            await asyncio.sleep(30)

        with tempfile.TemporaryDirectory() as folder, fixtures('127.0.0.1', folder) as settings:
            transport = ota.Transport('192.0.2.1', 443, HOSTNAME,
                                      bytes.fromhex(settings['ca_der_hex']), 't', timeout=0.2)
            with patch.object(asyncio, 'open_connection', never),                     patch.object(ota, 'release_sockets') as release:
                with self.assertRaises(asyncio.TimeoutError):
                    await transport.manifest_async('/api/v1/device-app/manifest')
            release.assert_called()
