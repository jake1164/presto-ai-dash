"""The HTTP servers bound what one slow or greedy client can hold.

The status port listens on the LAN with no login in front of it, so a client
that trickles bytes, or opens many connections, must not be able to keep
threads forever, grow them without limit, take every slot from the other
clients, or stop the daemon from shutting down. The admin server shares the
same code and is checked once as well.
"""

import socket
from pathlib import Path
import sys
import time
import unittest
from urllib.request import ProxyHandler, Request, build_opener

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from presto_ai_dash.admin import AdminServer
from presto_ai_dash.api import ApiServer
from test_management import ManagementBase


def connect(port, source=None):
    kwargs = {"source_address": (source, 0)} if source else {}
    return socket.create_connection(("127.0.0.1", port), timeout=5, **kwargs)


def read_all(sock):
    chunks = []
    while True:
        try:
            data = sock.recv(4096)
        except (ConnectionError, socket.timeout, OSError):
            break
        if not data:
            break
        chunks.append(data)
    return b"".join(chunks)


PARTIAL = b"GET /healthz HTTP/1.0\r\n"  # a request that never finishes
COMPLETE = b"GET /healthz HTTP/1.0\r\n\r\n"


class ServerLimitBase(ManagementBase):
    def make_api(self):
        api = ApiServer("127.0.0.1", 0, self.storage, "device-token",
                        status_provider=self.manager.status_payload)
        api.start()
        self.addCleanup(api.stop)
        return api

    def hold(self, api, count):
        socks = [connect(api.actual_port) for _ in range(count)]
        for sock in socks:
            self.addCleanup(sock.close)
            sock.sendall(PARTIAL)
        time.sleep(0.3)
        return socks


class RequestDeadlineTests(ServerLimitBase):
    def test_a_client_trickling_bytes_is_cut_off_at_the_deadline(self):
        api = self.make_api()
        api._server.RequestHandlerClass.request_deadline = 1.0
        sock = connect(api.actual_port)
        self.addCleanup(sock.close)
        started = time.monotonic()
        sock.sendall(b"GET /api/v1/status HTTP/1.0\r\n")
        # One header byte every 0.4 s stays inside the 3 s per-read timeout
        # but not inside the whole-request deadline.
        closed = False
        for _ in range(20):
            time.sleep(0.4)
            try:
                sock.sendall(b"X")
            except OSError:
                closed = True
                break
            sock.settimeout(0.01)
            try:
                if sock.recv(1) == b"":
                    closed = True
                    break
            except socket.timeout:
                pass
            except OSError:
                closed = True
                break
        self.assertTrue(closed, "the trickling connection was never closed")
        self.assertLess(time.monotonic() - started, 6)

    def test_a_normal_request_is_served(self):
        api = self.make_api()
        opener = build_opener(ProxyHandler({}))
        request = Request("http://127.0.0.1:%d/healthz" % api.actual_port)
        with opener.open(request, timeout=3) as response:
            self.assertEqual(response.status, 200)

    def test_a_response_is_written_in_full_after_a_slow_read(self):
        api = self.make_api()
        api._server.RequestHandlerClass.request_deadline = 2.0
        sock = connect(api.actual_port)
        self.addCleanup(sock.close)
        sock.sendall(PARTIAL)
        time.sleep(1.8)  # leaves 0.2 s of the deadline for the answer
        sock.sendall(b"\r\n")
        reply = read_all(sock)
        self.assertIn(b"200", reply.split(b"\r\n", 1)[0])
        self.assertIn(b'{"ok": true}', reply)


class HandlerCapTests(ServerLimitBase):
    def test_a_connection_over_the_cap_is_refused_with_503(self):
        api = self.make_api()
        api._server.max_handlers = 2
        self.hold(api, 2)
        extra = connect(api.actual_port)
        self.addCleanup(extra.close)
        reply = read_all(extra)
        self.assertTrue(reply.startswith(b"HTTP/1.0 503"), reply[:40])

    def test_a_freed_slot_is_reused(self):
        api = self.make_api()
        api._server.max_handlers = 1
        first = connect(api.actual_port)
        first.sendall(PARTIAL)
        time.sleep(0.3)
        first.close()
        deadline = time.monotonic() + 5
        status = None
        while time.monotonic() < deadline:
            sock = connect(api.actual_port)
            sock.sendall(COMPLETE)
            status = read_all(sock).split(b"\r\n", 1)[0]
            sock.close()
            if b"200" in status:
                break
            time.sleep(0.1)
        self.assertIn(b"200", status)
        # The slot is released when the handler has finished, a moment after
        # the client has what it asked for.
        deadline = time.monotonic() + 3
        while api._server._slots_used and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual(api._server._slots_used, 0)

    def test_one_address_cannot_take_every_slot(self):
        api = self.make_api()
        api._server.max_per_address = 2
        self.hold(api, 2)
        extra = connect(api.actual_port)
        self.addCleanup(extra.close)
        self.assertTrue(read_all(extra).startswith(b"HTTP/1.0 503"))
        self.assertEqual(api._server._slots_used, 2)

    def test_another_address_is_still_served_when_one_is_at_its_cap(self):
        api = self.make_api()
        api._server.max_per_address = 1
        self.hold(api, 1)
        try:
            other = connect(api.actual_port, source="127.0.0.2")
        except OSError:
            self.skipTest("this platform cannot connect from 127.0.0.2")
        self.addCleanup(other.close)
        other.sendall(COMPLETE)
        self.assertIn(b"200", read_all(other).split(b"\r\n", 1)[0])

    def test_the_address_count_returns_to_zero(self):
        api = self.make_api()
        for _ in range(3):
            sock = connect(api.actual_port)
            sock.sendall(COMPLETE)
            read_all(sock)
            sock.close()
        deadline = time.monotonic() + 3
        while api._server._address_counts and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertEqual(api._server._address_counts, {})
        self.assertEqual(api._server._slots_used, 0)

    def test_the_backlog_is_larger_than_the_stdlib_default(self):
        api = self.make_api()
        self.assertGreater(api._server.request_queue_size, 5)


class ShutdownTests(ServerLimitBase):
    def test_stop_does_not_wait_for_a_trickling_client(self):
        api = ApiServer("127.0.0.1", 0, self.storage, "device-token",
                        status_provider=self.manager.status_payload)
        api.STOP_GRACE_SECONDS = 0.5
        api._server.RequestHandlerClass.request_deadline = 60.0
        api.start()
        self.hold(api, 1)
        started = time.monotonic()
        api.stop()
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(api._server._slots_used, 0)


class AdminServerLimitTests(ManagementBase):
    def test_the_admin_server_shares_the_deadline_and_the_caps(self):
        admin = AdminServer(0, self.manager, "unit-secret-admin-token")
        admin.start()
        self.addCleanup(admin.stop)
        admin._server.RequestHandlerClass.request_deadline = 1.0
        sock = connect(admin.actual_port)
        self.addCleanup(sock.close)
        sock.sendall(b"GET / HTTP/1.0\r\nHost: 127.0.0.1:%d\r\n" % admin.actual_port)
        time.sleep(1.5)
        try:
            sock.sendall(b"X")
            sock.settimeout(2)
            gone = sock.recv(1) == b""
        except OSError:
            gone = True
        self.assertTrue(gone)
        self.assertEqual(admin._server.max_handlers, 64)
        self.assertEqual(admin._server.max_per_address, 16)


if __name__ == "__main__":
    unittest.main()
