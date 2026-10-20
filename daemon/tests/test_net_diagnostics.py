"""Why a fetch failed: the Presto reports a short reason, never raw exception text."""

import asyncio
import unittest

import presto_path  # noqa: F401

from net import NetworkUnavailable, StatusClient, describe_error


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class DescribeErrorTests(unittest.TestCase):
    def test_type_errno_and_stage(self):
        self.assertEqual(describe_error(OSError(104, "secret 10.0.0.1"), "body"),
                         "OSError 104 @body")
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
        self.assertTrue(error.reason.endswith("@conn"), error.reason)

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


if __name__ == "__main__":
    unittest.main()
