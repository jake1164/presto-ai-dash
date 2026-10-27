"""Live source/quota rows and bounded Home scrolling.

Home lists the accounts in the saved order. When the list is taller than the
list area, the rows that are not fully visible are named in a strip at the
bottom of that area, each with its status marker. The strip's height is a
property of the layout, never of the scroll position, so the list does not
jump while the user scrolls.
"""

from gestures import DRAG_SLOP_PX

LIST_TOP = 56
LIST_HEIGHT = 150

# Live Home: the accounts in the order they are saved. A quota account is two
# lines; a local service among quota rows is one compact line.
SOURCE_TOP = 4
SOURCE_HEIGHT = 204
SOURCE_ROW = 46
LOCAL_ROW = 18

# A quota row is its name and numbers, then one bar for each window. Every bar
# is the same thickness whatever the service, so one, two or three windows read
# alike, and the row is as tall as its bars need. With more than four windows
# the bars thin out, so a service with many buckets cannot fill the list.
ROW_TOP_H = 30
ROW_GAP = 4
BAR_H = 6
BAR_H_MANY = 3
BAR_GAP = 1
MANY_BARS = 4
# A row with nothing measured says so in one line of text where its bars would be.
MIN_BARS_AREA = 10


def bar_height(count):
    """The thickness of every bar of a row with this many windows."""
    return BAR_H if count <= MANY_BARS else BAR_H_MANY


def bars_area(count):
    """The height of the bars of a row, with the gaps between them."""
    if count <= 0:
        return 0
    return count * bar_height(count) + (count - 1) * BAR_GAP


def quota_row_height(row):
    """The height of one quota row on Home: its numbers, its bars, and the gap below."""
    windows = home_windows(row)
    measured = [w for w in windows if w["percent"] is not None]
    area = bars_area(len(windows)) if measured else MIN_BARS_AREA
    return ROW_TOP_H + area + ROW_GAP

# The overflow strip: the names of the rows that do not fit in the list.
LIST_X = 14
STRIP_LINE_HEIGHT = 18
STRIP_MAX_LINES = 3
DEFAULT_OVERFLOW_ROWS = 1
STRIP_MARKER = 6
STRIP_GAP = 4
STRIP_SEPARATOR = 8
STRIP_SLACK = 2
STRIP_MIN_CHARS = 4


def scroll_limit(total_height, height=LIST_HEIGHT):
    """How far a list of this total height scrolls inside the region."""
    return max(0, total_height - height)


def home_layout(rows):
    """The Home list as (row, height, compact) triples, in drawing order.

    A quota account keeps its two lines. A local service is a compact
    one-line row at its own position, unless no quota account is on the
    list: then the local services are the list and keep their two lines.
    """
    compact = False
    for row in rows:
        if "windows" in row:
            compact = True
            break
    layout = []
    for row in rows:
        if "windows" in row:
            layout.append((row, quota_row_height(row), False))
        elif compact:
            layout.append((row, LOCAL_ROW, True))
        else:
            layout.append((row, SOURCE_ROW, False))
    return layout


def home_total_height(rows):
    """The height of the whole list: scrolling uses this, not a row count."""
    total = 0
    for entry in home_layout(rows):
        total += entry[1]
    return total


def overflow_rows(payload):
    """The maximum number of overflow-strip lines, from the payload.

    The daemon sends the setting under "display". Anything the device does
    not trust - a missing key, a null, a non-dict, a boolean, a float, a
    number outside 0..3 - falls back to one line, so a broken setting can
    never blank or crowd the Home list.
    """
    display = payload.get("display") if isinstance(payload, dict) else None
    if not isinstance(display, dict):
        return DEFAULT_OVERFLOW_ROWS
    value = display.get("overflow_rows")
    if type(value) is not int or value < 0 or value > STRIP_MAX_LINES:
        return DEFAULT_OVERFLOW_ROWS
    return value


# The alert settings the daemon sends, and the device's own defaults for
# anything the payload does not say. The sound is off by default: a desk
# device must not chirp unless the user asked for it.
DEFAULT_ALERT_SETTINGS = {"quiet": False, "sound": False, "pop_to_top": True,
                           "enabled": True, "mute_hours": 4}


def alert_settings(payload):
    """The daemon's alert settings, with anything untrusted replaced.

    A missing key, a payload that is not an object, a value that is not a
    real boolean: the device falls back to its own defaults - sound off,
    quiet off, pop-to-top on, alerts on - so a broken payload can never
    make the device chirp, and can never silence it in a way the user did
    not ask for.

    The mute length is a number of hours. A value that is not a whole
    number in 0..48 is not used: the device keeps its own default of four
    hours, so a broken setting can never mute the sound for ever.
    """
    result = dict(DEFAULT_ALERT_SETTINGS)
    alerts = payload.get("alerts") if isinstance(payload, dict) else None
    if not isinstance(alerts, dict):
        return result
    quiet = alerts.get("quiet")
    sound = alerts.get("sound")
    pop_to_top = alerts.get("pop_to_top")
    enabled = alerts.get("enabled")
    mute_hours = alerts.get("mute_hours")
    if type(quiet) is bool:
        result["quiet"] = quiet
    if type(sound) is bool:
        result["sound"] = sound
    if type(pop_to_top) is bool:
        result["pop_to_top"] = pop_to_top
    if type(enabled) is bool:
        result["enabled"] = enabled
    if type(mute_hours) is int and not isinstance(mute_hours, bool) \
            and 0 <= mute_hours <= 48:
        result["mute_hours"] = mute_hours
    return result


def alerts_flag(source):
    """Whether a service alerts at all.

    The daemon says so with a boolean. A missing value, or one that is not a
    real boolean, counts as alerting: an older daemon knows nothing about
    this switch, and a service must not go quiet because the payload was
    odd.
    """
    if not isinstance(source, dict):
        return True
    return source.get("alerts", True) is not False


def alerts_state(source):
    """The service's alerts state: True, False, or None when unknown.

    None means the daemon did not say anything about this service, so the
    device shows no bell: there is nothing to switch.
    """
    if not isinstance(source, dict) or "alerts" not in source:
        return None
    value = source.get("alerts")
    if type(value) is bool:
        return value
    return True


def strip_needed(total_height, setting):
    """Does a list this tall need the overflow strip?

    The strip appears only when the list is taller than the list area and
    the setting allows it; a list that fits needs no band at all.
    """
    return bool(setting) and total_height > SOURCE_HEIGHT


def _fit_name(name, measure, space):
    """The longest form of this name that fits in ``space`` pixels.

    Returns None when even four characters do not fit: such a name cannot be
    shown at all, and the strip says how many rows are hidden instead.
    """
    from cards import truncate

    text = str(name)
    if measure(text) <= space:
        return text
    for length in range(len(text) - 1, STRIP_MIN_CHARS - 1, -1):
        candidate = truncate(text, length)
        if measure(candidate) <= space:
            return candidate
    return None


def _pack(entries, measure, limit, max_lines, reserve=0):
    """Pack the hidden names into strip lines, in order, without splitting one.

    ``entries`` are (row, name) pairs. Returns (lines, leftover, unfit):
    ``lines`` is a list of lines, each a list of (row, x, text) with x the
    text's left edge; ``leftover`` is the entries that did not fit in
    ``max_lines`` lines; ``unfit`` is the entries that do not fit on a line
    of their own. ``reserve`` is the width kept free at the end of the last
    line for the "+N" entry.
    """
    lines = []
    current = []
    x = LIST_X
    leftover = []
    unfit = []
    for row, name in entries:
        index = len(lines)
        if max_lines is not None and index >= max_lines:
            leftover.append((row, name))
            continue
        edge = limit
        if reserve and max_lines is not None and index == max_lines - 1:
            edge = limit - reserve
        text = _fit_name(name, measure, edge - x - STRIP_MARKER - STRIP_GAP)
        if text is None:
            if not current:
                unfit.append((row, name))
                continue
            lines.append(current)
            current = []
            x = LIST_X
            index = len(lines)
            if max_lines is not None and index >= max_lines:
                leftover.append((row, name))
                continue
            edge = limit
            if reserve and max_lines is not None and index == max_lines - 1:
                edge = limit - reserve
            text = _fit_name(name, measure, edge - x - STRIP_MARKER - STRIP_GAP)
            if text is None:
                unfit.append((row, name))
                continue
        current.append((row, x + STRIP_MARKER + STRIP_GAP, text))
        x += STRIP_MARKER + STRIP_GAP + measure(text) + STRIP_SEPARATOR
    if current:
        lines.append(current)
    return lines, leftover, unfit


class HomeLayout(object):
    """The Home list region, and the strip that names the rows that do not fit.

    The strip's line count L is a property of the layout - the rows, their
    names, the setting and the width - and never of the scroll offset: a band
    that changed height while the user scrolled would move the viewport under
    the finger.

    Finding L: for L = 1..setting, take the region (204 - 18 * L pixels),
    walk the scroll offsets where the set of fully visible rows changes (two
    per row at most, so O(rows) offsets instead of O(pixels)), and measure the
    names of the rows that are not fully visible. The first L whose names fit
    in L lines at every offset wins. If no L up to the setting is enough, L is
    the setting and the last slot of the last line shows "+N".

    Cost: O(setting * rows^2) text measurements - a few thousand for thirty
    accounts - once per payload, not once per frame.
    """

    def __init__(self, rows, setting=DEFAULT_OVERFLOW_ROWS, measure=None, width=0):
        self.rows = home_layout(rows)
        self.total = 0
        for entry in self.rows:
            self.total += entry[1]
        self.measure = measure
        self.width = width
        if type(setting) is not int:
            setting = DEFAULT_OVERFLOW_ROWS
        self.setting = max(0, min(STRIP_MAX_LINES, setting))
        self.lines = 0
        self.region = SOURCE_HEIGHT
        if strip_needed(self.total, self.setting):
            for lines in range(1, self.setting + 1):
                if self._names_fit(lines):
                    self.lines = lines
                    break
            else:
                self.lines = self.setting
        if self.lines:
            self.region = SOURCE_HEIGHT - STRIP_LINE_HEIGHT * self.lines
        self.limit = scroll_limit(self.total, self.region)

    def extra_lines(self, offset):
        """How many lines the hidden names would need at this offset."""
        hidden = self.hidden(offset)
        if not hidden:
            return 0
        entries = [(row, row.get("name", "")) for row in hidden]
        packed, leftover, unfit = _pack(entries, self.measure, self.strip_limit(),
                                       self.setting)
        return max(0, len(packed) + (1 if (leftover or unfit) else 0) - self.lines)

    def strip_limit(self):
        """The x the strip may not cross: the row width, less the draw slack."""
        return LIST_X + self.width - STRIP_SLACK

    def _offsets(self, region):
        """The scroll offsets where the set of fully visible rows changes."""
        limit = scroll_limit(self.total, region)
        offsets = [0, limit]
        top = 0
        for entry in self.rows:
            offsets.append(top)
            offsets.append(top + entry[1] - region)
            top += entry[1]
        offsets.sort()
        return [offset for offset in offsets if 0 <= offset <= limit]

    def hidden(self, offset, region=None):
        """The rows that are not fully visible at this scroll offset.

        A row cut by either edge of the region counts as not fully visible,
        so it is named in the strip. One cut by the bottom edge is not drawn
        (the strip stands in for it); one leaving at the top is drawn as it
        scrolls out.
        """
        if region is None:
            region = self.region
        hidden = []
        top = 0
        for entry in self.rows:
            if top < offset or top + entry[1] > offset + region:
                hidden.append(entry[0])
            top += entry[1]
        return hidden

    def _names_fit(self, lines):
        """Do the hidden names fit in ``lines`` lines at every scroll offset?"""
        region = SOURCE_HEIGHT - STRIP_LINE_HEIGHT * lines
        previous = None
        for offset in self._offsets(region):
            if offset == previous:
                continue
            previous = offset
            hidden = self.hidden(offset, region)
            if not hidden:
                continue
            entries = [(row, row.get("name", "")) for row in hidden]
            packed, leftover, unfit = _pack(entries, self.measure,
                                            self.strip_limit(), lines)
            if leftover or unfit:
                return False
        return True

    def _worst(self, rows):
        """The most severe status among these rows."""
        from cards import severity

        worst = None
        for row in rows:
            status = row.get("status")
            if worst is None or severity(status) > severity(worst):
                worst = status
        return worst

    def strip(self, offset):
        """The strip to draw at this scroll offset: (lines, plus).

        ``lines`` is a list of lines, each a list of (row, x, text); ``plus``
        is (text, status, x) for the "+N" entry, or None. A drawn strip is
        never empty: the list is taller than the region, so at least one row
        is never fully visible.
        """
        hidden = self.hidden(offset)
        if not hidden or not self.lines:
            return [], None
        entries = [(row, row.get("name", "")) for row in hidden]
        limit = self.strip_limit()
        lines, leftover, unfit = _pack(entries, self.measure, limit, self.lines)
        if unfit:
            # A name that cannot fit even on a line of its own: the strip
            # says how many rows are hidden instead of naming them.
            return [], ("+%d" % len(hidden), self._worst(hidden), LIST_X)
        if not leftover:
            return lines, None
        count = len(leftover)
        text = "+%d" % count
        for _ in range(4):
            reserve = self.measure(text) + 1 + STRIP_SEPARATOR
            lines, leftover, unfit = _pack(entries, self.measure, limit,
                                           self.lines, reserve)
            if unfit:
                return [], ("+%d" % len(hidden), self._worst(hidden), LIST_X)
            if len(leftover) == count:
                break
            count = len(leftover)
            text = "+%d" % count
        # The count is drawn one pixel inside the line: the firmware measures
        # a line one pixel narrower than it draws it.
        return lines, (text, self._worst([row for row, _name in leftover]),
                       limit - self.measure(text) - 1)


def iso_seconds(text):
    """Seconds since 1970 for an ISO-8601 time with an offset, else None.

    MicroPython has no datetime and its epoch differs by port, so count days
    directly. Only differences between two results are meaningful here.
    """
    try:
        text = str(text)
        year, month, day = int(text[0:4]), int(text[5:7]), int(text[8:10])
        hour, minute, second = int(text[11:13]), int(text[14:16]), int(text[17:19])
        tail = text[19:]
        if tail.startswith("."):
            tail = tail[1:]
            while tail and tail[0] in "0123456789":
                tail = tail[1:]
        offset = 0
        if tail and tail not in ("Z", "z"):
            sign = -1 if tail[0] == "-" else 1
            offset = sign * (int(tail[1:3]) * 3600 + int(tail[4:6]) * 60)
    except (ValueError, IndexError, TypeError):
        return None
    year -= 1 if month <= 2 else 0
    era = year // 400
    yoe = year - era * 400
    doy = (153 * (month + (-3 if month > 2 else 9)) + 2) // 5 + day - 1
    days = era * 146097 + yoe * 365 + yoe // 4 - yoe // 100 + doy - 719468
    return days * 86400 + hour * 3600 + minute * 60 + second - offset


def reset_label(seconds):
    """Compact duration: 45m, 2h27m, 4d19h, 12d. Past or unknown is blank."""
    if seconds is None or seconds <= 0:
        return ""
    minutes = (seconds + 59) // 60
    if minutes < 60:
        return "%dm" % minutes
    hours = minutes // 60
    if hours < 24:
        if minutes % 60 == 0:
            return "%dh" % hours
        return "%dh%02dm" % (hours, minutes % 60)
    days = hours // 24
    remainder = hours % 24
    if days >= 10 or remainder == 0:
        return "%dd" % days
    return "%dd%dh" % (days, remainder)


def compact_left(seconds):
    """The time left in short units: 45m, 5h, 2d."""
    if seconds is None:
        return ""
    seconds = max(0, int(seconds))
    minutes = seconds // 60
    if minutes < 60:
        return "%dm" % minutes
    hours = seconds // 3600
    if hours < 48:
        return "%dh" % hours
    return "%dd" % (seconds // 86400)


# A banked reset that expires within this many hours is worth a warning.
RESET_WARNING_SECONDS = 72 * 3600


def reset_badge(reset_credits, now):
    """A small badge for banked resets: "R3", amber with the time left.

    Nothing is drawn when the count is unknown or zero: an unknown count is
    never shown as a count of zero, and "R0" is never drawn.
    """
    if not isinstance(reset_credits, dict):
        return None
    available = reset_credits.get("available")
    if not isinstance(available, int) or isinstance(available, bool) or available <= 0:
        return None
    text = "R%d" % available
    expiries = reset_credits.get("expires_at")
    if not isinstance(expiries, list) or now is None:
        return {"text": text, "amber": False}
    moments = []
    for value in expiries:
        moment = iso_seconds(value)
        if moment is not None:
            moments.append(moment)
    if not moments:
        return {"text": text, "amber": False}
    left = min(moments) - now
    if left > RESET_WARNING_SECONDS:
        return {"text": text, "amber": False}
    return {"text": "%s %s" % (text, compact_left(left)), "amber": True}


def binding_window(windows):
    """The window most likely to stop work: highest use, then longest period."""
    best = None
    for index, window in enumerate(windows):
        if (window.get("expired") or window["percent"] is None or
                window["state"] not in ("available", "stale")):
            continue
        period = window.get("period_minutes")
        key = (window["percent"], period if isinstance(period, int) else 10 ** 9)
        if best is None or key > best[0]:
            best = (key, index)
    return None if best is None else best[1]


def reset_stamp(text):
    """The absolute reset time as it is shown: 10-25 12:00Z.

    The device has no time zone, so the time stays in UTC, as the rest of
    the card does.
    """
    text = str(text or "")
    if len(text) < 16 or text[4] != "-" or text[10] not in ("T", " "):
        return ""
    return "%s-%s %s:%sZ" % (text[5:7], text[8:10], text[11:13], text[14:16])


def window_tag(window):
    """Short period name so a value never depends on colour alone."""
    period = window.get("period_minutes")
    label = window["label"]
    if period == 10080:
        return "Wk"
    if isinstance(period, int) and period > 0 and period % 60 == 0 and period <= 1440:
        return "%dh" % (period // 60)
    if label.lower().startswith("month"):
        return "Mo"
    return label if len(label) <= 3 else label[:2]


def home_windows(row):
    """Windows worth a slot on Home: measured, or a known period that is missing.

    An unidentified empty slot (Codex's null "Secondary") stays on the detail
    card; a row with nothing else keeps its first window so it reads unknown.
    """
    windows = row.get("windows") or []
    shown = [w for w in windows if w["percent"] is not None or w.get("period_minutes")]
    return shown or windows[:1]


# A reading this old or younger is current; an older one is called stale.
FRESH_UPDATE_SECONDS = 15 * 60


def update_note(value):
    """A bounded quota-reading age, separate from the quota's reset time.

    "Updated 4m" while it is current, then "stale 42m", "stale 9h" or
    "stale 3d": the age is the whole message, in its largest whole unit.
    """
    if not isinstance(value, dict):
        return None
    age = value.get("age_seconds")
    if not _number(age) or age < 0 or age > 315360000:
        return None
    minutes = int(age) // 60
    if age <= FRESH_UPDATE_SECONDS:
        text = "Updated %dm" % minutes
    elif minutes < 60:
        text = "stale %dm" % minutes
    elif minutes < 48 * 60:
        text = "stale %dh" % (minutes // 60)
    else:
        text = "stale %dd" % (minutes // 1440)
    return {"text": text, "age_seconds": age, "in_use": value.get("in_use") is True}


def source_rows(payload):
    """Reusable per-instance summaries; absent usage is never a zero or quota."""
    from cards import clean_id, normalise_status
    sources = payload.get("sources")
    if not isinstance(sources, list):
        return None  # Older daemons still render their ordinary summary card.
    # Managed accounts follow the saved order on Home and in the carousel.
    if payload.get("card_order") != "accounts":
        # Quota accounts first, each group in its own order. Partition instead
        # of sorting: MicroPython's sort is not stable (see cards.parse_cards).
        quota = [source for source in sources if isinstance(source, dict) and
                 isinstance(source.get("quota"), dict)]
        other = [source for source in sources if not (isinstance(source, dict) and
                 isinstance(source.get("quota"), dict))]
        sources = quota + other
    rows = []
    # Daemon time, so resets read right on a device without a synced clock.
    now = iso_seconds(payload.get("updated_at"))
    for source in sources:
        if not isinstance(source, dict):
            continue
        quota = source.get("quota")
        if isinstance(quota, dict) and isinstance(quota.get("windows"), list):
            windows = []
            for window in quota["windows"]:
                if not isinstance(window, dict):
                    continue
                state = window.get("state", "unavailable")
                percent = quota_value(window.get("used_percent"))
                label = str(window.get("label") or "Allowance")
                resets = iso_seconds(window.get("resets_at"))
                observed = iso_seconds(window.get("observed_at"))
                reset_in = None if resets is None or now is None else resets - now
                period = window.get("period_minutes")
                windows.append({"label": label, "percent": percent, "state": state,
                                "period_minutes": period if _number(period) else None,
                                "resets_at": window.get("resets_at"),
                                "reset_in": reset_in,
                                "age": None if observed is None or now is None else now - observed,
                                "expired": reset_in is not None and reset_in <= 0})
            activity = quota.get("activity")
            text = ""
            if isinstance(activity, dict):
                requests, cost = activity.get("request_count"), activity.get("usage_usd")
                amount = (("%.5f" % cost).rstrip("0").rstrip(".")
                          if _number(cost) and 0 < cost < 0.01 else "%.2f" % cost if _number(cost) else "")
                text = "%s req" % requests if quota_value(requests) is not None else "Activity unavailable"
                if text != "Activity unavailable" and quota_value(cost) is not None:
                    text += " / $" + amount
                start, until = iso_seconds(activity.get("from")), iso_seconds(activity.get("until"))
                if text[0] != "A" and start is not None and until is not None and until > start:
                    period = activity.get("range")
                    text += (" / " + period if period in ("24h", "7d", "30d") else
                             " in %dd" % ((until - start + 43200) // 86400))
                if activity.get("state") == "stale":
                    text = "Stale: " + text
            hint = quota.get("hint")
            if not isinstance(hint, str):  # a junk hint must never reach the screen
                hint = None
            marks = source.get("marks")
            if not isinstance(marks, list):
                marks = None
            if windows or text:
                rows.append({"id": clean_id(source.get("id")), "type": source.get("type"),
                             "name": str(source.get("name") or "Cloud account"),
                             "status": normalise_status(source.get("status")), "usage": text,
                             "windows": windows, "activity": text, "hint": hint,
                             "last_update": update_note(quota.get("last_update")),
                             "reset_badge": reset_badge(quota.get("reset_credits"), now),
                             "binding": binding_window(windows),
                             "marks": marks,
                             "alerts": alerts_state(source)})
                continue
        metrics = source.get("metrics")
        metrics = metrics if isinstance(metrics, dict) else {}
        usage = []
        for key, label in (("requests_today", "req"), ("tokens_today", "tok")):
            value = metrics.get(key)
            if _number(value):
                usage.append("%s %s" % (compact_count(value) if key == "tokens_today" else value,
                                        label))
        text = " / ".join(usage) if usage else "Usage unavailable"
        if not usage and metrics.get("loaded_model"):
            text = str(metrics["loaded_model"])
        rows.append({"id": clean_id(source.get("id")), "type": source.get("type"),
                     "name": str(source.get("name") or source.get("type") or "Source"),
                     "status": normalise_status(source.get("status")), "usage": text,
                     "parts": local_parts(metrics, usage),
                     "note": clean_note(metrics.get("usage_note")),
                     "alerts": alerts_state(source)})
    return rows


_INFINITY = float("inf")


def _number(value):
    """A real, finite number. A bool, nan and inf are not: %d overflows on them."""
    return (isinstance(value, (int, float)) and not isinstance(value, bool) and
            value == value and value != _INFINITY and value != -_INFINITY)


def compact_count(value):
    """A count in few characters: 940, 10.9k, 124k, 1.2M."""
    value = int(value)
    if value < 1000:
        return "%d" % value
    if value < 100000:
        return "%.1fk" % (value / 1000.0)
    if value < 1000000:
        return "%dk" % (value // 1000)
    return "%.1fM" % (value / 1000000.0)


def clean_note(value):
    """A short reason from the daemon, or None. Plain text only."""
    if not isinstance(value, str) or not value or len(value) > 24:
        return None
    return value


def local_parts(metrics, usage):
    """What a one-line local row can say, most useful first.

    The row shows as many leading parts as fit beside the name. ``usage`` is
    the request and token counts already formatted. Only numbers and the
    loaded model's name are used, so an "unavailable" marker never shows.
    """
    parts = []
    model = metrics.get("loaded_model")
    if isinstance(model, str) and model:
        if model.endswith(":latest"):
            model = model[:-7]  # the tag every model has says nothing
        parts.append(model[:16])
    gpu = metrics.get("gpu_percent")
    if _number(gpu) and 0 <= gpu <= 100:
        parts.append("%d%% GPU" % gpu)
    parts.extend(usage)
    spend = metrics.get("spend_today")
    if _number(spend) and spend > 0:
        parts.append("$%.2f" % spend)
    errors = metrics.get("errors_today")
    if _number(errors) and errors > 0:
        parts.append("%d err" % errors)
    speed = metrics.get("last_measured_tokens_per_sec")
    if _number(speed) and speed > 0:
        parts.append("%.1f t/s" % speed)
    models = metrics.get("available_models")
    if not parts and _number(models) and models > 0:
        parts.append("%d models" % models)
    return parts


def row_at(layout, scroll, y):
    """The Home row under a touch at screen height ``y``, or None.

    Only the list region answers; the strip and the footer are not rows. The
    rows are walked with the same heights and scroll the drawing uses.
    """
    if y < SOURCE_TOP or y >= SOURCE_TOP + layout.region:
        return None
    top = SOURCE_TOP - scroll
    for row, height, _compact in layout.rows:
        if top <= y < top + height:
            return row
        top += height
    return None


class HomeScroll(object):
    """Finger-following vertical scroll; horizontal drags belong to cards.

    ``height`` is the touch area, which covers the whole list area; ``view``
    is the visible part of it, which the overflow strip takes away. The
    scroll limit follows the view, so the list scrolls behind the strip.
    """

    def __init__(self, total_height=0, top=LIST_TOP, height=LIST_HEIGHT, view=None):
        self.top, self.height = top, height
        self.view = height if view is None else view
        self.limit = scroll_limit(total_height, self.view)
        self.offset = 0
        self._down = False
        self._axis = None

    def set_height(self, total_height, top=None, height=None, view=None):
        if top is not None:
            self.top, self.height = top, height
            if view is None:
                view = height
        if view is not None:
            self.view = view
        self.limit = scroll_limit(total_height, self.view)
        self.offset = min(self.offset, self.limit)

    def update(self, touched, x, y, enabled=True):
        if not touched or not enabled:
            self._down = False
            return
        if not self._down:
            self._down = True
            self._x, self._y = x, y
            self._origin = self.offset
            self._axis = None if self.top <= y < self.top + self.height else "ignore"
            return
        dx, dy = x - self._x, y - self._y
        if self._axis is None and max(abs(dx), abs(dy)) >= DRAG_SLOP_PX:
            self._axis = "vertical" if abs(dy) > abs(dx) else "horizontal"
        if self._axis == "vertical":
            self.offset = max(0, min(self.limit, self._origin - dy))


def quota_value(value):
    """Unknown values stay unknown; the preview must not turn them into zero."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value < 0 or value != value or value == float("inf"):
        return None
    return value


def quota_label(value):
    value = quota_value(value)
    return "--" if value is None else "%d%%" % round(value)


def quota_width(value, width):
    """Used fraction of this period's own quota, clipped to the track width."""
    value = quota_value(value)
    if value is None:
        return 0
    return round(width * min(value, 100) / 100)


# A measured value above zero fills at least this many pixels: a 3% reading
# on a narrow bar is otherwise a couple of pixels and reads as empty.
MIN_BAR_FILL = 3


def bar_fill(value, width):
    """The filled part of a bar: at least three pixels for any reading.

    Zero fills nothing, an unknown value fills nothing, and a measured
    value fills at least three pixels so it never looks like an empty bar.
    """
    value = quota_value(value)
    if value is None or value <= 0:
        return 0
    return min(width, max(MIN_BAR_FILL, quota_width(value, width)))
