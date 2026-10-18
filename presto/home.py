"""Live source/quota rows, static preview data, and bounded Home scrolling."""

from gestures import DRAG_SLOP_PX

LIST_TOP = 56
LIST_HEIGHT = 150
ROW_HEIGHT = 50

# Live Home: cloud quota rows from the top edge, local services on one line.
SOURCE_TOP = 4
SOURCE_HEIGHT = 184
SOURCE_ROW = 46
LOCAL_Y = 194

DEMO_QUOTAS = (
    {"name": "OpenAI", "daily": 62, "weekly": 38},
    {"name": "Claude", "daily": 84, "weekly": 68},
    {"name": "Ollama Cloud", "daily": 93, "weekly": 79},
    {"name": "OpenCode.ai", "daily": 27, "weekly": 54},
)


def scroll_limit(count, row_height=ROW_HEIGHT, height=LIST_HEIGHT):
    return max(0, count * row_height - height)


def source_row_height(rows):
    """Every live row is two lines, however many windows it stacks."""
    return SOURCE_ROW


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
        return "%dh%02dm" % (hours, minutes % 60)
    days = hours // 24
    return "%dd" % days if days >= 10 else "%dd%dh" % (days, hours % 24)


def binding_window(windows):
    """The window most likely to stop work: highest use, then longest period."""
    best = None
    for index, window in enumerate(windows):
        if window["percent"] is None or window["state"] not in ("available", "stale"):
            continue
        period = window.get("period_minutes")
        key = (window["percent"], period if isinstance(period, int) else 10 ** 9)
        if best is None or key > best[0]:
            best = (key, index)
    return None if best is None else best[1]


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


def home_split(rows):
    """Cloud quota rows fill Home; local services share one line beneath.

    With no quota accounts at all, local services are the list instead.
    """
    cloud = [row for row in rows if "windows" in row]
    local = [row for row in rows if "windows" not in row]
    return (cloud, local) if cloud else (local, [])



def source_rows(payload):
    """Reusable per-instance summaries; absent usage is never a zero or quota."""
    from cards import normalise_status
    sources = payload.get("sources")
    if not isinstance(sources, list):
        return None  # Older daemons still render their ordinary summary card.
    # Managed accounts follow the saved order on Home and in the carousel.
    if payload.get("card_order") != "accounts":
        sources = sorted(sources, key=lambda source: 0 if isinstance(source, dict) and
                         isinstance(source.get("quota"), dict) else 1)
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
                windows.append({"label": label, "percent": percent, "state": state,
                                "period_minutes": window.get("period_minutes"),
                                "resets_at": window.get("resets_at"),
                                "reset_in": None if resets is None or now is None else resets - now,
                                "age": None if observed is None or now is None else now - observed})
            activity = quota.get("activity")
            text = ""
            if isinstance(activity, dict):
                requests, cost = activity.get("request_count"), activity.get("usage_usd")
                text = ("%s req / $%s" % (requests, ("%.6f" % cost).rstrip("0").rstrip("."))
                        if quota_value(requests) is not None and quota_value(cost) is not None
                        else "Activity unavailable")
                start, until = iso_seconds(activity.get("from")), iso_seconds(activity.get("until"))
                if text[0] != "A" and start is not None and until is not None and until > start:
                    text += " in %dd" % ((until - start + 43200) // 86400)
                if activity.get("state") == "stale":
                    text = "Stale: " + text
            if windows or text:
                rows.append({"id": source.get("id"), "type": source.get("type"),
                             "name": str(source.get("name") or "Cloud account"),
                             "status": normalise_status(source.get("status")), "usage": text,
                             "windows": windows, "activity": text,
                             "binding": binding_window(windows)})
                continue
        metrics = source.get("metrics")
        metrics = metrics if isinstance(metrics, dict) else {}
        usage = []
        for key, label in (("requests_today", "req"), ("tokens_today", "tok")):
            value = metrics.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                usage.append("%s %s" % (value, label))
        text = " / ".join(usage) if usage else "Usage unavailable"
        if not usage and metrics.get("loaded_model"):
            text = str(metrics["loaded_model"])
        rows.append({"id": source.get("id"), "type": source.get("type"),
                     "name": str(source.get("name") or source.get("type") or "Source"),
                     "status": normalise_status(source.get("status")), "usage": text})
    return rows


class HomeScroll(object):
    """Finger-following vertical scroll; horizontal drags belong to cards."""

    def __init__(self, count, row_height=ROW_HEIGHT, top=LIST_TOP, height=LIST_HEIGHT):
        self.top, self.height = top, height
        self.limit = scroll_limit(count, row_height, height)
        self.offset = 0
        self._down = False
        self._axis = None

    def set_count(self, count, row_height=ROW_HEIGHT, top=None, height=None):
        if top is not None:
            self.top, self.height = top, height
        self.limit = scroll_limit(count, row_height, self.height)
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
