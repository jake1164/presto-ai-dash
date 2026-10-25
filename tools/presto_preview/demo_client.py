"""The demo, in-process, for the preview: no daemon and no network.

The client answers with the daemon's own ``presto_ai_dash.demo`` module, so
PSL draws exactly the payloads the daemon's demo mode and the published demo
page draw. The scenario advances with a monotonic clock started when the
client is created; ``offset`` (in seconds) jumps to another point in the loop.

The app's client needs ``fetch_async()``, ``fetch_card(card_id)`` and
``post_alert(service, enabled)``. The last one is the device's only write: it
changes a dictionary here and nothing else, exactly as the daemon's demo mode
keeps the bell taps in memory.
"""

import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(HERE))
DAEMON_SRC = os.path.join(REPO_ROOT, "daemon", "src")
if DAEMON_SRC not in sys.path:
    sys.path.append(DAEMON_SRC)

from presto_ai_dash import demo  # noqa: E402

# The scenes the preview can jump to, in loop order.
SCENES = tuple(name for name, _start in demo.SCENES)
DEFAULT_SCENE = SCENES[0]


def scene_offset(name):
    """The loop second a scene starts at: the offset that jumps to it.

    Raises KeyError for a name the demo does not have, so a caller can refuse
    it rather than start somewhere arbitrary.
    """
    return demo.scene_second(name)


def payload_at(t):
    """The demo payload at loop second ``t``: what render.py draws."""
    return demo.demo_payload(t)


def card_detail_at(card_id, t):
    """One card's tiles and lines at loop second ``t``, or None."""
    return demo.demo_card_detail(card_id, t)


class DemoClient(object):
    """The demo as the app's client, without a socket anywhere."""

    def __init__(self, offset=0.0, clock=time.monotonic):
        self._clock = clock
        self.started = clock()
        self.offset = offset
        # The "Simulate network failure" switch: every request fails while set.
        self.simulate_failure = False
        # The bell, per service: in memory, and never written anywhere else.
        self.alerts = {}

    def second(self):
        """The loop second now: seconds since it started, plus the offset."""
        elapsed = self._clock() - self.started + self.offset
        return elapsed % demo.LOOP_SECONDS

    def _refuse_if_failing(self):
        if self.simulate_failure:
            # The same exception a failed fetch raises, so the app runs its
            # real stale (10 s) and offline (30 s) timers.
            import net
            error = net.NetworkUnavailable("simulated by preview")
            # The offline screen shows the reason a real client would give.
            error.reason = "simulated loss"
            raise error

    async def fetch_async(self):
        """The status payload for this moment, with the bells as tapped."""
        self._refuse_if_failing()
        return demo.demo_payload(self.second(), alert_overrides=self.alerts)

    async def fetch_card(self, card_id):
        """One card's lazy detail, or None for an id the demo does not have."""
        self._refuse_if_failing()
        detail = demo.demo_card_detail(card_id, self.second())
        if detail is not None:
            # The lines are built once per step; a copy keeps callers from
            # changing what the next fetch returns.
            detail = dict(detail, tiles=list(detail.get("tiles") or ()),
                          lines=list(detail.get("lines") or ()))
        return detail

    async def post_alert(self, service, enabled):
        """The one write: this service's alerts, remembered in memory only."""
        self._refuse_if_failing()
        enabled = bool(enabled)
        self.alerts[service] = enabled
        return {"ok": True, "alerts": enabled, "master": True}
