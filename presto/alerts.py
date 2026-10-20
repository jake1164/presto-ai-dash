"""LED and sound behaviour for the Presto app.

Pure Python. No hardware imports, so this module is importable (and testable)
on a development machine as well as on the Presto.

The mapping from status to colour and sound is deliberately data-driven so the
behaviour can be asserted in tests without a buzzer or an LED strip.
"""

from cards import VALID_ACCENTS, normalise_status

# LED colours, as (r, g, b) at full brightness. Scaled by LED_BRIGHTNESS at
# the point of use, so these stay readable.
ACCENT_RGB = {
    "green": (0, 200, 80),
    "blue": (40, 120, 255),
    "yellow": (255, 190, 0),
    "red": (255, 40, 30),
    "purple": (150, 60, 220),
    "white": (255, 255, 255),
    "grey": (60, 60, 60),
}

# Sound cues. The plan asks for a short chirp on a new warning and a double
# chirp on a critical alert, with no repeating alarm unless configured.
CUE_NONE = "none"
CUE_CHIRP = "chirp"            # short chirp: new warning
CUE_DOUBLE_CHIRP = "double"    # double chirp: critical alert

# Which accent each status drives. Mirrors cards.STATUS_ACCENT but is kept
# separate so the LED policy can change without touching the card model.
STATUS_ACCENT = {
    "ok": "green",
    "active": "blue",
    "warning": "yellow",
    "near_limit": "yellow",
    "rate_limited": "red",
    "critical": "red",
    "offline": "red",
}

# Statuses that warrant a sound cue.
_STATUS_CUE = {
    "warning": CUE_CHIRP,
    "near_limit": CUE_CHIRP,
    "rate_limited": CUE_DOUBLE_CHIRP,
    "critical": CUE_DOUBLE_CHIRP,
    "offline": CUE_DOUBLE_CHIRP,
}

# How a service's condition changes for the worse. This is not the card
# order: a service at 100% is rate limited, which is worse than the
# critical state just below it, so the ladder puts rate_limited last.
ALERT_ORDER = (
    "ok",
    "active",
    "warning",
    "near_limit",
    "critical",
    "rate_limited",
    "offline",
)

# Which colour the LED strip shows, worst first. Red beats yellow, yellow
# beats blue and blue beats green: a service that is merely in use never
# hides a warning.
STATUS_PRIORITY = {
    "ok": 0,
    "active": 1,
    "warning": 2,
    "near_limit": 2,
    "critical": 3,
    "rate_limited": 3,
    "offline": 3,
}
PRIORITY_ACCENT = {0: "green", 1: "blue", 2: "yellow", 3: "red"}

# How much of the LED brightness is left during quiet hours. The colour is
# kept: a warning must still be visible, only less insistently.
QUIET_LED_FACTOR = 0.4


def alert_rank(status):
    """The alert rank of a status. Higher is worse."""
    status = normalise_status(status)
    try:
        return ALERT_ORDER.index(status)
    except ValueError:  # pragma: no cover - normalise_status never returns this
        return ALERT_ORDER.index("offline")


def accent_for_status(status):
    """The LED accent for a status."""
    return STATUS_ACCENT.get(normalise_status(status), "red")


def rgb_for_accent(accent, brightness=1.0):
    """Scale an accent's RGB by ``brightness`` (0.0-1.0).

    Returns a 3-tuple of ints. An unknown accent falls back to red, because an
    unknown state must never look healthy.
    """
    if accent not in ACCENT_RGB:
        accent = "red"
    r, g, b = ACCENT_RGB[accent]
    if brightness >= 1.0:
        return (r, g, b)
    if brightness <= 0.0:
        return (0, 0, 0)
    return (
        int(r * brightness),
        int(g * brightness),
        int(b * brightness),
    )


def rgb_for_status(status, brightness=1.0):
    """The LED RGB for a status, scaled by brightness."""
    return rgb_for_accent(accent_for_status(status), brightness)


def cue_for_status(status):
    """The sound cue for a status."""
    return _STATUS_CUE.get(normalise_status(status), CUE_NONE)


def chirp_count(cue):
    """How many tones a cue plays."""
    if cue == CUE_CHIRP:
        return 1
    if cue == CUE_DOUBLE_CHIRP:
        return 2
    return 0


def worst_accent(statuses):
    """The accent for the worst of several statuses.

    Red wins over yellow, yellow over blue, blue over green. An unknown
    status counts as red: an unknown state must never look healthy.
    """
    accent = "green"
    best = 0
    for status in statuses or ():
        priority = STATUS_PRIORITY.get(normalise_status(status), 3)
        if priority > best:
            best = priority
            accent = PRIORITY_ACCENT[priority]
    return accent


def led_brightness(brightness, quiet=False):
    """The brightness to show: quiet hours dim the strip but keep the colour."""
    if not quiet:
        return brightness
    return brightness * QUIET_LED_FACTOR


class AlertTracker(object):
    """Decides when a sound cue should actually play.

    A cue plays when a service's status *becomes* worse than it was, not on
    every poll. Without this the device would chirp every fifteen seconds
    for as long as a warning persisted, which the plan explicitly rules
    out.

    Tracking is per service: a second service turning bad while another is
    already critical still speaks, which a single overall status could
    never do.

    The first reading of a service is a silent baseline, so the device does
    not chirp at the user the moment it powers on. A recovery is silent.

    ``muted`` suppresses all cues; long-press toggles it. ``quiet`` is the
    daemon's quiet-hours flag: it silences the sound and lets the caller
    dim the LED, without touching the user's own mute.
    """

    __slots__ = ("_statuses", "muted", "quiet")

    def __init__(self):
        self._statuses = {}
        self.muted = False
        self.quiet = False

    @property
    def statuses(self):
        """The last status seen for each service."""
        return dict(self._statuses)

    @property
    def last_status(self):
        """The worst status across the services seen so far."""
        worst = None
        for status in self._statuses.values():
            if worst is None or alert_rank(status) > alert_rank(worst):
                worst = status
        return worst

    @property
    def silenced(self):
        """True when nothing should sound: muted, or during quiet hours."""
        return self.muted or self.quiet

    def toggle_mute(self):
        """Flip the mute flag and return the new value."""
        self.muted = not self.muted
        return self.muted

    def reset(self):
        """Forget history, e.g. after a reconnect."""
        self._statuses.clear()

    def update(self, statuses):
        """Record every service's status and return the cues to play.

        ``statuses`` maps a service id to its status. The result is a list
        of ``(service, cue)`` pairs, in the order the services were given.
        """
        if not isinstance(statuses, dict):
            return []
        cues = []
        for service, status in statuses.items():
            cue = self.update_one(service, status)
            if cue != CUE_NONE:
                cues.append((service, cue))
        return cues

    def update_one(self, service, status):
        """Record one service's status and return the cue to play, if any.

        Returns one of ``CUE_NONE``, ``CUE_CHIRP`` or ``CUE_DOUBLE_CHIRP``.
        """
        status = normalise_status(status)
        previous = self._statuses.get(service)
        self._statuses[service] = status
        if self.silenced:
            return CUE_NONE
        if previous is None:
            # The first reading of a service is a silent baseline.
            return CUE_NONE
        if alert_rank(status) > alert_rank(previous):
            return cue_for_status(status)
        return CUE_NONE


def led_pattern(accent, brightness=1.0, muted=False, dim_when_muted=True):
    """The RGB to show on every LED.

    All seven LEDs show the same colour: the accent is a status cue, not a
    chart. When muted and ``dim_when_muted`` is set the LEDs drop to a dim
    grey so the device still shows it is alive without demanding attention.
    """
    if muted and dim_when_muted:
        return rgb_for_accent("grey", brightness * 0.5)
    return rgb_for_accent(accent, brightness)


def is_valid_accent(accent):
    return accent in VALID_ACCENTS
