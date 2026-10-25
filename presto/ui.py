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
STATUS_Y = 24
DETAIL_LINE_H = 16
FOOTER_Y = 222

# The card, from the top: the title, the status chip on the right, the
# numbers, and the detail lines. A quota card draws its windows where the
# numbers go, so the rows of a card and the bars of Home agree.
CARD_ROWS_TOP = 44
CARD_ROW_H = 30
CARD_ROW_H_TIGHT = 28
CARD_DETAIL_H = 12
CARD_BOTTOM = 204
CARD_DETAIL_LINES = 6
# One tile: a dim label over a bright value, in a two-column grid.
TILE_H = 27

# Character caps; _text also fits the selected font to the available pixels.
TITLE_MAX = 22
PRIMARY_MAX = 26
SECONDARY_MAX = 30
DETAIL_MAX = 34
FOOTER_MAX = 30

# Colours, created once per display instance.
INK = (235, 235, 235)
INK_DIM = (140, 140, 140)
INK_FAINT = (105, 105, 112)
BACKGROUND = (16, 18, 22)
PANEL = (28, 31, 38)
DAILY = (65, 225, 255)
WEEKLY = (178, 125, 255)
AMBER = (255, 169, 56)
MONTHLY = AMBER
LIMIT = (255, 64, 64)

# The track a quota bar is drawn on. It has to be visible against the
# background: the old panel colour was 1.13:1, which is nearly invisible.
# This one is 1.70:1 (see the contrast test).
TRACK = (56, 60, 72)

# Live Home rows: a name line, then a bars/reset line.
MARKER = 8
# The height and gap of a row's bars come from home.py, which also sizes the
# row to fit them: every bar is the same thickness, whatever the service.

# How far a stale window's colour is faded toward grey. It keeps a tint of its
# own hue, so three bars can still be told apart, but it stays readable:
# darkening a dark hue (the weekly purple at 45% was 1.36:1 on the background)
# made stale numerals disappear. Contrast is checked in tests/test_presto_bars.
DIM_FADE = 0.55

# The bell that switches one service's alerts on and off. It sits in the
# top-right corner of a card: the drawn bell is small, the tap area is
# large.
BELL_ICON = 20
BELL_HEIGHT = 16
BELL_HIT = 48
BELL_GAP = 4
BELL_ON = "ALERTS ON"
BELL_OFF = "ALERTS OFF"
BELL_OFF_ALL = "ALERTS OFF (ALL)"

# The bell, drawn from rectangles: the primitives the preview understands.
# It is an outline twenty pixels wide, so it reads as a bell across a desk.
BELL_SHAPE = (
    (9, 0, 2, 2),     # the knob
    (8, 2, 1, 2), (11, 2, 1, 2),
    (7, 4, 1, 2), (12, 4, 1, 2),
    (6, 6, 1, 2), (13, 6, 1, 2),
    (4, 8, 1, 2), (15, 8, 1, 2),
    (2, 10, 1, 3), (17, 10, 1, 3),
    (0, 13, 20, 1),   # the rim
    (9, 14, 2, 2),    # the clapper
)

# The mute chip: a small box in the bottom-right corner that says how long
# the mute has left ("MUTED", "MUTED 45M", "MUTED 3H"). It sits in the
# footer band, to the right of the page dots and below the STALE line, so
# it never covers the title, the bell, the status chips or the dots.
MUTE_CHIP_H = 12
MUTE_CHIP_PAD = 4
MUTE_CHIP_Y = 218
MUTE_CHIP_FONT = "bitmap6"

# The small mark on a Home row whose service has its alerts off: a dim
# eight-pixel bell with a slash cut through it. It is never interactive.
ALERT_MARK = 8
ALERT_MARK_SHAPE = (
    (3, 0, 2, 2),     # the knob
    (2, 2, 4, 2),     # the top
    (1, 4, 6, 2),     # the body
    (0, 6, 8, 1),     # the rim
)
# The slash, cut out of the bell in the background colour.
ALERT_MARK_CUT = (
    (4, 2, 2, 2),
    (3, 3, 2, 2),
    (2, 4, 2, 2),
)

# The demo chip: sample data must be obvious, so it is a filled box with
# bright text, not a dim note. Purple, because green, blue, yellow and red
# already mean a status. It takes the right end of the footer line: above
# the page dots, below the last list row, and left of nothing, beside the
# STALE line (which then keeps to the space on its left).
DEMO_CHIP_TEXT = "DEMO - SAMPLE DATA"
DEMO_CHIP_BG = (150, 60, 220)
DEMO_CHIP_Y = 208
DEMO_CHIP_H = 10
DEMO_CHIP_PAD = 3
DEMO_CHIP_FONT = "bitmap6"

# The empty Home: no service is set up, so there is nothing to show. Amber,
# not green: an empty screen is not a healthy one. The text names the action
# and never a URL: the device does not know the daemon's address for a
# browser.
EMPTY_TITLE = "No services set up"
EMPTY_HINT = "Open the admin page"
EMPTY_HINT_MORE = "on the daemon host"
EMPTY_ACCENT = "yellow"


def bar_layout(count):
    """The bar heights of a row and the gap between them.

    Every bar has the same thickness; the row is made tall enough for them.
    """
    from home import BAR_GAP, bar_height
    if count <= 0:
        return [], BAR_GAP
    return [bar_height(count)] * count, BAR_GAP


def bar_heights(count):
    """The height of each bar of a row."""
    return bar_layout(count)[0]


def dim_rgb(rgb, fade=DIM_FADE):
    """The colour faded toward grey: a stale window keeps a tint of its hue."""
    keep = 1.0 - fade
    return (int(rgb[0] * keep + INK_DIM[0] * fade),
            int(rgb[1] * keep + INK_DIM[1] * fade),
            int(rgb[2] * keep + INK_DIM[2] * fade))


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


def mute_chip_rect(display, text, width=240):
    """Where the mute chip is drawn: (x, y, w, h).

    One fixed slot for every screen: the bottom-right corner, below the
    STALE line and to the right of the page dots.
    """
    display.set_font(MUTE_CHIP_FONT)
    box_width = display.measure_text(text, 1) + 2 * MUTE_CHIP_PAD
    x = max(0, width - MARGIN - box_width)
    return (x, MUTE_CHIP_Y, box_width, MUTE_CHIP_H)


def _draw_mute_chip(display, palette, text, width=240):
    """The mute chip: a small box that says how long the mute has left."""
    if not text:
        return None
    x, y, box_width, box_height = mute_chip_rect(display, text, width)
    display.set_pen(palette.pen(PANEL))
    display.rectangle(x, y, box_width, box_height)
    display.set_font(MUTE_CHIP_FONT)
    # The firmware draws a line one pixel wider than it measures, and it
    # wraps a word that does not fit: the text needs its own width plus one.
    text_width = display.measure_text(text, 1)
    _text(display, palette, text, x + MUTE_CHIP_PAD, y + 2, INK, scale=1,
          max_width=text_width + 1)
    return (x, y, box_width, box_height)


def demo_chip_rect(display, width=240):
    """Where the demo chip is drawn: (x, y, w, h).

    The right end of the footer line. It is above the page dots (which start
    at height - 15), below the last list row (which ends at SOURCE_TOP +
    SOURCE_HEIGHT), and clear of the mute chip (which starts at y 218).
    """
    display.set_font(DEMO_CHIP_FONT)
    box_width = display.measure_text(DEMO_CHIP_TEXT, 1) + 2 * DEMO_CHIP_PAD + 1
    return (max(0, width - MARGIN - box_width), DEMO_CHIP_Y, box_width,
            DEMO_CHIP_H)


def _draw_demo_chip(display, palette, width=240, height=240):
    """Say, clearly, that these numbers are sample data, not an account."""
    x, y, box_width, box_height = demo_chip_rect(display, width)
    display.set_pen(palette.pen(DEMO_CHIP_BG))
    display.rectangle(x, y, box_width, box_height)
    display.set_font(DEMO_CHIP_FONT)
    _text(display, palette, DEMO_CHIP_TEXT, x + DEMO_CHIP_PAD, y + 2, INK,
          scale=1, max_width=box_width - 2 * DEMO_CHIP_PAD + 1)
    return (x, y, box_width, box_height)


def _footer_width(display, width, demo, x=MARGIN + 6):
    """The space a footer line keeps free so the demo chip has its own room."""
    if not demo:
        return None
    return max(0, demo_chip_rect(display, width)[0] - 8 - x)


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
    if slashed:
        for step in range(12):
            display.rectangle(x + 2 + step, y + step, 2, 2)
    if crossed:
        # A compact cross in the middle: the bell's outline still reads.
        for step in range(8):
            display.rectangle(x + 5 + step, y + 3 + step, 2, 2)
            display.rectangle(x + 13 - step, y + 3 + step, 2, 2)


def _draw_alert_mark(display, palette, x, y):
    """The small dim bell on a Home row whose service has its alerts off.

    A slash is cut out of it, so it reads as "alerts off" and not as a
    decoration. It is drawn on the row's background, never on a bar.
    """
    display.set_pen(palette.pen(INK_DIM))
    for dx, dy, w, h in ALERT_MARK_SHAPE:
        display.rectangle(x + dx, y + dy, w, h)
    display.set_pen(palette.pen(BACKGROUND))
    for dx, dy, w, h in ALERT_MARK_CUT:
        display.rectangle(x + dx, y + dy, w, h)


def _window_hue(window):
    """Cyan for a day or less, purple for longer, orange monthly; red at limit."""
    if (window["percent"] or 0) >= 100:
        return LIMIT
    period = window.get("period_minutes")
    if not period:
        return MONTHLY
    return DAILY if period <= 1440 else WEEKLY


def _window_rgb(window, dim):
    """The colour of a window's bar and label.

    A stale or dimmed window keeps a tint of its own hue, faded toward grey,
    so three bars of one service can still be told apart and stay readable.
    """
    rgb = _window_hue(window)
    if dim or window["state"] != "available":
        return dim_rgb(rgb)
    return rgb


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
    update = row.get("last_update")
    if hint and not update:
        return hint, INK_DIM
    binding = row.get("binding")
    if binding is None:
        if update and any(w.get("expired") for w in row["windows"]):
            return "awaiting refresh", INK_DIM
        return "", INK_DIM
    window = row["windows"][binding]
    if (stale or window["state"] == "stale") and not update:
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


def _update_note(row, stale):
    """Colour only the reading age; idle or disconnected readings stay grey."""
    from alerts import rgb_for_accent
    update = row.get("last_update")
    if not update:
        return "", INK_DIM
    rgb = INK_DIM
    if not stale and update["in_use"]:
        rgb = rgb_for_accent("yellow" if update["age_seconds"] > 15 * 60 else "green")
    return update["text"], rgb


def _draw_quota_row(display, palette, row, y, row_width, stale):
    """Name and reset; big labelled percentages; one stacked bar per window."""
    from home import bar_fill, home_windows, quota_label, window_tag
    right = 14 + row_width
    _marker(display, palette, row, 14, y, MARKER, stale)
    note, note_rgb = _freshness(row, stale)
    update, update_rgb = _update_note(row, stale)
    display.set_font("bitmap6")
    # Small secondary notes leave the account name readable at 240px.
    note_w = display.measure_text(note, 1) + 2 if note else 0
    update_w = display.measure_text(update, 1) + 8 if update else 0
    display.set_font("bitmap8")
    name_x = 14 + MARKER + 4
    space = max(0, row_width - MARKER - 4 - note_w - update_w - 8)
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
        display.set_font("bitmap6")
        _text(display, palette, note, right - note_w, y, note_rgb, scale=1)
    if update:
        display.set_font("bitmap6")
        _text(display, palette, update, right - note_w - update_w, y, update_rgb, scale=1)

    display.set_font("bitmap8")
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
    heights, gap = bar_layout(len(windows))
    for index, window in enumerate(windows):
        bar_h = heights[index]
        display.set_pen(palette.pen(TRACK))
        display.rectangle(14, bars_y, row_width, bar_h)
        # An expired window's bar stays empty; a stale one keeps its dim fill.
        filled = 0 if window.get("expired") else bar_fill(window["percent"], row_width)
        if filled:
            display.set_pen(palette.pen(_window_rgb(window, stale)))
            display.rectangle(14, bars_y, filled, bar_h)
        bars_y += bar_h + gap

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
                 layout=None, mute_text=None, demo=False):
    """Live Home: the accounts in the saved order, quota rows and local ones."""
    from alerts import rgb_for_accent
    from cards import accent_for
    from home import SOURCE_TOP
    display.set_pen(palette.pen(BACKGROUND))
    display.clear()
    # An empty list is not a healthy one: amber asks for a look, where the
    # status colour would call an unconfigured daemon "ok".
    accent_name = accent_for(status) if rows else EMPTY_ACCENT
    accent = rgb_for_accent(accent_name, 0.45 if muted else 1.0)
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
        display.set_font("bitmap8")
        _text(display, palette, EMPTY_TITLE, 14, SOURCE_TOP + 10, INK,
              scale=1, max_width=row_width)
        display.set_font("bitmap6")
        _text(display, palette, EMPTY_HINT, 14, SOURCE_TOP + 28, INK_DIM,
              scale=1, max_width=row_width)
        _text(display, palette, EMPTY_HINT_MORE, 14, SOURCE_TOP + 40, INK_DIM,
              scale=1, max_width=row_width)
    y = SOURCE_TOP - scroll
    list_bottom = SOURCE_TOP + region
    for row, row_height, compact in layout.rows:
        # The list is clipped at its edges, so a partly scrolled row is cut
        # there and never drawn over the strip or the footer. A row cut by the
        # bottom edge is not drawn at all while the strip is on: the strip names
        # it, and half a row of text above the strip reads as a mistake. A row
        # leaving at the top is still drawn, so scrolling stays smooth.
        cut_below = layout.lines > 0 and y + row_height > list_bottom
        if y + row_height > SOURCE_TOP and y < list_bottom and not cut_below:
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
        _text(display, palette, "STALE - retrying", 14, 210, INK_DIM, scale=1,
              max_width=_footer_width(display, width, demo, 14))
    if demo:
        _draw_demo_chip(display, palette, width, height)
    _draw_dots(display, palette, index, total, accent, width, height)
    if muted:
        _draw_mute_chip(display, palette, mute_text or "MUTED", width)


def _draw_chip(display, palette, text, right, y, background, foreground,
               scale=2, height=16):
    """A small labelled box, right-aligned: the status and the stale chip."""
    display.set_font("bitmap6")
    width = display.measure_text(text, scale)
    x = max(0, right - width - 6)
    display.set_pen(palette.pen(background))
    display.rectangle(x, y, width + 6, height)
    _text(display, palette, text, x + 3, y + (height - 6 * scale) // 2,
          foreground, scale=scale)
    return x


def _draw_marks(display, palette, marks, x, y, width, height):
    """Tick marks at the warning and critical percentages.

    The daemon sends the two numbers; the device draws them and decides
    nothing. Missing or junk marks mean no ticks at all: half a scale
    would be worse than none.
    """
    if not isinstance(marks, (list, tuple)) or len(marks) < 2:
        return
    warning, critical = marks[0], marks[1]
    for percent in (warning, critical):
        if type(percent) is not int or percent <= 0 or percent > 100:
            return
    for index, percent in enumerate((warning, critical)):
        position = x + round(width * percent / 100)
        if position > x + width - 1:
            position = x + width - 1
        display.set_pen(palette.pen(INK))
        display.rectangle(position, y, 2 if index else 1, height)


def _reset_note(window):
    """The reset countdown and the absolute time after it."""
    from home import reset_label, reset_stamp

    reset_in = window.get("reset_in")
    if reset_in is None:
        return "", ""
    if reset_in <= 0:
        return "reset passed", ""
    note = "resets in " + reset_label(reset_in)
    return note, reset_stamp(window.get("resets_at"))


def _draw_window_block(display, palette, window, x, y, width, stale, marks=None,
                       row_h=CARD_ROW_H):
    """One window of a quota card: its colour, its big number, its bar.

    The same parts a Home row draws, so a number looks the same in both
    places.
    """
    from home import bar_fill, quota_label, window_tag

    tag = window_tag(window)
    value = quota_label(None if window.get("expired") else window["percent"])
    rgb = _window_rgb(window, stale)
    display.set_font("bitmap8")
    _text(display, palette, tag, x, y + 8, INK_DIM, scale=1)
    tag_w = display.measure_text(tag, 1) + 3
    _text(display, palette, value, x + tag_w, y, rgb, scale=2)

    bar_y = y + 17
    bar_h = 5 if row_h >= CARD_ROW_H else 4
    display.set_pen(palette.pen(TRACK))
    display.rectangle(x, bar_y, width, bar_h)
    filled = 0 if window.get("expired") else bar_fill(window["percent"], width)
    if filled:
        display.set_pen(palette.pen(rgb))
        display.rectangle(x, bar_y, filled, bar_h)
    _draw_marks(display, palette, marks, x, bar_y, width, bar_h)

    relative, absolute = _reset_note(window)
    if relative:
        display.set_font("bitmap6")
        _text(display, palette, relative, x, bar_y + bar_h + 1, INK_DIM, scale=1)
    if absolute:
        display.set_font("bitmap6")
        _text(display, palette, absolute,
              x + display.measure_text(relative, 1) + 4, bar_y + bar_h + 1,
              INK_FAINT, scale=1)


def _missing_window(windows):
    """The dim line for a window the provider does not report.

    None when every window has a reading: there is then nothing to fold.
    """
    for window in windows:
        if window.get("percent") is None:
            label = str(window.get("label") or "").strip()
            head = label.split(" ")[0].lower() if label else "secondary"
            return "no %s window" % head
    return None


def _filter_reset_lines(lines, windows):
    """Drop the per-window reset lines: the quota rows show them better."""
    heads = []
    for window in windows:
        reset_in = window.get("reset_in")
        if reset_in is None or reset_in <= 0:
            continue
        label = str(window.get("label") or "").strip()
        if label:
            heads.append(label.split(" ")[0])
    kept = []
    for line in lines:
        if any(line.startswith(head + " reset: ") for head in heads):
            continue
        kept.append(line)
    return kept


def _tone_rgb(tone):
    """The colour of a tile's value, from the palette the card already uses."""
    from alerts import rgb_for_accent

    if tone == "good":
        return rgb_for_accent("green")
    if tone == "warn":
        return rgb_for_accent("yellow")
    if tone == "bad":
        return rgb_for_accent("red")
    if tone == "dim":
        return INK_DIM
    return INK


def _draw_tiles(display, palette, tiles, x, y, width):
    """The tiles as a two-column grid: a dim label over a bright value."""
    if not tiles:
        return y
    column = width // 2
    for index, item in enumerate(tiles):
        cell_x = x + (index % 2) * column
        cell_y = y + (index // 2) * TILE_H
        label = item.get("label") or ""
        value = item.get("value") or ""
        tone = item.get("tone") or "normal"
        display.set_font("bitmap8")
        _text(display, palette, label, cell_x, cell_y, INK_DIM, scale=1,
              max_width=column - 4)
        _text(display, palette, value, cell_x, cell_y + 9, _tone_rgb(tone),
              scale=2, max_width=column - 4)
    rows = (len(tiles) + 1) // 2
    return y + rows * TILE_H


def draw_card(display, palette, card, index, total, muted=False,
              width=240, height=240, stale=False, alerts=None, message=None,
              quota=None, marks=None, detail=None, tiles=None, loading=False,
              failed=False, detail_open=False, mute_text=None, demo=False):
    """Draw one card.

    ``index``/``total`` drive the page dots at the bottom. ``muted`` dims the
    accent so the device visibly acknowledges a long-press. ``alerts`` is
    the service's alert state, and draws the bell in the top-right corner.
    ``message`` is a short line above the dots, used when a write fails.

    ``quota`` is this card's structured quota: the windows the daemon sent
    for this service. They are drawn like Home draws them, so a card and a
    row show the same numbers the same way. A card without that data falls
    back to the text layout. ``marks`` are the warning and critical
    percentages, drawn as ticks on the bars.

    ``detail`` is the lazily fetched detail: its tiles and its lines.
    ``tiles`` draws tiles the payload carried with the card. ``loading``
    and ``failed`` say what the lazy fetch is doing.
    """
    from alerts import rgb_for_accent
    from cards import is_unavailable_line

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

    # The status chip, on the right of the card, and the stale chip beside
    # it: staleness is said once, not on every line.
    chip_right = width - MARGIN
    if card.stale or stale:
        chip_right = _draw_chip(display, palette, "STALE", chip_right,
                                STATUS_Y, TRACK, INK_DIM, scale=1) - 4
    _draw_chip(display, palette, card.status.upper(), chip_right, STATUS_Y,
               TRACK, accent_rgb, scale=2)

    windows = quota if isinstance(quota, list) else []
    lines = detail if isinstance(detail, list) else card.detail
    secondary_missing = is_unavailable_line(card.secondary)
    if windows:
        # The provider does not report this window: the lines that say so
        # are folded into one dim line below.
        lines = [line for line in lines if not is_unavailable_line(line)]
    missing = _missing_window(windows) if windows else None
    if missing is None and secondary_missing:
        missing = "no secondary window"
    y = CARD_ROWS_TOP
    measured = [window for window in windows
                if window.get("percent") is not None and not window.get("expired")]
    if measured:
        # A quota card: the windows are drawn like Home draws them.
        row_h = CARD_ROW_H if len(windows) <= 2 else CARD_ROW_H_TIGHT
        for window in measured:
            _draw_window_block(display, palette, window, MARGIN + 6, y,
                               width - 2 * MARGIN - 6, stale, marks, row_h)
            y += row_h
        lines = _filter_reset_lines(lines, windows)
    else:
        # A text card: the big number, the dim line under it, then the lines.
        _text(display, palette, card.primary, MARGIN + 6, y, INK,
              scale=3, max_chars=PRIMARY_MAX, min_scale=2)
        y += 28
        if card.secondary and not secondary_missing:
            display.set_font("bitmap6")
            _text(display, palette, card.secondary, MARGIN + 6, y, INK_DIM,
                  scale=2, max_chars=SECONDARY_MAX)
            y += 16
    if missing:
        display.set_font("bitmap6")
        _text(display, palette, missing, MARGIN + 6, y, INK_DIM, scale=1)
        y += 10

    # The tiles of the card, when it has any. The detail view shows the
    # lines instead: the tiles and the lines never fight for the same room.
    card_tiles = tiles if isinstance(tiles, list) else card.tiles
    if not isinstance(card_tiles, list):
        card_tiles = []
    if detail_open:
        card_tiles = []
    if card_tiles:
        y += 4
        y = _draw_tiles(display, palette, card_tiles, MARGIN + 6, y,
                        width - 2 * MARGIN - 6)
    elif loading:
        # A quiet placeholder: the detail is on its way.
        display.set_font("bitmap6")
        _text(display, palette, "loading detail", MARGIN + 6, y, INK_FAINT,
              scale=1)
        y += 12
    elif failed and not lines:
        # The detail could not be fetched and the card has no lines of its
        # own: the summary stays, and says so.
        display.set_font("bitmap6")
        _text(display, palette, "detail unavailable", MARGIN + 6, y, INK_FAINT,
              scale=1)
        y += 12

    # Detail lines
    room = max(0, (CARD_BOTTOM - y) // CARD_DETAIL_H)
    count = min(CARD_DETAIL_LINES, room)
    display.set_font("bitmap6")
    for line in lines[:count]:
        _text(display, palette, line, MARGIN + 6, y, INK_DIM,
              scale=1, max_chars=DETAIL_MAX)
        y += CARD_DETAIL_H
    if len(lines) > count:
        _text(display, palette, "+%d more (tap)" % (len(lines) - count),
              MARGIN + 6, y, INK_FAINT, scale=1, max_chars=DETAIL_MAX)

    # Page dots
    footer_width = _footer_width(display, width, demo)
    if message:
        # A short line above the dots: what went wrong, or what to do.
        display.set_font("bitmap6")
        _text(display, palette, message, MARGIN + 6, 208, INK, scale=1,
              max_chars=DETAIL_MAX, max_width=footer_width)
    elif stale:
        display.set_font("bitmap6")
        _text(display, palette, "STALE - retrying", MARGIN + 6, 208, INK_DIM,
              scale=1, max_width=footer_width)
    if demo:
        _draw_demo_chip(display, palette, width, height)
    _draw_dots(display, palette, index, total, accent_rgb, width, height)

    # Mute indicator: the chip says how long the mute has left.
    if muted:
        _draw_mute_chip(display, palette, mute_text or "MUTED", width)


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
                 detail=None, demo=False):
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
            room = None
            if demo and y < DEMO_CHIP_Y + DEMO_CHIP_H and y + 6 > DEMO_CHIP_Y:
                chip_x = demo_chip_rect(display)[0]
                room = max(0, chip_x - MARGIN - 10)
            _text(display, palette, line, MARGIN + 6, y, INK_DIM, scale=1,
                  max_chars=DETAIL_MAX, max_width=room)
            y += DETAIL_LINE_H
    if demo:
        _draw_demo_chip(display, palette)


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
