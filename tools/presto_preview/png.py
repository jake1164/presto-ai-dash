"""PNG encoding for the preview tools (CPython only).

``framebuffer.py`` is imported by the browser demo under MicroPython, which
has no ``struct`` or ``zlib``; it therefore draws only. This module is the one
place that encodes PNGs, and only the CPython tools import it.
"""

import struct
import zlib


def encode_png(pixels, width, height, scale=1):
    rows = []
    stride = width * 3
    for row in range(height):
        line = bytes(pixels[row * stride:(row + 1) * stride])
        if scale > 1:
            line = b"".join(line[px:px + 3] * scale for px in range(0, stride, 3))
        rows.extend([b"\x00" + line] * scale)

    def chunk(kind, data):
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    header = struct.pack(">IIBBBBB", width * scale, height * scale, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) +
            chunk(b"IDAT", zlib.compress(b"".join(rows), 6)) + chunk(b"IEND", b""))


def png(display, scale=2):
    """Encode a Framebuffer, pixel-doubled like the 480x480 panel."""
    return encode_png(display.pixels, display.width, display.height, scale)
