"""Shared vocabulary for the daemon.

Everything the daemon passes between modules is defined here, so the status
set, the severity ordering and the snapshot shape are stated exactly once.
"""

from datetime import datetime, timezone

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
STATUS_ALIASES = {
    "normal": "ok",
}

# Status -> accent name, which maps onto an LED colour on the Presto.
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

    Anything unrecognised becomes ``offline``. A malformed value must never
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
    """Severity rank of a status. Higher is worse."""
    try:
        return STATUS_ORDER.index(normalise_status(status))
    except ValueError:  # pragma: no cover - normalise_status never returns this
        return STATUS_ORDER.index(UNKNOWN_STATUS)


def accent_for(status):
    """The accent name for a status."""
    return STATUS_ACCENT.get(normalise_status(status), "red")


def now_utc():
    """The current time as an aware UTC datetime."""
    return datetime.now(timezone.utc)


def iso(moment):
    """Format a datetime as an ISO-8601 string with offset."""
    if moment is None:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.isoformat()


class SourceSnapshot(object):
    """What one source reported at one moment.

    ``metrics`` is source-specific and may be partial. ``alerts`` is a list of
    dicts with ``severity`` and ``message``. ``cards`` is normally empty; the
    daemon builds cards from snapshots so the layout lives in one place.
    """

    __slots__ = ("source_id", "status", "updated_at", "metrics", "alerts",
                 "cards", "error")

    def __init__(self, source_id, status, updated_at=None, metrics=None,
                 alerts=None, cards=None, error=None):
        self.source_id = source_id
        self.status = normalise_status(status)
        self.updated_at = updated_at or now_utc()
        self.metrics = dict(metrics or {})
        self.alerts = list(alerts or [])
        self.cards = list(cards or [])
        self.error = error

    def to_dict(self):
        return {
            "source_id": self.source_id,
            "status": self.status,
            "updated_at": iso(self.updated_at),
            "metrics": self.metrics,
            "alerts": self.alerts,
            "cards": self.cards,
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, data):
        updated = data.get("updated_at")
        if isinstance(updated, str):
            try:
                updated = datetime.fromisoformat(updated)
            except ValueError:
                updated = None
        return cls(
            source_id=data.get("source_id", "unknown"),
            status=data.get("status"),
            updated_at=updated,
            metrics=data.get("metrics"),
            alerts=data.get("alerts"),
            cards=data.get("cards"),
            error=data.get("error"),
        )

    def __repr__(self):
        return "SourceSnapshot(%r, %r)" % (self.source_id, self.status)


class Alert(object):
    """A raised alert."""

    __slots__ = ("severity", "message", "source_id", "created_at")

    def __init__(self, severity_, message, source_id=None, created_at=None):
        self.severity = normalise_status(severity_)
        self.message = message
        self.source_id = source_id
        self.created_at = created_at or now_utc()

    def to_dict(self):
        return {
            "severity": self.severity,
            "message": self.message,
            "source_id": self.source_id,
            "created_at": iso(self.created_at),
        }

    @classmethod
    def from_dict(cls, data):
        created = data.get("created_at")
        if isinstance(created, str):
            try:
                created = datetime.fromisoformat(created)
            except ValueError:
                created = None
        return cls(
            severity_=data.get("severity"),
            message=data.get("message", ""),
            source_id=data.get("source_id"),
            created_at=created,
        )

    def __repr__(self):
        return "Alert(%r, %r)" % (self.severity, self.message)


class Card(object):
    """One card in the status payload."""

    __slots__ = ("id", "title", "status", "primary", "secondary", "detail",
                 "accent", "alert")

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

    def to_dict(self):
        return {
            "id": self.id,
            "title": self.title,
            "status": self.status,
            "primary": self.primary,
            "secondary": self.secondary,
            "detail": self.detail,
            "accent": self.accent,
            "alert": self.alert,
        }

    def __repr__(self):
        return "Card(%r, %r, %r)" % (self.id, self.title, self.status)


def sort_cards(cards):
    """Order cards by the plan's fixed order; unknown ids go last."""
    def key(card):
        try:
            return (0, CARD_ORDER.index(card.id))
        except ValueError:
            return (1, 0)
    return sorted(cards, key=key)


def overall_status(cards):
    """The most severe status across all cards.

    An empty list is ``offline``: no data is not the same as good data.
    """
    if not cards:
        return UNKNOWN_STATUS
    worst = cards[0].status
    for card in cards[1:]:
        if severity(card.status) > severity(worst):
            worst = card.status
    return normalise_status(worst)
