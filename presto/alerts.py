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

# The mute: how long a long-press mute lasts, in hours. Zero means it lasts
# until the user presses again. The daemon sends the number; the device
# counts the time with its own tick counter, because it has no wall clock.
DEFAULT_MUTE_HOURS = 4
MUTE_HOURS_MIN = 0
MUTE_HOURS_MAX = 48
MUTE_HOUR_MS = 60 * 60 * 1000


def valid_mute_hours(value):
    """Whether this can be used as a mute length, in hours."""
    if isinstance(value, bool) or not isinstance(value, int):
        return False
    return MUTE_HOURS_MIN <= value <= MUTE_HOURS_MAX


def mute_chip_text(remaining_ms):
    """The chip text for a mute: "MUTED", "MUTED 45M" or "MUTED 3H".

    The time left is rounded up, so a mute with half a minute left says
    "MUTED 1M" and never "MUTED 0M". ``None`` means the mute has no end:
    it lasts until the user presses again.
    """
    if remaining_ms is None:
        return "MUTED"
    minutes = (int(remaining_ms) + 59999) // 60000
    if minutes <= 0:
        return "MUTED"
    if minutes < 60:
        return "MUTED %dM" % minutes
    return "MUTED %dH" % ((minutes + 59) // 60)


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

    The mute has an end: it lasts ``mute_hours`` hours and then lifts
    itself, silently. The device has no wall clock, so the length is
    counted with the device's own tick counter, which wraps; the clock and
    the tick difference are injected so the behaviour can be tested. A
    tracker without a clock never expires a mute, and a mute length of zero
    lasts until the user presses again.
    """

    __slots__ = ("_statuses", "muted", "quiet", "mute_hours", "_mute_started",
                 "_mute_duration", "_clock", "_ticks_diff")

    def __init__(self, clock=None, ticks_diff=None):
        self._statuses = {}
        self.muted = False
        self.quiet = False
        self.mute_hours = DEFAULT_MUTE_HOURS
        self._mute_started = None
        self._mute_duration = None
        self._clock = clock
        self._ticks_diff = ticks_diff

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

    def toggle_mute(self, now=None):
        """Flip the mute flag and return the new value.

        A long press while muted unmutes at once. A long press while the
        sound is on starts a mute that ends ``mute_hours`` hours later; the
        length is taken now, so changing the setting later applies to the
        next mute, not to the running one.
        """
        if self.muted:
            self.muted = False
            self._mute_started = None
            self._mute_duration = None
            return False
        self.muted = True
        started = self._now(now)
        self._mute_started = started
        if started is None or self.mute_hours <= 0:
            # No clock, or a mute that lasts until the user presses again.
            self._mute_duration = None
        else:
            self._mute_duration = self.mute_hours * MUTE_HOUR_MS
        return True

    def set_mute_hours(self, hours):
        """Set the mute length in hours; anything unusable is the default."""
        self.mute_hours = hours if valid_mute_hours(hours) else DEFAULT_MUTE_HOURS
        return self.mute_hours

    def set_mute(self, muted, remaining_ms=None, now=None):
        """Put the tracker in a known mute state.

        The app itself only ever toggles the mute. This is here so that a
        test or the preview can show the chip with a given time left:
        ``remaining_ms`` is how much of the mute is left, and None means
        the whole mute length is left.
        """
        if not muted:
            self.muted = False
            self._mute_started = None
            self._mute_duration = None
            return False
        started = self._now(now)
        self.muted = True
        duration = self.mute_hours * MUTE_HOUR_MS
        self._mute_duration = duration
        if started is None:
            self._mute_started = None
        elif remaining_ms is None:
            self._mute_started = started
        else:
            # The mute started that much earlier, so this much is left.
            self._mute_started = started - (duration - remaining_ms)
        return True

    def _now(self, now=None):
        """The tick counter's value, or None when there is no clock."""
        if now is not None:
            return now
        if self._clock is None:
            return None
        return self._clock()

    def _elapsed(self, now=None):
        """Milliseconds since the mute started, or None when unknown."""
        if self._mute_started is None:
            return None
        moment = self._now(now)
        if moment is None:
            return None
        if self._ticks_diff is not None:
            return self._ticks_diff(moment, self._mute_started)
        return moment - self._mute_started

    def mute_expired(self, now=None):
        """Whether the mute has run out; it is then lifted, silently.

        Lifting the mute says nothing: the statuses were tracked while it
        was on, so the device resumes without chirping for an alarm that
        began during the mute.
        """
        if not self.muted or not self._mute_duration:
            return False
        elapsed = self._elapsed(now)
        if elapsed is None or elapsed < self._mute_duration:
            return False
        self.muted = False
        self._mute_started = None
        self._mute_duration = None
        return True

    def mute_remaining(self, now=None):
        """Milliseconds the mute has left, or None when it has no end."""
        if not self.muted or not self._mute_duration:
            return None
        elapsed = self._elapsed(now)
        if elapsed is None:
            return None
        left = self._mute_duration - elapsed
        return left if left > 0 else 0

    def mute_label(self, now=None):
        """The chip text for the mute, or None when the sound is not muted."""
        if not self.muted:
            return None
        return mute_chip_text(self.mute_remaining(now))

    def reset(self):
        """Forget history, e.g. after a reconnect."""
        self._statuses.clear()

    def forget(self, service):
        """Forget one service's baseline.

        A service whose alerts are turned off stops counting at once: it
        makes no sound and no longer colours the light. Its baseline goes
        with it, so turning the alerts back on starts from the status as it
        is now - an alarm that already existed does not chirp again.
        """
        self._statuses.pop(service, None)

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
