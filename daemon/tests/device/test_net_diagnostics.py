"""Why a fetch failed: the Presto reports a short reason, never raw exception text."""

import asyncio
import unittest
from unittest.mock import patch

import presto_path  # noqa: F401

import net
from net import NetworkUnavailable, StatusClient, describe_error


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class DescribeErrorTests(unittest.TestCase):
    def test_type_errno_and_stage(self):
        # MicroPython raises a plain OSError; CPython turns errno 104 into a
        # subclass, whose name describe_error reports.
        self.assertIn(describe_error(OSError(104, "secret 10.0.0.1"), "body"),
                      ("OSError 104 @body", "ConnectionResetError 104 @body"))
        self.assertEqual(describe_error(MemoryError("x"), "parse"), "MemoryError @parse")
        self.assertEqual(describe_error(asyncio.TimeoutError(), "conn"), "TimeoutError @conn")

    def test_text_of_the_exception_is_never_used(self):
        reason = describe_error(ValueError("Bearer abc123 http://10.0.0.5"), "head")
        self.assertNotIn("abc123", reason)
        self.assertNotIn("10.0.0.5", reason)

    def test_short_ascii_only(self):
        class Oddé(Exception):
            pass
        reason = describe_error(Oddé("x"), "a" * 80)
        self.assertTrue(reason.isascii())
        self.assertLessEqual(len(reason), 30)

    def test_network_unavailable_uses_its_own_label(self):
        self.assertEqual(describe_error(NetworkUnavailable("short body"), "body"),
                         "short body @body")


class FetchReasonTests(unittest.TestCase):
    def _client(self):
        client = StatusClient("127.0.0.1", 9, "t", timeout=1)
        return client

    def test_connection_refused_reports_the_stage(self):
        client = self._client()

        async def go():
            with self.assertRaises(NetworkUnavailable) as caught:
                await client.fetch_async()
            return caught.exception

        error = run(go())
        # The reason is cut at 30 characters, and on CPython the name of the
        # exception ("ConnectionRefusedError") is long enough to cut the stage.
        self.assertIn(error.reason.rsplit("@", 1)[-1], ("conn", "co"), error.reason)

    def test_stalled_server_reports_timeout_at_head(self):
        async def go():
            async def handler(reader, writer):
                await reader.read(1)
                await asyncio.sleep(5)  # accept, then say nothing
            server = await asyncio.start_server(handler, "127.0.0.1", 0)
            port = server.sockets[0].getsockname()[1]
            client = StatusClient("127.0.0.1", port, "t", timeout=1)
            try:
                with self.assertRaises(NetworkUnavailable) as caught:
                    await client.fetch_async()
            finally:
                server.close()
            return caught.exception

        error = run(go())
        self.assertIn("Timeout", error.reason)
        self.assertTrue(error.reason.endswith("@head"), error.reason)

    def test_truncated_body_reports_bytes_and_stage(self):
        async def go():
            async def handler(reader, writer):
                await reader.read(200)
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\n12345")
                await writer.drain()
                writer.close()
            server = await asyncio.start_server(handler, "127.0.0.1", 0)
            port = server.sockets[0].getsockname()[1]
            client = StatusClient("127.0.0.1", port, "t", timeout=2)
            try:
                with self.assertRaises(NetworkUnavailable) as caught:
                    await client.fetch_async()
            finally:
                server.close()
            return caught.exception, client.received

        error, received = run(go())
        self.assertEqual(error.reason, "short body @body")
        self.assertEqual(received, 5)

class AbandonedConnectTests(unittest.TestCase):
    """A timed-out connect leaves its socket open until a collection runs."""

    def test_a_timed_out_connect_is_collected_before_the_next_poll(self):
        async def never(host, port):
            await asyncio.sleep(30)

        async def go():
            client = StatusClient("192.0.2.1", 8787, "t", timeout=1)
            with patch.object(asyncio, "open_connection", never),                     patch.object(net, "_collect") as collect:
                with self.assertRaises(NetworkUnavailable):
                    await client.fetch_async()
                return collect.call_count

        self.assertEqual(run(go()), 1)

    def test_a_failed_alert_write_is_collected_too(self):
        async def never(host, port):
            await asyncio.sleep(30)

        async def go():
            client = StatusClient("192.0.2.1", 8787, "t", timeout=1)
            with patch.object(asyncio, "open_connection", never),                     patch.object(net, "_collect") as collect:
                with self.assertRaises(NetworkUnavailable):
                    await client.post_alert("codex", False)
                return collect.call_count

        self.assertEqual(run(go()), 1)



if __name__ == "__main__":
    unittest.main()
