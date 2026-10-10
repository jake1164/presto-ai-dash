"""Card model and status vocabulary for the Presto app.

Pure Python. No hardware imports, so this module is importable (and testable)
on a development machine as well as on the Presto.
"""

# Canonical statuses, least severe first. Index == severity.
STATUS_ORDER = (
    "ok",
    "active",
    "warning",
    "near_limit",
    "rate_limited",
    "critical",
    "offline",
)

# The project plan uses both "ok" and "normal" for the same condition.
# Accept "normal" on input and normalise it away.
STATUS_ALIASES = {
    "normal": "ok",
    "unknown": "offline",
}

# Status -> accent name. The accent maps onto an LED colour.
STATUS_ACCENT = {
    "ok": "green",
    "active": "blue",
    "warning": "yellow",
    "near_limit": "yellow",
    "rate_limited": "red",
    "critical": "red",
    "offline": "red",
}

VALID_ACCENTS = ("green", "blue", "yellow", "red", "purple", "white", "grey")

# Card order is fixed by the project plan (section 12).
CARD_ORDER = ("dash", "ollama", "litellm")

UNKNOWN_STATUS = "offline"


def normalise_status(status):
    """Map any incoming status onto the canonical vocabulary.

    Anything unrecognised becomes ``offline`` so a malformed payload can never
    make the device look healthier than it is.
    """
    if not isinstance(status, str):
        return UNKNOWN_STATUS
    key = status.strip().lower()
    key = STATUS_ALIASES.get(key, key)
    if key in STATUS_ORDER:
        return key
    return UNKNOWN_STATUS


def severity(status):
    """Return the severity rank of a status. Higher is worse."""
    try:
        return STATUS_ORDER.index(normalise_status(status))
    except ValueError:
        return STATUS_ORDER.index(UNKNOWN_STATUS)


def accent_for(status):
    """Return the accent name for a status."""
    return STATUS_ACCENT.get(normalise_status(status), "red")


def overall_status(cards):
    """The most severe status across all cards.

    An empty card list is ``offline`` - no data is not the same as good data.
    """
    if not cards:
        return UNKNOWN_STATUS
    worst = cards[0].status
    for card in cards[1:]:
        if severity(card.status) > severity(worst):
            worst = card.status
    return normalise_status(worst)


def truncate(text, max_chars):
    """Clip a string to ``max_chars``, adding an ellipsis if it was cut."""
    if text is None:
        return ""
    text = str(text)
    if max_chars <= 0:
        return ""
    if len(text) <= max_chars:
        return text
    if max_chars <= 1:
        return text[:max_chars]
    return text[:max_chars - 1] + "~"


class Card(object):
    """One swipeable card."""

    __slots__ = ("id", "title", "status", "primary", "secondary",
                 "detail", "accent", "alert")

    def __init__(self, id, title, status, primary="", secondary="",
                 detail=None, accent=None, alert=None):
        self.id = id
        self.title = title
        self.status = normalise_status(status)
        self.primary = "" if primary is None else str(primary)
        self.secondary = "" if secondary is None else str(secondary)
        self.detail = [str(line) for line in (detail or [])]
        self.accent = accent if accent in VALID_ACCENTS else accent_for(self.status)
        self.alert = alert

    @classmethod
    def from_dict(cls, data):
        """Build a Card from the daemon's JSON. Never raises."""
        if not isinstance(data, dict):
            return cls("unknown", "Unknown", "offline", "bad card data")
        alert = data.get("alert")
        if not isinstance(alert, dict):
            alert = None
        detail = data.get("detail")
        if not isinstance(detail, list):
            detail = []
        return cls(
            id=data.get("id") or "unknown",
            title=data.get("title") or "Unknown",
            status=data.get("status"),
            primary=data.get("primary"),
            secondary=data.get("secondary"),
            detail=detail,
            accent=data.get("accent"),
            alert=alert,
        )

    def detail_lines(self, limit):
        """Up to ``limit`` detail lines, padded with blanks."""
        lines = list(self.detail)[:limit]
        while len(lines) < limit:
            lines.append("")
        return lines

    def __repr__(self):
        return "Card(%r, %r, %r)" % (self.id, self.title, self.status)


def error_card(message):
    """The card shown when the payload could not be understood at all."""
    return Card(
        id="error",
        title="Bad data",
        status="critical",
        primary="Cannot read status",
        secondary=message,
        detail=[message],
        accent="red",
    )


def offline_card(message):
    """The card shown when the daemon is unreachable."""
    return Card(
        id="offline",
        title="Daemon offline",
        status="offline",
        primary="No data",
        secondary=message,
        detail=[message, "Retrying..."],
        accent="red",
    )


def parse_cards(payload):
    """Turn a daemon payload into a list of Cards.

    Returns ``(cards, error)``. ``error`` is a string when the payload was
    unusable, otherwise ``None``. A payload with zero cards is an error: the
    device must never look healthy because it received nothing.
    """
    if not isinstance(payload, dict):
        return [error_card("payload was not an object")], "payload was not an object"

    raw = payload.get("cards")
    if not isinstance(raw, list) or not raw:
        return [error_card("no cards in payload")], "no cards in payload"

    cards = [Card.from_dict(item) for item in raw]

    # Keep the plan's fixed order; unknown ids go on the end.
    def sort_key(card):
        try:
            return (0, CARD_ORDER.index(card.id))
        except ValueError:
            return (1, 0)

    cards.sort(key=sort_key)
    return cards, None
