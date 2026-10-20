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

# The bell that switches one service's alerts on and off. It sits in the
# top-right corner of a card: the drawn bell is small, the tap area is
# large.
BELL_ICON = 12
BELL_HIT = 48
BELL_GAP = 4
BELL_ON = "ALERTS ON"
BELL_OFF = "ALERTS OFF"
BELL_OFF_ALL = "ALERTS OFF (ALL)"

# The bell, drawn from rectangles: the primitives the preview understands.
# It is an outline, so it reads as a bell at twelve pixels.
BELL_SHAPE = (
    (5, 0, 2, 2),     # the knob
    (4, 2, 1, 1), (7, 2, 1, 1),
    (3, 3, 1, 1), (8, 3, 1, 1),
    (2, 4, 1, 1), (9, 4, 1, 1),
    (2, 5, 1, 1), (9, 5, 1, 1),
    (1, 6, 1, 1), (10, 6, 1, 1),
    (1, 7, 1, 1), (10, 7, 1, 1),
    (1, 8, 1, 1), (10, 8, 1, 1),
    (0, 9, 12, 1),    # the rim
    (5, 10, 2, 2),    # the clapper
)

# The small mark on a Home row whose service has its alerts off.
ALERT_MARK = 6
ALERT_MARK_SHAPE = (
    (2, 0, 2, 1),
    (1, 1, 4, 1),
    (1, 2, 4, 1),
    (0, 3, 6, 1),
    (2, 4, 2, 2),
)


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


def bell_box(width=240):
    """Where the bell is drawn: (x, y)."""
    return (width - MARGIN - BELL_ICON, TITLE_Y)


def bell_hit(x, y, width=240, height=240):
    """Whether a touch is on the bell, and so switches a service's alerts.

    The tap area is the top-right corner of the card, about 48 by 48
    pixels: big enough for a fingertip, and it never overlaps the page
    dots at the bottom or the Home rows.
    """
    if not isinstance(x, (int, float)) or not isinstance(y, (int, float)):
        return False
    left = width - BELL_HIT
    return left <= x < width and 0 <= y < BELL_HIT


def bell_label(alerts):
    """The label next to the bell: what the service's alerts are doing.

    ``alerts`` is the service's state as the daemon reports it, with the
    master switch applied. None means the daemon said nothing about this
    service, so there is no bell and no label.
    """
    if not isinstance(alerts, dict):
        return None
    if alerts.get("master_off"):
        return BELL_OFF_ALL
    return BELL_ON if alerts.get("enabled") else BELL_OFF


def _draw_bell(display, palette, x, y, rgb, slashed=False, crossed=False):
    """A bell made of rectangles, with a slash or a cross when asked."""
    display.set_pen(palette.pen(rgb))
    for dx, dy, w, h in BELL_SHAPE:
        display.rectangle(x + dx, y + dy, w, h)
    if not slashed and not crossed:
        return
    # The mark is drawn over the bell in the bright ink, so it reads on
    # every background.
    display.set_pen(palette.pen(INK))
    for step in range(11):
        if slashed:
            display.rectangle(x + step, y + step, 2, 2)
        if crossed:
            display.rectangle(x + step, y + step, 2, 2)
            display.rectangle(x + 10 - step, y + step, 2, 2)


def _draw_alert_mark(display, palette, x, y):
    """The small dim bell on a Home row whose service has its alerts off."""
    display.set_pen(palette.pen(INK_DIM))
    for dx, dy, w, h in ALERT_MARK_SHAPE:
        display.rectangle(x + dx, y + dy, w, h)


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
    # The mark and the badge are drawn only when they fit between the name
    # and the note. A name that has to be cut never pushes them over it.
    mark_w = (ALERT_MARK + 4) if row.get("alerts") is False else 0
    draw_mark = bool(mark_w) and name_w + mark_w <= space
    draw_badge = (bool(badge_text) and
                  name_w + badge_w + (mark_w if draw_mark else 0) <= space)
    used = (mark_w if draw_mark else 0) + (badge_w if draw_badge else 0)
    name_limit = space - used
    if name_w > name_limit:
        draw_mark = False
        draw_badge = False
        used = 0
        name_limit = space
    _text(display, palette, row["name"], name_x, y, INK, scale=1,
          max_width=name_limit)
    cursor = name_x + name_w + 4
    if draw_mark:
        # A small dim bell: this service's alerts are off.
        _draw_alert_mark(display, palette, cursor, y + 1)
        cursor += mark_w
    if draw_badge:
        _text(display, palette, badge_text, cursor, y,
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
    mark_w = (ALERT_MARK + 4) if row.get("alerts") is False else 0
    name_x = 14 + MARKER + 4
    name_limit = max(0, row_width - MARKER - status_w - 12 - mark_w)
    _text(display, palette, row["name"], name_x, y, INK, scale=1,
          max_width=name_limit)
    if mark_w:
        name_w = display.measure_text(row["name"], 1)
        if name_w + mark_w <= row_width - MARKER - status_w - 12:
            _draw_alert_mark(display, palette, name_x + name_w + 2, y + 1)
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
    healthy = row["status"] in ("ok", "active")
    detail = (row["usage"].split(" / ")[0] if healthy
              and row["usage"] != "Usage unavailable" else row["status"].upper())
    parts = row.get("parts") if healthy else None
    if parts:
        # As many leading parts as fit beside the name; never fewer than before.
        space = row_width - 6 - 4 - display.measure_text(row["name"], 1) - 8 - 2
        for count in range(len(parts), 0, -1):
            candidate = " / ".join(parts[:count])
            if display.measure_text(candidate, 1) <= space:
                detail = candidate
                break
    # Two spare pixels: the firmware wraps a line whose last word does not
    # fit, and the text is measured one pixel narrower than it is drawn.
    detail_w = display.measure_text(detail, 1) + 2
    text_y = y + (LOCAL_ROW - 8) // 2
    _marker(display, palette, row, 14, y + (LOCAL_ROW - 6) // 2, 6, stale)
    mark_w = (ALERT_MARK + 4) if row.get("alerts") is False else 0
    name_x = 14 + 6 + 4
    name_limit = max(0, row_width - 6 - 4 - detail_w - 8 - mark_w)
    _text(display, palette, row["name"], name_x, text_y, INK, scale=1,
          max_width=name_limit)
    if mark_w:
        name_w = display.measure_text(row["name"], 1)
        if name_w + mark_w <= row_width - 6 - 4 - detail_w - 8:
            _draw_alert_mark(display, palette, name_x + name_w + 2, text_y + 1)
    rgb = INK_DIM if stale or row["status"] == "ok" else rgb_for_accent(accent_for(row["status"]))
    _text(display, palette, detail, 14 + row_width - detail_w, text_y, rgb, scale=1)


def build_home_layout(display, rows, setting=None, width=240):
    """The Home layout for these rows: the list region and the strip band.

    The names are measured with the display's own font metrics, in the font
    the strip is drawn in, so the layout and the drawing always agree. The
    measurement is the drawing's, not a guess at character counts.
    """
    from home import DEFAULT_OVERFLOW_ROWS, HomeLayout, LIST_X

    if setting is None:
        setting = DEFAULT_OVERFLOW_ROWS

    def measure(text):
        display.set_font("bitmap8")
        return display.measure_text(text, 1)

    return HomeLayout(rows, setting, measure, width - LIST_X - MARGIN)


def _draw_strip(display, palette, layout, scroll, stale):
    """The overflow strip: the names of the rows that are not fully visible.

    Each name has its status marker; the last slot of the last line shows
    "+N" when the names did not fit, in the worst colour of exactly the
    rows it replaced.
    """
    from alerts import rgb_for_accent
    from cards import accent_for
    from home import SOURCE_TOP, STRIP_LINE_HEIGHT, STRIP_MARKER, STRIP_GAP

    lines, plus = layout.strip(scroll)
    if not lines and plus is None:
        return
    display.set_font("bitmap8")
    band_top = SOURCE_TOP + layout.region
    limit = layout.strip_limit()
    for index, line in enumerate(lines):
        y = band_top + index * STRIP_LINE_HEIGHT
        text_y = y + (STRIP_LINE_HEIGHT - 8) // 2
        for row, x, text in line:
            _marker(display, palette, row, x - STRIP_MARKER - STRIP_GAP,
                    y + (STRIP_LINE_HEIGHT - STRIP_MARKER) // 2, STRIP_MARKER, stale)
            _text(display, palette, text, x, text_y,
                  INK_DIM if stale else INK, scale=1,
                  max_width=max(0, limit - x))
    if plus:
        text, status, x = plus
        index = len(lines) - 1 if lines else 0
        y = band_top + index * STRIP_LINE_HEIGHT
        rgb = INK_DIM if stale else rgb_for_accent(accent_for(status))
        _text(display, palette, text, x, y + (STRIP_LINE_HEIGHT - 8) // 2, rgb,
              scale=1, max_width=max(0, limit - x))


def draw_sources(display, palette, rows, index, total, status="offline",
                 muted=False, width=240, height=240, scroll=0, stale=False,
                 layout=None):
    """Live Home: the accounts in the saved order, quota rows and local ones."""
    from alerts import rgb_for_accent
    from cards import accent_for
    from home import SOURCE_TOP
    display.set_pen(palette.pen(BACKGROUND))
    display.clear()
    accent = rgb_for_accent(accent_for(status), 0.45 if muted else 1.0)
    display.set_pen(palette.pen(accent))
    display.rectangle(0, 0, 5, height)

    if layout is None:
        layout = build_home_layout(display, rows, None, width)
    region = layout.region
    total_height = layout.total
    limit = layout.limit
    scroll = max(0, min(limit, int(scroll)))
    row_width = width - 14 - MARGIN
    display.set_clip(14, SOURCE_TOP, row_width, region)
    if not rows:
        display.set_font("bitmap6")
        _text(display, palette, "No sources enabled", 14, SOURCE_TOP, INK_DIM, scale=1)
    y = SOURCE_TOP - scroll
    for row, row_height, compact in layout.rows:
        # The list is clipped at its edges, so a partly scrolled row is cut
        # there and never drawn over the strip or the footer.
        if y + row_height > SOURCE_TOP and y < SOURCE_TOP + region:
            if compact:
                _draw_compact_local_row(display, palette, row, y, row_width, stale)
            elif "windows" in row:
                _draw_quota_row(display, palette, row, y, row_width, stale)
            else:
                _draw_local_row(display, palette, row, y, row_width, stale)
        y += row_height
    display.remove_clip()
    if limit:
        thumb = max(12, region * region // total_height)
        display.set_pen(palette.pen(PANEL))
        display.rectangle(width - 5, SOURCE_TOP, 2, region)
        display.set_pen(palette.pen(INK_DIM))
        display.rectangle(width - 5, SOURCE_TOP + (region - thumb) * scroll // limit,
                          2, thumb)
    _draw_strip(display, palette, layout, scroll, stale)
    display.set_font("bitmap6")
    if stale:
        _text(display, palette, "STALE - retrying", 14, 210, INK_DIM, scale=1)
    elif rows and all(row["type"] == "fake" for row in rows):
        _text(display, palette, "FAKE DATA", 14, 210, INK_DIM, scale=1)
    _draw_dots(display, palette, index, total, accent, width, height)
    if muted:
        _text(display, palette, "MUTED", width - 58, FOOTER_Y, INK_DIM, scale=1)


def draw_card(display, palette, card, index, total, muted=False,
              width=240, height=240, stale=False, alerts=None, message=None):
    """Draw one card.

    ``index``/``total`` drive the page dots at the bottom. ``muted`` dims the
    accent so the device visibly acknowledges a long-press. ``alerts`` is
    the service's alert state, and draws the bell in the top-right corner.
    ``message`` is a short line above the dots, used when a write fails.
    """
    from alerts import rgb_for_accent

    display.set_pen(palette.pen(BACKGROUND))
    display.clear()

    accent_rgb = rgb_for_accent(card.accent, 1.0 if not muted else 0.45)

    # Accent bar down the left edge: the status cue you can read across a desk.
    display.set_pen(palette.pen(accent_rgb))
    display.rectangle(0, 0, 5, height)

    # Title. It is cut so it never reaches the bell.
    title_x = MARGIN + 6
    label = bell_label(alerts)
    bell_x = width - MARGIN - BELL_ICON
    label_right = bell_x - BELL_GAP
    label_w = 0
    if label:
        display.set_font("bitmap6")
        label_w = display.measure_text(label, 1)
    display.set_font("bitmap8")
    title_width = None
    if label:
        title_width = max(0, label_right - label_w - BELL_GAP - title_x)
    _text(display, palette, card.title, title_x, TITLE_Y, INK,
          scale=2, max_chars=TITLE_MAX, max_width=title_width)

    # Status pill
    display.set_font("bitmap6")
    _text(display, palette, card.status.upper(), MARGIN + 6, STATUS_Y,
          accent_rgb, scale=2, max_chars=14)

    # The bell: the service's own alert switch.
    if label:
        bell_colour = accent_rgb if (alerts.get("enabled") and
                                     not alerts.get("master_off")) else INK_DIM
        _draw_bell(display, palette, bell_x, TITLE_Y, bell_colour,
                   slashed=not alerts.get("enabled"),
                   crossed=bool(alerts.get("master_off")))
        display.set_font("bitmap6")
        _text(display, palette, label, label_right - label_w, TITLE_Y + 3,
              bell_colour, scale=1, max_width=label_w + 2)

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
    if message:
        # A short line above the dots: what went wrong, or what to do.
        display.set_font("bitmap6")
        _text(display, palette, message, MARGIN + 6, 208, INK, scale=1,
              max_chars=DETAIL_MAX)
    elif stale:
        display.set_font("bitmap6")
        _text(display, palette, "STALE - retrying", MARGIN + 6, 208, INK_DIM, scale=1)
    _draw_dots(display, palette, index, total, accent_rgb, width, height)

    # Mute indicator
    if muted:
        _text(display, palette, "MUTED", width - 58, FOOTER_Y, INK_DIM,
              scale=1, max_chars=8)


DOT_SPACING = 12
DOT_BAND = 24


def _dot_span(index, total, width):
    """(dots shown, first card index shown, x of the first dot)."""
    visible = min(total, 9)
    first = max(0, min(index - visible // 2, total - visible))
    return visible, first, (width - (visible - 1) * DOT_SPACING) // 2


def dot_hit(x, y, index, total, width=240, height=240):
    """The card whose page dot is nearest a touch in the footer band, or None."""
    if total <= 1 or y < height - DOT_BAND:
        return None
    visible, first, start = _dot_span(index, total, width)
    position = (x - start + DOT_SPACING // 2) // DOT_SPACING
    if position < 0 or position >= visible:
        return None
    return first + position


def _draw_dots(display, palette, index, total, accent_rgb, width, height):
    """Small dots showing which card is showing."""
    if total <= 1:
        return
    radius = 3
    spacing = DOT_SPACING
    visible, first, start = _dot_span(index, total, width)
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
