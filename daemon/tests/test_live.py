"""Device polling, stale/offline recovery, and cooperative HTTP against real sockets."""

import asyncio
import importlib.util
import json
import os
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import presto_path
from cards import parse_cards
from home import source_rows
from net import NetworkUnavailable, StatusClient, StatusError, Unauthorised, embedded_payload
from ui import Palette, draw_sources
from test_ui import Display


def response(body=b'{}', status=200):
    return (b'HTTP/1.1 %d OK\r\nContent-Length: %d\r\n\r\n' %
            (status, len(body))) + body


class AsyncHttpTests(unittest.IsolatedAsyncioTestCase):
    async def server(self, reply, delay=0):
        self.requests = []
        self.connections = []
        self.closed = asyncio.Event()

        async def handle(reader, writer):
            self.connections.append(writer)
            try:
                self.requests.append(await reader.readuntil(b'\r\n\r\n'))
                await asyncio.sleep(delay)
                for start in range(0, len(reply), 37):
                    writer.write(reply[start:start + 37])
                    await writer.drain()
                    await asyncio.sleep(0)
                await reader.read()
            except OSError:
                pass  # The timeout client deliberately closes before the reply.
            finally:
                self.closed.set()
                writer.close()
                try:
                    await writer.wait_closed()
                except OSError:
                    pass

        server = await asyncio.start_server(handle, '127.0.0.1', 0)
        self.addAsyncCleanup(server.wait_closed)
        self.addCleanup(server.close)
        self.addCleanup(lambda: [writer.close() for writer in self.connections])
        return StatusClient('127.0.0.1', server.sockets[0].getsockname()[1], 'test-device-token', timeout=1)

    async def test_fragmented_payload_auth_and_input_can_run_during_wait(self):
        client = await self.server(response(json.dumps(embedded_payload()).encode()), delay=.08)
        task = asyncio.create_task(client.fetch_async())
        await asyncio.sleep(.02)
        self.assertFalse(task.done(), 'Network wait must yield to the UI loop')
        payload = await task
        self.assertEqual(len(parse_cards(payload)[0]), 3)
        self.assertIn(b'Authorization: Bearer test-device-token\r\n', self.requests[0])
        await asyncio.wait_for(self.closed.wait(), 1)

    async def test_unauthorised_closes_connection(self):
        client = await self.server(response(status=401))
        with self.assertRaises(Unauthorised):
            await client.fetch_async()
        await asyncio.wait_for(self.closed.wait(), 1)

    async def test_redirect_is_rejected_without_forwarding_token(self):
        client = await self.server(response(status=302))
        with self.assertRaises(StatusError):
            await client.fetch_async()
        self.assertEqual(len(self.requests), 1)
        await asyncio.wait_for(self.closed.wait(), 1)

    async def test_invalid_json_and_oversize_are_rejected(self):
        for reply in (response(b'not json'),
                      b'HTTP/1.1 200 OK\r\nContent-Length: 65537\r\n\r\n',
                      b'HTTP/1.1 200 OK\r\nContent-Length: 2\r\nTransfer-Encoding: chunked\r\n\r\n',
                      b'HTTP/1.1 200 OK\r\nContent-Length: 2\r\nContent-Length: 9\r\n\r\n'):
            with self.subTest(reply=reply):
                client = await self.server(reply)
                with self.assertRaises(StatusError):
                    await client.fetch_async()
                await asyncio.wait_for(self.closed.wait(), 1)

    async def test_timeout_is_bounded_and_retry_can_succeed(self):
        client = await self.server(response(), delay=.1)
        client.timeout = .02
        with self.assertRaises(NetworkUnavailable):
            await client.fetch_async()
        # Allow the fixture's first connection to finish and observe EOF.
        await asyncio.wait_for(self.closed.wait(), 1)
        client.timeout = 1
        self.assertEqual(await client.fetch_async(), {})

    async def test_real_managed_fake_daemon_contract(self):
        from presto_ai_dash.app import Daemon
        from presto_ai_dash.config import load_config
        with patch.dict(os.environ, {}, clear=True):
            config = load_config()
            config['daemon'].update(host='127.0.0.1', port=0, database=':memory:', shared_token='device-token')
            config['admin'].update(enabled=True, port=0, token='admin-token')
            daemon = Daemon(config).setup().start()
        try:
            client = StatusClient('127.0.0.1', daemon.api.actual_port, 'device-token')
            payload = await client.fetch_async()
            self.assertEqual([card.id for card in parse_cards(payload)[0]], ['dash', 'ollama', 'litellm'])
            self.assertEqual(source_rows(payload)[0]['type'], 'fake')
            client.token = 'admin-token'
            with self.assertRaises(Unauthorised):
                await client.fetch_async()
        finally:
            daemon.stop()


class DevicePollingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location(
            'live_device_app_for_test', os.path.join(presto_path.PRESTO_DIR, 'main.py'))
        self.module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {'config': SimpleNamespace(STATIC_DEMO=False)}):
            spec.loader.exec_module(self.module)
        self.device = SimpleNamespace(display=Display(), update=lambda: None,
            wifi=SimpleNamespace(isconnected=lambda: True, connect=AsyncMock(return_value=True)),
            touch=SimpleNamespace(state=False, poll=lambda: None, x=0, y=0))
        self.app = self.module.App(self.device)
        self.clock = patch.object(self.module, '_now_ms', return_value=0)
        self.clock.start()
        self.addCleanup(self.clock.stop)
        diff = patch.object(self.module, '_ticks_diff', side_effect=lambda a, b: a - b)
        diff.start()
        self.addCleanup(diff.stop)
        self.app.client.fetch_async = AsyncMock(return_value=self.payload())

    def payload(self, count=1):
        payload = embedded_payload()
        payload['sources'] = [{'id': str(i), 'type': 'litellm', 'name': 'Gateway %s' % i,
            'status': 'ok', 'metrics': {'requests_today': 12 + i, 'tokens_today': 340}} for i in range(count)]
        return payload

    async def test_success_renders_configured_instances_not_demo_cloud_values(self):
        self.assertTrue(await self.app.poll_once())
        self.app._render()
        texts = [row[0] for row in self.device.display.rows]
        self.assertIn('Gateway 0', texts)
        self.assertIn('12 req / 340 tok', texts)
        self.assertIn('LIVE', texts)
        self.assertNotIn('OpenAI', texts)

    async def test_stale_then_offline_then_recovers(self):
        await self.app.poll_once()
        self.app.index = 1
        self.app.client.fetch_async.side_effect = NetworkUnavailable('unreachable')
        self.assertFalse(await self.app.poll_once())
        self.assertIsNone(self.app._connection_screen())
        self.app._render()
        self.assertIn('STALE - retrying', [row[0] for row in self.device.display.rows])
        with patch.object(self.module, '_now_ms', return_value=30001):
            self.assertEqual(self.app._connection_screen()[0], 'Daemon offline')
        self.app.client.fetch_async.side_effect = None
        self.assertTrue(await self.app.poll_once())
        self.assertIsNone(self.app.connection_error)
        self.assertEqual(self.app.index, 1)

    async def test_auth_and_bad_data_show_immediate_errors_and_preserve_last_good(self):
        await self.app.poll_once()
        old = self.app.cards
        self.app.client.fetch_async.side_effect = Unauthorised('wrong')
        self.assertFalse(await self.app.poll_once())
        self.assertEqual(self.app._connection_screen()[0], 'Unauthorised')
        self.app.client.fetch_async.side_effect = None
        self.app.client.fetch_async.return_value = {'cards': []}
        self.assertFalse(await self.app.poll_once())
        self.assertEqual(self.app._connection_screen()[0], 'Bad data')
        self.assertIs(self.app.cards, old)

    async def test_wifi_reconnect_is_bounded_and_missing_settings_do_not_crash(self):
        self.device.wifi.isconnected = lambda: False
        self.device.wifi.connect.return_value = False
        self.assertFalse(await self.app.poll_once())
        self.device.wifi.connect.assert_awaited_once_with(timeout=10, retries=1)
        self.app.client.fetch_async.assert_not_awaited()
        self.assertEqual(self.app._connection_screen()[0], 'No network')
        self.device.wifi.connect.return_value = True
        self.assertTrue(await self.app.poll_once())

    async def test_more_instances_scroll_and_removed_instances_clamp_scroll(self):
        self.app.client.fetch_async.return_value = self.payload(5)
        await self.app.poll_once()
        self.app.home_scroll.offset = 100
        self.app._render()
        self.assertIn('Gateway 4', [row[0] for row in self.device.display.rows])
        self.app.client.fetch_async.return_value = self.payload(1)
        await self.app.poll_once()
        self.assertEqual(self.app.home_scroll.offset, 0)

    async def test_dynamic_cards_keep_selected_account_and_wrap_swipes(self):
        def accounts(ids):
            payload = self.payload(0)
            payload.update(card_order='accounts', cards=[{'id': key, 'title': key, 'status': 'ok'}
                for key in ['dash'] + ids])
            return payload
        self.app.client.fetch_async.return_value = accounts(['a', 'b', 'c'])
        await self.app.poll_once()
        self.app.index = 2
        self.app.detail_open = True
        self.app.client.fetch_async.return_value = accounts(['new', 'c', 'a', 'b'])
        await self.app.poll_once()
        self.assertEqual(self.app._current_card().id, 'b')
        self.assertTrue(self.app.detail_open)
        self.app._next_card()
        self.assertEqual(self.app._current_card().id, 'dash')
        self.app._prev_card()
        self.assertEqual(self.app._current_card().id, 'b')
        self.app.client.fetch_async.return_value = accounts(['new', 'c', 'a'])
        await self.app.poll_once()
        self.assertEqual(self.app._current_card().id, 'dash')
        self.assertFalse(self.app.detail_open)

    async def test_unknown_usage_empty_sources_and_old_daemon_remain_honest(self):
        row = source_rows({'sources': [{'name': 'Unknown gateway', 'metrics': {}}]})[0]
        self.assertEqual(row['usage'], 'Usage unavailable')
        self.app.client.fetch_async.return_value = self.payload(0)
        await self.app.poll_once()
        self.app._render()
        self.assertIn('No sources enabled', [row[0] for row in self.device.display.rows])
        self.app.client.fetch_async.return_value = embedded_payload()
        await self.app.poll_once()
        self.assertIsNone(self.app.live_rows)
        self.app._render()
        self.assertIn('AI Dash', [row[0] for row in self.device.display.rows])


if __name__ == '__main__':
    unittest.main()
