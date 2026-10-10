"""Touch gesture recognition for the Presto app.

Pure Python. No hardware imports, so this module is importable (and testable)
on a development machine as well as on the Presto.

The Presto's ``touch.Button`` class is *level* sensitive: ``is_pressed()``
hit-tests the current touch position against a rectangle on every ``poll()``.
There is no built-in tap, swipe or long-press detection, so it lives here.

Written as a pure function of ``(state, x, y, timestamp_ms)`` so the whole
recogniser can be exercised without a screen.
"""

# Gestures the recogniser can emit.
TAP = "tap"
SWIPE_LEFT = "swipe_left"
SWIPE_RIGHT = "swipe_right"
SWIPE_UP = "swipe_up"
SWIPE_DOWN = "swipe_down"
LONG_PRESS = "long_press"
DRAG_SLOP_PX = 8

# Internal states.
_IDLE = "idle"
_DOWN = "down"
_LONG_FIRED = "long_fired"


class GestureRecogniser(object):
    """Turns a stream of touch samples into discrete gestures.

    Feed it ``update(state, x, y, now_ms)`` once per frame. It returns a list
    of gestures recognised on that frame (usually empty, at most one).

    Multi-touch is deliberately ignored. Pimoroni documents that two touches
    aligned horizontally are not correctly registered by the controller, so
    acting on anything other than a single touch would be unreliable.
    """

    __slots__ = ("_state", "_start_x", "_start_y", "_start_ms",
                 "long_press_ms", "swipe_min_px", "debounce_ms",
                 "_last_emit_ms", "_armed", "_axis")

    def __init__(self, long_press_ms=700, swipe_min_px=40, debounce_ms=180):
        self.long_press_ms = long_press_ms
        self.swipe_min_px = swipe_min_px
        self.debounce_ms = debounce_ms
        self._state = _IDLE
        self._start_x = 0
        self._start_y = 0
        self._start_ms = 0
        self._last_emit_ms = -debounce_ms - 1
        self._armed = True
        self._axis = None

    def reset(self):
        """Forget any in-progress touch. Used when changing screens."""
        self._state = _IDLE
        self._armed = True
        self._axis = None

    def _debounced(self, now_ms):
        return (now_ms - self._last_emit_ms) < self.debounce_ms

    def update(self, touched, x, y, now_ms):
        """Process one touch sample.

        ``touched`` is a bool, ``x``/``y`` are the touch coordinates, and
        ``now_ms`` is a monotonically increasing millisecond clock.

        Returns a list of gesture strings.
        """
        gestures = []

        if not touched:
            # Finger lifted. Decide what the whole gesture was.
            if self._state == _DOWN:
                dx = x - self._start_x
                dy = y - self._start_y
                # Include the final release position in axis recognition.
                self._track_motion(x, y)
                if self._axis == "horizontal" and abs(dx) >= self.swipe_min_px:
                    if not self._debounced(now_ms):
                        gestures.append(SWIPE_LEFT if dx < 0 else SWIPE_RIGHT)
                        self._last_emit_ms = now_ms
                elif self._axis == "vertical" and abs(dy) >= self.swipe_min_px:
                    if not self._debounced(now_ms):
                        gestures.append(SWIPE_UP if dy < 0 else SWIPE_DOWN)
                        self._last_emit_ms = now_ms
                elif self._armed and not self._debounced(now_ms):
                    # A short press that never became a long press is a tap.
                    gestures.append(TAP)
                    self._last_emit_ms = now_ms
            self._state = _IDLE
            self._armed = True
            self._axis = None
            return gestures

        # touched is True from here on.
        if self._state == _IDLE:
            self._state = _DOWN
            self._start_x = x
            self._start_y = y
            self._start_ms = now_ms
            self._armed = True
            self._axis = None
            return gestures

        if self._state == _DOWN:
            self._track_motion(x, y)
            held = now_ms - self._start_ms
            if held >= self.long_press_ms and self._armed:
                if not self._debounced(now_ms):
                    gestures.append(LONG_PRESS)
                    self._last_emit_ms = now_ms
                # A long press has fired; do not also report a tap on release.
                self._armed = False
                self._state = _LONG_FIRED
            return gestures

        # _LONG_FIRED: wait for the finger to come up.
        return gestures

    def _track_motion(self, x, y):
        dx, dy = x - self._start_x, y - self._start_y
        if self._axis is None and max(abs(dx), abs(dy)) >= DRAG_SLOP_PX:
            self._axis = "vertical" if abs(dy) > abs(dx) else "horizontal"
            # A drag must never become a tap or long press, even at a boundary.
            self._armed = False
