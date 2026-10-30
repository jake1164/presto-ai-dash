"""Provisioned, certificate-verified downloads from one daemon endpoint.

The opt-in profile and public CA live outside both replaceable app slots.
Absent or incompatible provisioning leaves the normal dashboard unchanged.
"""

import json
import os
import re
import sys
import time

PROFILE = "/ai_dash_state/ota.json"
MAX_PROFILE = 8192
MAX_HEADERS = 4096
NAME = re.compile(r"^[A-Za-z0-9_]+\.py$")


def release_sockets():
    """Free the socket of a connect that a timeout cancelled.

    MicroPython's ``open_connection`` leaves its socket open when the connect
    is cancelled, and the stack's small socket pool is exhausted after about
    five of them (measured: every later connect then fails with ENOMEM).
    """
    import gc
    gc.collect()


class Transport:
    def __init__(self, host, port, hostname, ca, token, timeout=3):
        # The browser demo imports every shared module but has no sockets.
        # Hardware networking is needed only for an explicitly enabled profile.
        import socket
        import ssl
        if (not isinstance(host, str) or not host or any(c in host for c in "\r\n /\\")
                or type(port) is not int or not 1 <= port <= 65535
                or not isinstance(hostname, str) or not hostname
                or any(c in hostname for c in "\r\n /\\")
                or not isinstance(ca, bytes) or not ca
                or not isinstance(token, str) or any(c in token for c in "\r\n")):
            raise ValueError("invalid OTA endpoint")
        self.host, self.port, self.hostname = host, port, hostname
        self.token, self.timeout = token, timeout
        self._socket = socket
        self.context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        self.context.verify_mode = ssl.CERT_REQUIRED
        self.context.load_verify_locations(cadata=ca)
        self.trusted = True

    def _open(self, path, maximum):
        raw, stream = None, None
        try:
            raw = self._socket.socket()
            raw.settimeout(self.timeout)
            raw.connect(self._socket.getaddrinfo(self.host, self.port)[0][-1])
            stream = self.context.wrap_socket(raw, server_hostname=self.hostname)
            request = ("GET %s HTTP/1.0\r\nHost: %s\r\nAuthorization: Bearer %s\r\n"
                       "Connection: close\r\n\r\n" % (path, self.hostname, self.token)).encode()
            stream.write(request)
            # Byte-bounded lines: readline() alone could allocate an unbounded
            # response header before its length could be checked.
            consumed = 0
            def line():
                nonlocal consumed
                result = bytearray()
                while True:
                    char = stream.read(1)
                    if not char:
                        raise OSError("incomplete OTA headers")
                    consumed += len(char)
                    if consumed > MAX_HEADERS:
                        raise ValueError("OTA headers too large")
                    result.extend(char)
                    if char == b"\n":
                        return bytes(result)
            status = line().split()
            if len(status) < 2 or status[0] not in (b"HTTP/1.0", b"HTTP/1.1") or status[1] != b"200":
                raise ValueError("OTA HTTP status rejected")
            length = None
            while True:
                value = line()
                if value in (b"\r\n", b"\n"):
                    break
                key, separator, data = value.partition(b":")
                if not separator:
                    raise ValueError("OTA header rejected")
                if key.lower() == b"transfer-encoding":
                    raise ValueError("OTA transfer encoding rejected")
                if key.lower() == b"content-length":
                    if length is not None or not data.strip().isdigit():
                        raise ValueError("OTA length rejected")
                    length = int(data.strip())
            if length is None or not 0 < length <= maximum:
                raise ValueError("OTA length rejected")
            return stream, length
        except Exception:
            if stream is not None:
                stream.close()
            elif raw is not None:
                raw.close()
            raise

    def manifest(self, path):
        if path != "/api/v1/device-app/manifest":
            raise ValueError("OTA manifest path rejected")
        stream, length = self._open(path, 8192)
        try:
            data = bytearray()
            while len(data) < length:
                chunk = stream.read(min(1024, length - len(data)))
                if not chunk:
                    raise OSError("incomplete OTA manifest")
                data.extend(chunk)
            return bytes(data).decode("ascii")
        finally:
            stream.close()

    def stream(self, name, chunk_bytes):
        if (not isinstance(name, str) or name != name.strip() or not NAME.match(name)
                or "secret" in name.lower() or name.lower().startswith("config")):
            raise ValueError("OTA file path rejected")
        if type(chunk_bytes) is not int or not 1 <= chunk_bytes <= 1024:
            raise ValueError("OTA chunk size rejected")
        stream, remaining = self._open("/api/v1/device-app/file/" + name, 131072)
        try:
            while remaining:
                chunk = stream.read(min(chunk_bytes, remaining))
                if not chunk:
                    raise OSError("incomplete OTA file")
                remaining -= len(chunk)
                yield chunk
        finally:
            stream.close()

    async def _async_open(self, path, maximum):
        import asyncio
        writer = None
        try:
            print('AI Dash: update TLS connect')
            reader, writer = await asyncio.open_connection(self.host, self.port,
                ssl=self.context, server_hostname=self.hostname)
            print('AI Dash: update TLS connected')
            writer.write(("GET %s HTTP/1.0\r\nHost: %s\r\nAuthorization: Bearer %s\r\n"
                          "Connection: close\r\n\r\n" % (path, self.hostname, self.token)).encode())
            await writer.drain()
            print('AI Dash: update request sent')
            header = b''
            while b'\r\n\r\n' not in header:
                chunk = await reader.read(256)
                if not chunk:
                    raise OSError('incomplete OTA headers')
                header += chunk
                if len(header) > MAX_HEADERS:
                    raise ValueError('OTA headers too large')
            head, body = header.split(b'\r\n\r\n', 1)
            lines = head.split(b'\r\n')
            status = lines[0].split()
            if len(status) < 2 or status[0] not in (b'HTTP/1.0', b'HTTP/1.1') or status[1] != b'200':
                raise ValueError('OTA HTTP status rejected')
            fields = {}
            for line in lines[1:]:
                name, separator, value = line.partition(b':')
                name = name.lower()
                if not separator or name in fields:
                    raise ValueError('OTA header rejected')
                fields[name] = value.strip()
            length = fields.get(b'content-length', b'')
            if (b'transfer-encoding' in fields or not length.isdigit()
                    or not 0 < int(length) <= maximum or len(body) > int(length)):
                raise ValueError('OTA length rejected')
            return reader, writer, body, int(length)
        except BaseException:
            if writer is not None:
                writer.close()
            raise

    async def manifest_async(self, path):
        if path != '/api/v1/device-app/manifest':
            raise ValueError('OTA manifest path rejected')
        import asyncio
        async def fetch():
            reader, writer, body, length = await self._async_open(path, 8192)
            try:
                while len(body) < length:
                    chunk = await reader.read(min(1024, length - len(body)))
                    if not chunk:
                        raise OSError('incomplete OTA manifest')
                    body += chunk
                return body.decode('ascii')
            finally:
                writer.close()
                await writer.wait_closed()
        try:
            return await asyncio.wait_for(fetch(), self.timeout + 3)
        except BaseException:
            release_sockets()
            raise

    def stream_async(self, name, chunk_bytes):
        if (not isinstance(name, str) or name != name.strip() or not NAME.match(name)
                or 'secret' in name.lower() or name.lower().startswith('config')
                or type(chunk_bytes) is not int or not 1 <= chunk_bytes <= 1024):
            raise ValueError('OTA file path rejected')
        return BufferedStream(self, name, chunk_bytes)


class BufferedStream:
    """At most four chunks buffered; the display thread never blocks on TLS."""
    def __init__(self, transport, name, chunk_bytes):
        import asyncio
        self.queue, self.error, self.finished = [], None, False
        self.task = asyncio.create_task(self._receive(transport, name, chunk_bytes))

    async def _receive(self, transport, name, chunk_bytes):
        import asyncio
        writer = None
        try:
            reader, writer, body, length = await asyncio.wait_for(
                transport._async_open('/api/v1/device-app/file/' + name, 131072), transport.timeout + 3)
            received = len(body)
            if body:
                self.queue.append(body)
            while received < length:
                while len(self.queue) >= 4:
                    await asyncio.sleep(0.02)
                chunk = await asyncio.wait_for(reader.read(min(chunk_bytes, length - received)), transport.timeout)
                if not chunk:
                    raise OSError('incomplete OTA file')
                received += len(chunk)
                self.queue.append(chunk)
        except Exception as exc:
            self.error = exc
            release_sockets()
        finally:
            if writer is not None:
                writer.close()
                try:
                    await writer.wait_closed()
                except OSError:
                    pass
            self.finished = True

    def __next__(self):
        if self.error is not None:
            raise self.error
        if self.queue:
            return self.queue.pop(0)
        if self.finished:
            raise StopIteration
        return b''

    def __iter__(self):
        return self

    def close(self):
        self.task.cancel()
        self.queue = []


def configure(app, path=PROFILE):
    """Enable only an explicitly provisioned, matching firmware profile.

    An expiring acceptance section may point this board at an isolated daemon
    during USB-authorized commissioning. It never changes private config.py.
    """
    if app.journal is None or app._store is None:
        return
    try:
        with open(path, "r") as handle:
            text = handle.read(MAX_PROFILE + 1)
        if len(text) > MAX_PROFILE:
            return
        profile = json.loads(text)
        if not isinstance(profile, dict) or profile.get("enabled") is not True:
            return
        if profile.get("firmware") != os.uname().version:
            return
        ca_path = profile.get("ca_file")
        if ca_path != "/ai_dash_state/ota-ca.der":
            return
        with open(ca_path, "rb") as handle:
            ca = handle.read(8193)
        if not 0 < len(ca) <= 8192:
            return
        acceptance = profile.get("acceptance")
        token = app.client.token
        if acceptance is not None:
            if (not isinstance(acceptance, dict) or type(acceptance.get("until")) is not int
                    or not 1735689600 <= time.time() < acceptance["until"]):
                return
            token = acceptance["token"]
        transport = Transport(profile["host"], profile["port"], profile["hostname"], ca, token)
        runtime = sys.modules.get("machine")
        firmware = {"sha256": True, "rename": callable(getattr(os, "rename", None)),
                    "statvfs": callable(getattr(os, "statvfs", None)),
                    "watchdog": callable(getattr(runtime, "WDT", None))}
        import updater
        app.ota = updater.make_updater(app._store, transport, time.time, firmware, app.journal)
        if app.ota is not None and acceptance is not None:
            app.client.host = acceptance["host"]
            app.client.port = acceptance["port"]
            app.client.token = token
            app.poll_interval = 2
    except Exception:
        # Provisioning failure must never take down the dashboard or downgrade
        # to unauthenticated code transport.
        app.ota = None
