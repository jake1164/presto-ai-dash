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


class AlertTracker(object):
    """Decides when a sound cue should actually play.

    A cue plays when the status *becomes* worse than it was, not on every poll.
    Without this the device would chirp every fifteen seconds for as long as a
    warning persisted, which the plan explicitly rules out.

    ``muted`` suppresses all cues. Long-press toggles it.
    """

    __slots__ = ("_last_status", "muted", "_ever_seen")

    def __init__(self):
        self._last_status = None
        self.muted = False
        self._ever_seen = False

    @property
    def last_status(self):
        return self._last_status

    def toggle_mute(self):
        """Flip the mute flag and return the new value."""
        self.muted = not self.muted
        return self.muted

    def reset(self):
        """Forget history, e.g. after a reconnect."""
        self._last_status = None
        self._ever_seen = False

    def _rank(self, status):
        from cards import severity
        return severity(status)

    def update(self, status):
        """Record a status and return the cue to play, if any.

        Returns one of ``CUE_NONE``, ``CUE_CHIRP`` or ``CUE_DOUBLE_CHIRP``.
        """
        status = normalise_status(status)

        if self.muted:
            self._last_status = status
            return CUE_NONE

        if not self._ever_seen:
            # First reading after boot: establish a baseline silently so the
            # device does not chirp at the user the moment it powers on.
            self._ever_seen = True
            self._last_status = status
            return CUE_NONE

        previous = self._last_status
        self._last_status = status

        if previous is None:
            return CUE_NONE

        if self._rank(status) > self._rank(previous):
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
