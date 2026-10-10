"""Drawing for the Presto app.

This is the only module besides ``main.py`` that touches PicoGraphics, and it
does so only through the ``display`` object handed to it. Keeping the drawing
here means the layout can be reasoned about in one place.

Uses bitmap fonts only. The vector (Hershey) fonts are no longer built into
MicroPython firmware and ``set_font`` raises ``ValueError`` for them; PicoVector
with an ``.af`` file is the alternative, but bitmap text is faster and this is
a status display, not a poster.
"""

from cards import truncate

# Layout, in the 240x240 default framebuffer.
MARGIN = 8
TITLE_Y = 6
STATUS_Y = 26
PRIMARY_Y = 48
SECONDARY_Y = 84
DETAIL_Y = 108
DETAIL_LINE_H = 16
FOOTER_Y = 222

# Character caps; _text also fits the selected font to the available pixels.
TITLE_MAX = 22
PRIMARY_MAX = 26
SECONDARY_MAX = 30
DETAIL_MAX = 30
FOOTER_MAX = 30

# Colours, created once per display instance.
INK = (235, 235, 235)
INK_DIM = (140, 140, 140)
BACKGROUND = (16, 18, 22)
PANEL = (28, 31, 38)
DAILY = (65, 225, 255)
WEEKLY = (135, 62, 210)


class Palette(object):
    """Creates and caches pens for one display."""

    def __init__(self, display):
        self._display = display
        self._cache = {}

    def pen(self, rgb):
        """Return a pen for an (r, g, b) tuple, creating it once."""
        key = (rgb[0], rgb[1], rgb[2])
        pen = self._cache.get(key)
        if pen is None:
            pen = self._display.create_pen(key[0], key[1], key[2])
            self._cache[key] = pen
        return pen


def _text(display, palette, text, x, y, rgb, scale=2, max_chars=None,
          min_scale=1, max_width=None):
    """Fit a single row by pixel width so PicoGraphics never wraps it."""
    text = str(text).replace("\n", " ").replace("\r", " ")
    if max_chars is not None:
        text = truncate(text, max_chars)
    available = max(0, display.get_bounds()[0] - x - MARGIN)
    if max_width is not None:
        available = min(available, max_width)
    while scale > min_scale and display.measure_text(text, scale) > available:
        scale -= 1
    length = len(text)
    fitted = text
    while length and display.measure_text(fitted, scale) > available:
        length -= 1
        fitted = truncate(text, length)
    display.set_pen(palette.pen(rgb))
    display.text(fitted, x, y, available, scale)


def draw_home(display, palette, quotas, index, total, muted=False,
              width=240, height=240, scroll=0):
    """Scrollable demo quotas with a pinned title, legend, and footer."""
    from alerts import rgb_for_accent
    from home import LIST_TOP, LIST_HEIGHT, ROW_HEIGHT, quota_label, quota_width, scroll_limit

    display.set_pen(palette.pen(BACKGROUND))
    display.clear()
    accent = rgb_for_accent("green", 0.45 if muted else 1.0)
    display.set_pen(palette.pen(accent))
    display.rectangle(0, 0, 5, height)
    display.set_font("bitmap8")
    _text(display, palette, "Home", 14, TITLE_Y, INK, scale=2)
    display.set_font("bitmap6")
    _text(display, palette, "DEMO", width - 42, 12, INK_DIM, scale=1)

    # Daily above weekly: shared scale, separate lanes so neither is hidden.
    display.set_pen(palette.pen(DAILY))
    display.rectangle(14, 33, 16, 6)
    _text(display, palette, "Daily", 36, 32, DAILY, scale=1, max_width=70)
    display.set_pen(palette.pen(WEEKLY))
    display.rectangle(120, 33, 16, 6)
    _text(display, palette, "Weekly", 142, 32, WEEKLY, scale=1)

    bar_width = width - 14 - MARGIN
    limit = scroll_limit(len(quotas))
    scroll = max(0, min(limit, int(scroll)))
    display.set_clip(14, LIST_TOP, bar_width, LIST_HEIGHT)
    for index_in_list, quota in enumerate(quotas):
        y = LIST_TOP + index_in_list * ROW_HEIGHT - scroll
        if y + ROW_HEIGHT <= LIST_TOP or y >= LIST_TOP + LIST_HEIGHT:
            continue
        display.set_font("bitmap8")
        _text(display, palette, quota["name"], 14, y, INK, scale=2,
              max_width=bar_width)
        display.set_pen(palette.pen(PANEL))
        display.rectangle(14, y + 20, bar_width, 6)
        display.rectangle(14, y + 28, bar_width, 6)
        weekly_width = quota_width(quota.get("weekly"), bar_width)
        daily_width = quota_width(quota.get("daily"), bar_width)
        if daily_width:
            display.set_pen(palette.pen(DAILY))
            display.rectangle(14, y + 20, daily_width, 6)
        if weekly_width:
            display.set_pen(palette.pen(WEEKLY))
            display.rectangle(14, y + 28, weekly_width, 6)
        display.set_font("bitmap6")
        _text(display, palette, "D " + quota_label(quota.get("daily")),
              14, y + 36, DAILY, scale=1, max_width=98)
        _text(display, palette, "W " + quota_label(quota.get("weekly")),
              120, y + 36, WEEKLY, scale=1)

    display.remove_clip()
    if limit:
        # A thumb shows that more rows exist and which end of the list is near.
        rail_x = width - 5
        thumb_height = max(12, LIST_HEIGHT * LIST_HEIGHT // (len(quotas) * ROW_HEIGHT))
        thumb_y = LIST_TOP + (LIST_HEIGHT - thumb_height) * scroll // limit
        display.set_pen(palette.pen(PANEL))
        display.rectangle(rail_x, LIST_TOP, 2, LIST_HEIGHT)
        display.set_pen(palette.pen(INK_DIM))
        display.rectangle(rail_x, thumb_y, 2, thumb_height)

    display.set_font("bitmap6")
    footer = "Swipe up/down - %d demos" % len(quotas) if limit else "% of each period's limit"
    _text(display, palette, footer, 14, 208,
          INK_DIM, scale=1)
    _draw_dots(display, palette, index, total, accent, width, height)
    if muted:
        _text(display, palette, "MUTED", width - 58, FOOTER_Y, INK_DIM, scale=1)


def draw_card(display, palette, card, index, total, muted=False,
              width=240, height=240):
    """Draw one card.

    ``index``/``total`` drive the page dots at the bottom. ``muted`` dims the
    accent so the device visibly acknowledges a long-press.
    """
    from alerts import rgb_for_accent

    display.set_pen(palette.pen(BACKGROUND))
    display.clear()

    accent_rgb = rgb_for_accent(card.accent, 1.0 if not muted else 0.45)

    # Accent bar down the left edge: the status cue you can read across a desk.
    display.set_pen(palette.pen(accent_rgb))
    display.rectangle(0, 0, 5, height)

    # Title
    display.set_font("bitmap8")
    _text(display, palette, card.title, MARGIN + 6, TITLE_Y, INK,
          scale=2, max_chars=TITLE_MAX)

    # Status pill
    display.set_font("bitmap6")
    _text(display, palette, card.status.upper(), MARGIN + 6, STATUS_Y,
          accent_rgb, scale=2, max_chars=14)

    # Primary metric, the biggest thing on screen.
    display.set_font("bitmap8")
    _text(display, palette, card.primary, MARGIN + 6, PRIMARY_Y, INK,
          scale=3, max_chars=PRIMARY_MAX, min_scale=2)

    # Secondary line
    display.set_font("bitmap6")
    _text(display, palette, card.secondary, MARGIN + 6, SECONDARY_Y, INK_DIM,
          scale=2, max_chars=SECONDARY_MAX)

    # Detail lines
    y = DETAIL_Y
    for line in card.detail_lines(6):
        _text(display, palette, line, MARGIN + 6, y, INK_DIM,
              scale=1, max_chars=DETAIL_MAX)
        y += DETAIL_LINE_H

    # Page dots
    _draw_dots(display, palette, index, total, accent_rgb, width, height)

    # Mute indicator
    if muted:
        _text(display, palette, "MUTED", width - 58, FOOTER_Y, INK_DIM,
              scale=1, max_chars=8)


def _draw_dots(display, palette, index, total, accent_rgb, width, height):
    """Small dots showing which card is showing."""
    if total <= 1:
        return
    radius = 3
    spacing = 12
    block = (total - 1) * spacing
    start = (width - block) // 2
    y = height - 12
    for i in range(total):
        cx = start + i * spacing
        if i == index:
            display.set_pen(palette.pen(accent_rgb))
        else:
            display.set_pen(palette.pen((70, 74, 82)))
        display.circle(cx, y, radius)


def draw_message(display, palette, title, message, accent_rgb=None,
                 detail=None):
    """A full-screen message: offline, connecting, or an error."""
    from alerts import rgb_for_accent

    if accent_rgb is None:
        accent_rgb = rgb_for_accent("red")

    display.set_pen(palette.pen(BACKGROUND))
    display.clear()

    display.set_pen(palette.pen(accent_rgb))
    display.rectangle(0, 0, 5, 240)

    display.set_font("bitmap8")
    _text(display, palette, title, MARGIN + 6, 70, INK, scale=3,
          max_chars=PRIMARY_MAX)

    display.set_font("bitmap6")
    _text(display, palette, message, MARGIN + 6, 110, INK_DIM, scale=2,
          max_chars=SECONDARY_MAX)

    if detail:
        y = 140
        for line in detail:
            _text(display, palette, line, MARGIN + 6, y, INK_DIM, scale=1,
                  max_chars=DETAIL_MAX)
            y += DETAIL_LINE_H


def draw_boot(display, palette, message="Starting..."):
    """The white boot screen."""
    display.set_pen(palette.pen(BACKGROUND))
    display.clear()
    display.set_font("bitmap8")
    _text(display, palette, "AI Dash", MARGIN + 6, 100, INK, scale=3,
          max_chars=PRIMARY_MAX)
    display.set_font("bitmap6")
    _text(display, palette, message, MARGIN + 6, 130, INK_DIM, scale=2,
          max_chars=SECONDARY_MAX)
