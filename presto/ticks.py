"""A millisecond counter that keeps counting through the hardware's rollover.

The Presto's ``time.ticks_ms()`` wraps after 2**30 ms, about 12.4 days, and
``time.ticks_diff()`` is only right for two readings less than 2**29 ms,
about 6.2 days, apart. Code that subtracts raw ticks goes wrong at the wrap:
the answer turns hugely negative, so a debounce never ends and a timer that
should have fired never does. Code that uses ``ticks_diff`` goes wrong when the
two readings are further apart than half the range.

``Uptime`` adds up the small differences between successive readings instead,
so the total never wraps and two totals can simply be subtracted, however long
the device has been up. It must be read at least once in half the range; the
app reads it many times a second.

Pure Python, no hardware imports: the clock and the difference function are
passed in.
"""


class Uptime(object):
    """Milliseconds since the first reading, counted through any rollover."""

    __slots__ = ("_now", "_diff", "_last", "_total")

    def __init__(self, now, diff):
        self._now = now
        self._diff = diff
        # Read lazily, so building the object needs no clock.
        self._last = None
        self._total = 0

    def ms(self):
        """The milliseconds that have passed since the first call."""
        reading = self._now()
        if self._last is not None:
            self._total += self._diff(reading, self._last)
        self._last = reading
        return self._total

    def seconds(self):
        """Whole seconds since the first call, an exact integer."""
        return self.ms() // 1000
