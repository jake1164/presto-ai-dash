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

# Legacy/static payload order. Managed accounts use the daemon's sequence.
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


# The screen has bitmap fonts and no other glyphs, so every line is ASCII.
NON_ASCII = {
    "·": "-",   # the middle dot the daemon uses between two facts
    "…": "-",
    "–": "-",
    "—": "-",
    "‘": "'",
    "’": "'",
    "“": '"',
    "”": '"',
    "°": " deg",
}


def ascii_text(text):
    """The line as ASCII: every other character becomes a space."""
    out = []
    for char in str(text):
        if " " <= char <= "~":
            out.append(char)
        elif char in NON_ASCII:
            out.append(NON_ASCII[char])
        else:
            out.append(" ")
    return "".join(out)


def _money_value(whole, fraction):
    """One amount: two decimals, unless two decimals would hide it."""
    if not fraction:
        return "$" + whole
    number = int(fraction)
    scale = 10 ** len(fraction)
    cents = (number * 100 + scale // 2) // scale
    if cents:
        return "$%s.%02d" % (whole, cents)
    if number:
        # A real amount below a cent: two decimals would read as zero.
        trimmed = fraction.rstrip("0")
        return "$%s.%s" % (whole, trimmed[:5] if len(trimmed) > 5 else trimmed)
    return "$%s.00" % whole


def format_money(text):
    """A price with two decimals: $0.5000 is $0.50."""
    out = []
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if char != "$":
            out.append(char)
            index += 1
            continue
        whole = ""
        probe = index + 1
        while probe < length and text[probe].isdigit():
            whole += text[probe]
            probe += 1
        if not whole:
            out.append(char)
            index += 1
            continue
        fraction = ""
        if probe < length and text[probe] == ".":
            end = probe + 1
            while end < length and text[end].isdigit():
                end += 1
            if end > probe + 1:
                fraction = text[probe + 1:end]
                probe = end
        out.append(_money_value(whole, fraction))
        index = probe
    return "".join(out)


def format_booleans(text):
    """A boolean is a word, not a word of code: ok and no."""
    lowered = text.lower()
    for word, spoken in (("true", "ok"), ("false", "no")):
        suffix = ": " + word
        if lowered.endswith(suffix):
            return text[:-len(suffix)] + ": " + spoken
    return text


def tidy_line(text):
    """One card line, ready for the screen.

    ASCII only, money with two decimals, a boolean as a word, and no
    "(stale)": staleness is shown once, as a chip beside the status.
    """
    text = ascii_text(text)
    text = format_money(text)
    text = format_booleans(text)
    text = text.replace(" (stale)", "").replace("(stale)", "")
    return " ".join(text.split())


def is_unavailable_line(text):
    """A line that says a window is not available."""
    return str(text).strip().lower().endswith("unavailable")


# The tiles a card may carry: at most six, a short label, a short value
# and a tone. The daemon builds them; the device only draws them.
MAX_TILES = 6
MAX_TILE_LABEL = 8
MAX_TILE_VALUE = 10
TILE_TONES = ("normal", "good", "warn", "bad", "dim")


def parse_tiles(raw):
    """The tiles of a card, inside the limits. Never raises."""
    if not isinstance(raw, list):
        return []
    tiles = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        label = item.get("label")
        value = item.get("value")
        if not isinstance(label, str) or not isinstance(value, str):
            continue
        label = " ".join(ascii_text(label).split())[:MAX_TILE_LABEL]
        value = " ".join(ascii_text(value).split())[:MAX_TILE_VALUE]
        if not label or not value:
            continue
        tone = item.get("tone")
        if tone not in TILE_TONES:
            tone = "normal"
        tiles.append({"label": label, "value": value, "tone": tone})
        if len(tiles) >= MAX_TILES:
            break
    return tiles


def clean_id(value):
    """A card or service id: a non-empty string, or ``None``.

    Ids are used as dictionary keys and compared with each other, and a list
    or a dict from a bad payload cannot be hashed. Anything but text is
    therefore not an id.
    """
    if isinstance(value, str) and value:
        return value
    return None


# The most detail lines a card may show; the daemon sends far fewer.
MAX_DETAIL_LINES = 40


def parse_lines(raw):
    """The detail lines of a card, as clean text. Never raises."""
    if not isinstance(raw, list):
        return []
    lines = []
    for item in raw:
        if isinstance(item, str):
            lines.append(tidy_line(item))
            if len(lines) >= MAX_DETAIL_LINES:
                break
    return lines


class Card(object):
    """One swipeable card."""

    __slots__ = ("id", "title", "status", "primary", "secondary",
                 "detail", "accent", "alert", "stale", "tiles")

    def __init__(self, id, title, status, primary="", secondary="",
                 detail=None, accent=None, alert=None, stale=False, tiles=None):
        self.id = id
        self.title = title
        self.status = normalise_status(status)
        self.primary = "" if primary is None else str(primary)
        self.secondary = "" if secondary is None else str(secondary)
        self.detail = [str(line) for line in (detail or [])]
        self.accent = accent if accent in VALID_ACCENTS else accent_for(self.status)
        self.alert = alert
        # Whether any of the card's numbers is a stale reading. It is shown
        # once, as a chip, and never on every line.
        self.stale = bool(stale)
        # Tiles the payload carried with the card. The lazy detail brings
        # its own.
        self.tiles = parse_tiles(tiles)

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
        raw = [str(line) for line in detail]
        raw_primary = str(data.get("primary") or "")
        raw_secondary = str(data.get("secondary") or "")
        stale = any("(stale)" in line for line in
                    [raw_primary, raw_secondary] + raw)
        title = data.get("title")
        return cls(
            id=clean_id(data.get("id")) or "unknown",
            title=title if isinstance(title, str) and title else "Unknown",
            status=data.get("status"),
            primary=tidy_line(data.get("primary") or ""),
            secondary=tidy_line(data.get("secondary") or ""),
            detail=[tidy_line(line) for line in raw],
            accent=data.get("accent"),
            alert=alert,
            stale=stale,
            tiles=data.get("tiles"),
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

    # MicroPython's list.sort() is NOT stable (verified on a Presto: sorting
    # seven cards by a key that is the same for six of them scrambled those
    # six). Never sort with a key that can tie: the saved account order would
    # be lost on the device while the PC preview, whose sort is stable, looked
    # right. tests/test_card_order.py guards every sort in presto/.
    if payload.get("card_order") == "accounts":
        # Keep configured account order, with Home first even after an edit.
        home = [card for card in cards if card.id == "dash"]
        rest = [card for card in cards if card.id != "dash"]
        return home + rest, None

    # Older/static payloads: the known order first; anything else keeps its
    # position. The position is part of the key, so no two keys are equal.
    def sort_key(position):
        try:
            return (0, CARD_ORDER.index(cards[position].id), position)
        except ValueError:
            return (1, 0, position)

    return [cards[position] for position in sorted(range(len(cards)), key=sort_key)], None
