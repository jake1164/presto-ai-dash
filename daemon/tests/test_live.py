"""Device polling, stale/offline recovery, and cooperative HTTP against real sockets."""

import asyncio
import importlib.util
import json
import os
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from envutil import clean_env

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

    async def test_incomplete_transfer_is_connection_failure_and_retry_recovers(self):
        replies = iter((b'HTTP/1.1 200 OK\r\nContent-Len',
                        b'HTTP/1.1 200 OK\r\nContent-Length: 10\r\n\r\n{}',
                        response(json.dumps(embedded_payload()).encode())))

        async def handle(reader, writer):
            await reader.readuntil(b'\r\n\r\n')
            writer.write(next(replies))
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        server = await asyncio.start_server(handle, '127.0.0.1', 0)
        self.addAsyncCleanup(server.wait_closed)
        self.addCleanup(server.close)
        client = StatusClient('127.0.0.1', server.sockets[0].getsockname()[1], 'token')
        for _ in range(2):
            with self.assertRaises(NetworkUnavailable):
                await client.fetch_async()
        self.assertEqual(await client.fetch_async(), embedded_payload())

    async def test_real_managed_fake_daemon_contract(self):
        from presto_ai_dash.app import Daemon
        from presto_ai_dash.config import load_config
        with clean_env():
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

    def mixed_payload(self):
        """Four quota accounts and two local services, in the saved order."""
        payload = embedded_payload()
        payload['card_order'] = 'accounts'
        payload['updated_at'] = '2026-10-20T12:00:00+00:00'
        payload['sources'] = []
        for index in range(4):
            payload['sources'].append({
                'id': 'cloud-%d' % index, 'type': 'claude', 'name': 'Cloud %d' % index,
                'status': 'ok',
                'quota': {'windows': [{'label': 'Weekly', 'period_minutes': 10080,
                                       'used_percent': 14, 'state': 'available',
                                       'resets_at': '2026-10-25T12:00:00+00:00',
                                       'observed_at': '2026-10-20T11:55:00+00:00'}]}})
        for index in range(2):
            payload['sources'].append({
                'id': 'local-%d' % index, 'type': 'litellm', 'name': 'Gateway %d' % index,
                'status': 'ok', 'metrics': {'requests_today': 12}})
        return payload

    async def test_the_home_scroll_uses_the_total_height_of_mixed_rows(self):
        self.app.client.fetch_async.return_value = self.mixed_payload()
        await self.app.poll_once()
        total = 4 * 46 + 2 * 18
        # The list is taller than the list area, so the overflow strip takes
        # one line of it: 204 - 18 = 186 pixels of list.
        self.assertEqual(self.app.home_layout.lines, 1)
        self.assertEqual(self.app.home_layout.region, 186)
        self.assertEqual(self.app.home_scroll.limit, total - 186)
        self.app._render()
        texts = [row[0] for row in self.device.display.rows]
        self.assertIn('Cloud 0', texts)
        # The local rows are named in the overflow strip until they are
        # scrolled into the list.
        band = [row[0] for row in self.device.display.rows if row[2] >= 190]
        self.assertIn('Gateway 0', band)
        self.assertIn('Gateway 1', band)
        # At the end of the scroll the local rows are on the list, in place.
        self.app.home_scroll.offset = self.app.home_scroll.limit
        self.app._render()
        texts = [row[0] for row in self.device.display.rows]
        self.assertIn('Gateway 0', texts)
        self.assertIn('12 req', texts)

    async def test_a_vertical_drag_scrolls_the_live_list_and_a_horizontal_one_does_not(self):
        self.app.client.fetch_async.return_value = self.mixed_payload()
        await self.app.poll_once()
        self.assertGreater(self.app.home_scroll.limit, 0)
        self.device.touch.state = True
        self.device.touch.x, self.device.touch.y = 120, 150
        self.app._poll_touch()
        self.device.touch.x, self.device.touch.y = 120, 130
        self.app._poll_touch()
        self.assertEqual(self.app.home_scroll.offset, 20)
        # A horizontal drag belongs to the cards, not to the list.
        self.device.touch.state = False
        self.app._poll_touch()
        self.device.touch.state = True
        self.device.touch.x, self.device.touch.y = 60, 150
        self.app._poll_touch()
        self.device.touch.x, self.device.touch.y = 180, 150
        self.app._poll_touch()
        self.assertEqual(self.app.home_scroll.offset, 20)

    async def test_a_drag_in_the_strip_band_scrolls_the_list(self):
        self.app.client.fetch_async.return_value = self.mixed_payload()
        await self.app.poll_once()
        self.assertGreater(self.app.home_scroll.limit, 0)
        # The band is at the bottom of the list area; a drag that starts
        # there scrolls the list.
        self.device.touch.state = True
        self.device.touch.x, self.device.touch.y = 120, 195
        self.app._poll_touch()
        self.device.touch.x, self.device.touch.y = 120, 175
        self.app._poll_touch()
        self.assertEqual(self.app.home_scroll.offset, 20)

    async def test_a_horizontal_swipe_in_the_strip_band_still_changes_cards(self):
        self.app.client.fetch_async.return_value = self.mixed_payload()
        await self.app.poll_once()
        self.device.touch.state = True
        self.device.touch.x, self.device.touch.y = 120, 195
        self.app._poll_touch()
        self.device.touch.x, self.device.touch.y = 120, 175
        self.app._poll_touch()
        self.assertEqual(self.app.home_scroll.offset, 20)
        # A horizontal drag belongs to the cards, not to the list.
        self.device.touch.state = False
        self.app._handle_gestures(self.app._poll_touch())
        self.device.touch.state = True
        self.device.touch.x, self.device.touch.y = 60, 195
        self.app._handle_gestures(self.app._poll_touch())
        self.device.touch.x, self.device.touch.y = 180, 195
        self.app._handle_gestures(self.app._poll_touch())
        self.device.touch.state = False
        self.app._handle_gestures(self.app._poll_touch())
        self.assertEqual(self.app.home_scroll.offset, 20)
        self.assertNotEqual(self.app._current_card().id, "dash")

    async def test_success_renders_configured_instances_not_demo_cloud_values(self):
        self.assertTrue(await self.app.poll_once())
        self.app._render()
        texts = [row[0] for row in self.device.display.rows]
        self.assertIn('Gateway 0', texts)
        self.assertIn('12 req / 340 tok', texts)
        self.assertEqual(texts[:2], ['Gateway 0', 'OK'])
        self.assertNotIn('OpenAI', texts)

    async def test_offline_screen_says_why_and_counts(self):
        await self.app.poll_once()
        error = NetworkUnavailable('cannot reach daemon')
        error.reason = 'TimeoutError @head'
        self.app.client.fetch_async.side_effect = error
        self.assertFalse(await self.app.poll_once())
        self.assertFalse(await self.app.poll_once())
        self.assertEqual(self.app.fail_count, 2)
        with patch.object(self.module, '_now_ms', return_value=30001):
            screen = self.app._connection_screen()
            self.assertEqual(screen, ('Daemon offline', 'TimeoutError @head'))
            self.app._render()
        texts = [row[0] for row in self.device.display.rows]
        self.assertIn('TimeoutError @head', texts)
        self.assertTrue(any(t.startswith('fails 2') for t in texts), texts)
        self.assertTrue(all(t.isascii() for t in texts))
        self.app.client.fetch_async.side_effect = None
        self.assertTrue(await self.app.poll_once())
        self.assertEqual(self.app.fail_count, 0)

    async def test_diagnostics_never_raise(self):
        self.device.wifi = SimpleNamespace(isconnected=lambda: 1 / 0)
        self.app.presto = self.device
        self.assertTrue(self.app._diag_lines()[0].startswith('fails 0'))

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

    async def test_auth_is_immediate_and_preserves_last_good(self):
        await self.app.poll_once()
        old = self.app.cards
        self.app.client.fetch_async.side_effect = Unauthorised('wrong')
        self.assertFalse(await self.app.poll_once())
        self.assertEqual(self.app._connection_screen()[0], 'Unauthorised')
        self.assertIs(self.app.cards, old)

    async def test_bad_refresh_keeps_recent_cards_stale_then_errors_and_recovers(self):
        await self.app.poll_once()
        self.app.index = 1
        self.app.detail_open = True
        old_cards, old_rows = self.app.cards, self.app.live_rows
        self.app.client.fetch_async.return_value = {'cards': []}
        for now in (1000, 13000, 29999):
            with patch.object(self.module, '_now_ms', return_value=now):
                self.assertFalse(await self.app.poll_once())
                self.assertIsNone(self.app._connection_screen())
                self.app._render()
                self.assertIn('STALE - retrying', [row[0] for row in self.device.display.rows])
                self.assertIs(self.app.cards, old_cards)
                self.assertIs(self.app.live_rows, old_rows)
                self.assertEqual(self.app.last_good_at, 0)
                self.assertEqual(self.app.index, 1)
                self.assertTrue(self.app.detail_open)
        with patch.object(self.module, '_now_ms', return_value=30000):
            self.assertEqual(self.app._connection_screen()[0], 'Bad data')
        self.app.client.fetch_async.side_effect = None
        self.app.client.fetch_async.return_value = self.payload()
        with patch.object(self.module, '_now_ms', return_value=31000):
            self.assertTrue(await self.app.poll_once())
        self.assertIsNone(self.app.connection_error)
        self.assertEqual(self.app.last_good_at, 31000)
        self.assertEqual(self.app.index, 1)

    async def test_bad_data_on_first_boot_is_immediate(self):
        self.app.client.fetch_async.side_effect = StatusError('response was not valid JSON')
        self.assertFalse(await self.app.poll_once())
        self.assertEqual(self.app._connection_screen()[0], 'Bad data')
        self.assertIsNone(self.app.last_good_at)
        self.assertEqual(self.app.cards, [])

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
