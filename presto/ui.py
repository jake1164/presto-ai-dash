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
DETAIL_MAX = 34
FOOTER_MAX = 30

# Colours, created once per display instance.
INK = (235, 235, 235)
INK_DIM = (140, 140, 140)
BACKGROUND = (16, 18, 22)
PANEL = (28, 31, 38)
DAILY = (65, 225, 255)
WEEKLY = (135, 62, 210)
AMBER = (255, 169, 56)
MONTHLY = AMBER
LIMIT = (255, 64, 64)

# Live Home rows: a name line, then a bars/reset line.
MARKER = 8
BARS_H = 11


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
    limit = scroll_limit(len(quotas) * ROW_HEIGHT, LIST_HEIGHT)
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


def _window_rgb(window, dim):
    """Cyan for a day or less, purple for longer, orange monthly; red at limit."""
    if dim or window["state"] != "available":
        return INK_DIM
    if (window["percent"] or 0) >= 100:
        return LIMIT
    period = window.get("period_minutes")
    if not period:
        return MONTHLY
    return DAILY if period <= 1440 else WEEKLY


def _marker(display, palette, row, x, y, size, stale):
    """Status colour; solid only when the daemon reports the source active."""
    from alerts import rgb_for_accent
    from cards import accent_for
    rgb = INK_DIM if stale else rgb_for_accent(accent_for(row["status"]))
    display.set_pen(palette.pen(rgb))
    display.rectangle(x, y, size, size)
    if row["status"] != "active":
        display.set_pen(palette.pen(BACKGROUND))
        display.rectangle(x + 2, y + 2, size - 4, size - 4)
    return rgb


def _freshness(row, stale):
    """Line-1 note: labelled reset countdown, or why there is none."""
    from home import reset_label, window_tag
    hint = row.get("hint")
    # A daemon hint takes the note slot; the name makes room for it below.
    if hint:
        return hint, INK_DIM
    binding = row.get("binding")
    if binding is None:
        return "", INK_DIM
    window = row["windows"][binding]
    if stale or window["state"] == "stale":
        # Never present an old reading, or a passed reset, as current.
        # The daemon's age says nothing about how long the device lost it.
        ago = "" if stale else reset_label(window.get("age"))
        return ("last known " + ago + " ago") if ago else "last known", INK_DIM
    reset_in = window.get("reset_in")
    if reset_in is None:
        return "", INK_DIM
    if reset_in <= 0:
        return "awaiting refresh", INK_DIM
    return "%s resets %s" % (window_tag(window), reset_label(reset_in)), _window_rgb(window, stale)


def _draw_quota_row(display, palette, row, y, row_width, stale):
    """Name and reset; big labelled percentages; one stacked bar per window."""
    from home import quota_label, quota_width, home_windows, window_tag
    right = 14 + row_width
    _marker(display, palette, row, 14, y, MARKER, stale)
    note, note_rgb = _freshness(row, stale)
    display.set_font("bitmap8")
    # Firmware wraps at the exact width, so leave slack after the note.
    note_w = display.measure_text(note, 1) + 2 if note else 0
    name_x = 14 + MARKER + 4
    space = max(0, row_width - MARKER - 4 - note_w - 8)
    badge = row.get("reset_badge")
    badge_text = badge.get("text") if isinstance(badge, dict) else None
    badge_w = display.measure_text(badge_text, 1) + 4 if badge_text else 0
    name_w = display.measure_text(row["name"], 1)
    # The badge is drawn only when it fits between the name and the note.
    # A hint or a reset note always wins the space.
    draw_badge = bool(badge_text) and name_w + badge_w <= space
    _text(display, palette, row["name"], name_x, y, INK, scale=1,
          max_width=space - badge_w if draw_badge else space)
    if draw_badge:
        _text(display, palette, badge_text, name_x + name_w + 4, y,
              AMBER if badge.get("amber") else INK_DIM, scale=1)
    if note:
        _text(display, palette, note, right - note_w, y, note_rgb, scale=1)

    windows = home_windows(row)
    cells = []
    for window in windows:
        # A passed reset is never a reading: not fresh, not dimmed, not 0%.
        # An expired value survives on the detail card only.
        percent = None if window.get("expired") else window["percent"]
        cells.append((window_tag(window), quota_label(percent),
                      _window_rgb(window, stale)))
    scale = 2
    if sum(display.measure_text(t, 1) + 2 + display.measure_text(v, 2) + 10
           for t, v, _ in cells) - 10 > row_width:
        scale = 1
    x = 14
    value_y = y + 11
    for tag, value, rgb in cells:
        _text(display, palette, tag, x, value_y + 8 * scale - 8, INK_DIM, scale=1)
        x += display.measure_text(tag, 1) + 2
        _text(display, palette, value, x, value_y, rgb, scale=scale)
        x += display.measure_text(value, scale) + 10

    bars_y = value_y + 8 * scale + 3
    measured = [w for w in windows if w["percent"] is not None]
    if not measured:
        # Nothing measured: say what is known instead of drawing empty bars.
        text = row.get("activity") or (windows[0]["label"] + " " + windows[0]["state"]
                                       if windows else "")
        display.set_font("bitmap6")
        _text(display, palette, text, 14, bars_y + 2, INK_DIM, scale=1)
        return
    bar_h = min(5, max(2, (BARS_H - (len(windows) - 1)) // len(windows)))
    for window in windows:
        display.set_pen(palette.pen(PANEL))
        display.rectangle(14, bars_y, row_width, bar_h)
        # An expired window's bar stays empty; a stale one keeps its dim fill.
        filled = 0 if window.get("expired") else quota_width(window["percent"], row_width)
        if filled:
            display.set_pen(palette.pen(_window_rgb(window, stale)))
            display.rectangle(14, bars_y, filled, bar_h)
        bars_y += bar_h + 1


def _draw_local_row(display, palette, row, y, row_width, stale):
    """Fallback list row when no quota accounts exist: name/status, usage."""
    rgb = _marker(display, palette, row, 14, y, MARKER, stale)
    display.set_font("bitmap8")
    status = row["status"].upper()
    status_w = display.measure_text(status, 1)
    _text(display, palette, row["name"], 14 + MARKER + 4, y, INK, scale=1,
          max_width=max(0, row_width - MARKER - status_w - 12))
    _text(display, palette, status, 14 + row_width - status_w, y, rgb, scale=1)
    display.set_font("bitmap6")
    _text(display, palette, row["usage"], 14, y + 13, INK_DIM, scale=1)


def _draw_compact_local_row(display, palette, row, y, row_width, stale):
    """A local service among quota rows: marker, name, and what it is doing.

    The same text the old bottom strip showed, so nothing is lost when the
    strip is gone.
    """
    from alerts import rgb_for_accent
    from cards import accent_for
    from home import LOCAL_ROW
    display.set_font("bitmap8")
    # A healthy service shows what it is doing; otherwise its status.
    detail = (row["usage"].split(" / ")[0] if row["status"] in ("ok", "active")
              and row["usage"] != "Usage unavailable" else row["status"].upper())
    # Two spare pixels: the firmware wraps a line whose last word does not
    # fit, and the text is measured one pixel narrower than it is drawn.
    detail_w = display.measure_text(detail, 1) + 2
    text_y = y + (LOCAL_ROW - 8) // 2
    _marker(display, palette, row, 14, y + (LOCAL_ROW - 6) // 2, 6, stale)
    _text(display, palette, row["name"], 14 + 6 + 4, text_y, INK, scale=1,
          max_width=max(0, row_width - 6 - 4 - detail_w - 8))
    rgb = INK_DIM if stale or row["status"] == "ok" else rgb_for_accent(accent_for(row["status"]))
    _text(display, palette, detail, 14 + row_width - detail_w, text_y, rgb, scale=1)


def draw_sources(display, palette, rows, index, total, status="offline",
                 muted=False, width=240, height=240, scroll=0, stale=False):
    """Live Home: the accounts in the saved order, quota rows and local ones."""
    from alerts import rgb_for_accent
    from cards import accent_for
    from home import SOURCE_TOP, SOURCE_HEIGHT, home_layout, home_total_height, scroll_limit
    display.set_pen(palette.pen(BACKGROUND))
    display.clear()
    accent = rgb_for_accent(accent_for(status), 0.45 if muted else 1.0)
    display.set_pen(palette.pen(accent))
    display.rectangle(0, 0, 5, height)

    layout = home_layout(rows)
    total_height = home_total_height(rows)
    limit = scroll_limit(total_height, SOURCE_HEIGHT)
    scroll = max(0, min(limit, int(scroll)))
    row_width = width - 14 - MARGIN
    display.set_clip(14, SOURCE_TOP, row_width, SOURCE_HEIGHT)
    if not rows:
        display.set_font("bitmap6")
        _text(display, palette, "No sources enabled", 14, SOURCE_TOP, INK_DIM, scale=1)
    y = SOURCE_TOP - scroll
    for row, row_height, compact in layout:
        # The list is clipped at its edges, so a partly scrolled row is cut
        # there and never drawn over the footer.
        if y + row_height > SOURCE_TOP and y < SOURCE_TOP + SOURCE_HEIGHT:
            if compact:
                _draw_compact_local_row(display, palette, row, y, row_width, stale)
            elif "windows" in row:
                _draw_quota_row(display, palette, row, y, row_width, stale)
            else:
                _draw_local_row(display, palette, row, y, row_width, stale)
        y += row_height
    display.remove_clip()
    if limit:
        thumb = max(12, SOURCE_HEIGHT * SOURCE_HEIGHT // total_height)
        display.set_pen(palette.pen(PANEL))
        display.rectangle(width - 5, SOURCE_TOP, 2, SOURCE_HEIGHT)
        display.set_pen(palette.pen(INK_DIM))
        display.rectangle(width - 5, SOURCE_TOP + (SOURCE_HEIGHT - thumb) * scroll // limit,
                          2, thumb)
    display.set_font("bitmap6")
    if stale:
        _text(display, palette, "STALE - retrying", 14, 210, INK_DIM, scale=1)
    elif rows and all(row["type"] == "fake" for row in rows):
        _text(display, palette, "FAKE DATA", 14, 210, INK_DIM, scale=1)
    _draw_dots(display, palette, index, total, accent, width, height)
    if muted:
        _text(display, palette, "MUTED", width - 58, FOOTER_Y, INK_DIM, scale=1)


def draw_card(display, palette, card, index, total, muted=False,
              width=240, height=240, stale=False):
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
    if stale:
        display.set_font("bitmap6")
        _text(display, palette, "STALE - retrying", MARGIN + 6, 208, INK_DIM, scale=1)
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
    visible = min(total, 9)
    first = max(0, min(index - visible // 2, total - visible))
    block = (visible - 1) * spacing
    start = (width - block) // 2
    y = height - 12
    for position in range(visible):
        i = first + position
        cx = start + position * spacing
        if i == index:
            display.set_pen(palette.pen(accent_rgb))
        else:
            display.set_pen(palette.pen((70, 74, 82)))
        display.circle(cx, y, radius)
    if total > visible:
        display.set_font("bitmap6")
        _text(display, palette, "%d/%d" % (index + 1, total), 14, FOOTER_Y, INK_DIM,
              scale=1, max_width=max(0, start - 22))


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
