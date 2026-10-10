"""Illustrative quotas and bounded scrolling for the Home design preview."""

from gestures import DRAG_SLOP_PX

LIST_TOP = 56
LIST_HEIGHT = 150
ROW_HEIGHT = 50

DEMO_QUOTAS = (
    {"name": "OpenAI", "daily": 62, "weekly": 38},
    {"name": "Claude", "daily": 84, "weekly": 68},
    {"name": "Ollama Cloud", "daily": 93, "weekly": 79},
    {"name": "OpenCode.ai", "daily": 27, "weekly": 54},
)


def scroll_limit(count):
    return max(0, count * ROW_HEIGHT - LIST_HEIGHT)


def source_rows(payload):
    """Reusable per-instance summaries; absent usage is never a zero or quota."""
    from cards import normalise_status
    sources = payload.get("sources")
    if not isinstance(sources, list):
        return None  # Older daemons still render their ordinary summary card.
    rows = []
    for source in sources:
        if not isinstance(source, dict):
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

    def __init__(self, count):
        self.limit = scroll_limit(count)
        self.offset = 0
        self._down = False
        self._axis = None

    def set_count(self, count):
        self.limit = scroll_limit(count)
        self.offset = min(self.offset, self.limit)

    def update(self, touched, x, y, enabled=True):
        if not touched or not enabled:
            self._down = False
            return
        if not self._down:
            self._down = True
            self._x, self._y = x, y
            self._origin = self.offset
            self._axis = None if LIST_TOP <= y < LIST_TOP + LIST_HEIGHT else "ignore"
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
