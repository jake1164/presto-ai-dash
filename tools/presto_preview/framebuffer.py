"""A CPython stand-in for the PicoGraphics calls made by presto/ui.py.

It rasterises into an RGB565-quantised framebuffer using the device's own
bitmap font data, so text fitting and truncation match the Presto. Only the
calls the app uses are implemented: anything new fails loudly with an
AttributeError instead of being silently drawn wrong.

It imports nothing but ``fonts``, so the same code runs under the browser's
MicroPython WebAssembly build (no ``struct`` or ``zlib`` there). PNG encoding
lives in ``png.py``, which only the CPython tools import.
"""

from fonts import FONTS


def _rgb565(r, g, b):
    """Quantise to the panel's 5-6-5 colour, expanded back to 8 bits."""
    r, g, b = r >> 3, g >> 2, b >> 3
    return bytes(((r << 3) | (r >> 2), (g << 2) | (g >> 4), (b << 3) | (b >> 2)))


class Framebuffer(object):
    def __init__(self, width=240, height=240):
        self.width = width
        self.height = height
        self.pixels = bytearray(width * height * 3)
        self.pen = _rgb565(0, 0, 0)
        self.font = FONTS["bitmap8"]
        self.clip = (0, 0, width, height)

    # -- state ---------------------------------------------------------------

    def get_bounds(self):
        return self.width, self.height

    def create_pen(self, r, g, b):
        return _rgb565(r, g, b)

    def set_pen(self, pen):
        self.pen = pen

    def set_font(self, name):
        if name not in FONTS:
            # Firmware no longer bundles the vector fonts; see ui.py.
            raise ValueError("unsupported font: %s" % name)
        self.font = FONTS[name]

    def set_clip(self, x, y, w, h):
        x0, y0 = max(0, x), max(0, y)
        x1, y1 = min(self.width, x + w), min(self.height, y + h)
        self.clip = (x0, y0, max(0, x1 - x0), max(0, y1 - y0))

    def remove_clip(self):
        self.clip = (0, 0, self.width, self.height)

    # -- primitives ----------------------------------------------------------

    def clear(self):
        x, y, w, h = self.clip
        self.rectangle(x, y, w, h)

    def rectangle(self, x, y, w, h):
        cx, cy, cw, ch = self.clip
        x0, y0 = max(x, cx), max(y, cy)
        x1, y1 = min(x + w, cx + cw), min(y + h, cy + ch)
        if x1 <= x0 or y1 <= y0:
            return
        span = self.pen * (x1 - x0)
        for row in range(y0, y1):
            start = (row * self.width + x0) * 3
            self.pixels[start:start + len(span)] = span

    def pixel(self, x, y):
        self.rectangle(x, y, 1, 1)

    def circle(self, x, y, radius):
        # Same midpoint spans as PicoGraphics::circle.
        ox, oy, err = radius, 0, -radius
        while ox >= oy:
            last_oy = oy
            err += oy
            oy += 1
            err += oy
            self.rectangle(x - ox, y + last_oy, ox * 2 + 1, 1)
            if last_oy:
                self.rectangle(x - ox, y - last_oy, ox * 2 + 1, 1)
            if err >= 0 and ox != last_oy:
                self.rectangle(x - last_oy, y + ox, last_oy * 2 + 1, 1)
                if ox:
                    self.rectangle(x - last_oy, y - ox, last_oy * 2 + 1, 1)
                err -= ox
                ox -= 1
                err -= ox

    # -- text (bitmap_fonts.cpp) ---------------------------------------------

    def _char_width(self, char, scale):
        code = ord(char)
        # Non-ASCII is remapped on the device; here it measures and draws as
        # nothing, so keep labels ASCII if the preview must be trusted.
        if code < 32 or code > 127:
            return 0
        return len(self.font["glyphs"][code - 32]) * scale

    def measure_text(self, text, scale=2, spacing=1, fixed_width=False):
        width = sum(self._char_width(char, scale) + spacing * scale for char in str(text))
        return width - spacing * scale if width > 0 else 0

    def _character(self, char, x, y, scale):
        code = ord(char)
        if code < 32 or code > 127:
            return
        for column, bits in enumerate(self.font["glyphs"][code - 32]):
            for row in range(8):
                if bits & (1 << row):
                    self.rectangle(x + column * scale, y + row * scale, scale, scale)

    def text(self, text, x, y, wrap=-1, scale=2, angle=0, spacing=1, fixed_width=False):
        if angle:
            raise NotImplementedError("rotated text is not emulated")
        text = str(text)
        if wrap is None or wrap < 0:
            wrap = 1 << 31
        line_height = (self.font["height"] + 1) * scale
        space = self._char_width(" ", scale) + spacing * scale
        char_offset = line_offset = 0
        i = 0
        while i < len(text):
            next_space = text.find(" ", i + 1)
            next_break = text.find("\n", i + 1)
            next_space = len(text) if next_space < 0 else next_space
            next_break = len(text) if next_break < 0 else next_break
            end = min(next_space, next_break)
            word = sum(self._char_width(c, scale) + spacing * scale for c in text[i:end])
            if char_offset and char_offset + word > wrap:
                char_offset = 0
                line_offset += line_height
            for char in text[i:min(end + 1, len(text))]:
                if char == "\n":
                    line_offset += line_height
                    char_offset = 0
                elif char == " ":
                    char_offset += space
                else:
                    self._character(char, x + char_offset, y + line_offset, scale)
                    char_offset += self._char_width(char, scale) + spacing * scale
            i = end + 1
