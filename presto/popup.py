"""The alarm pop-to-top: Home scrolls to a service that just got worse.

Pure Python and no hardware imports, so the state machine can be tested on a
development machine. The clock is injected: the device passes its own
millisecond counter, the tests pass a number of seconds. Nothing here draws
anything; the app carries out the action this module returns.

The rules, from the plan:

* the alarm must persist before the list moves, so a reading that flickers
  never bounces the list,
* at most one pop for a service in the cool-down period,
* never while the user is touching the screen or has scrolled recently,
* the user's own position comes back after a while, unless the user has
  scrolled since, and
* a new alarm only acts when it is worse than the worst one already shown.

Home only: the app asks for an action only while Home is on screen, never on
a card or a detail view.
"""

from alerts import alert_rank
from cards import normalise_status

# How long an alarm must persist before Home moves.
PERSIST_SECONDS = 30
# At most one pop for a service in this period.
COOLDOWN_SECONDS = 600
# Never move the list this soon after the user touched it.
TOUCH_GUARD_SECONDS = 20
# How long the popped position is kept before the user's position returns.
RETURN_SECONDS = 60


def row_offset(layout, service, offset=0):
    """The scroll offset that brings this service's row into view.

    Returns None when the layout does not know the service. A row that is
    already fully visible keeps the list where it is: moving the list for a
    service the user can already see would only annoy them.
    """
    if layout is None:
        return None
    top = 0
    for row, height, _compact in layout.rows:
        if str(row.get("id")) == str(service):
            if offset <= top and top + height <= offset + layout.region:
                return offset
            lower = max(0, top + height - layout.region)
            upper = min(top, layout.limit)
            if lower > upper:
                return lower
            return upper
        top += height
    return None


class PopToTop(object):
    """Decides when Home scrolls to an alarmed service, and when it goes back."""

    def __init__(self):
        # The last status seen for each service: the baseline a worsening
        # is measured against.
        self._seen = {}
        # Services whose status got worse and are waiting to be shown.
        self._pending = {}
        # When each service last moved the list.
        self._cooldown = {}
        # The worst status that has already been shown.
        self._shown = None
        # When the user last touched the screen.
        self._last_touch = None
        # The move in progress, if any.
        self._pop = None

    # -- what the app tells us ---------------------------------------------

    def touch(self, now):
        """The user touched the screen."""
        self._last_touch = now

    def scrolled(self, now):
        """The user scrolled the list.

        A scroll also cancels a pending return to the old position: the
        user has said where they want the list.
        """
        self.touch(now)
        if self._pop is not None:
            self._pop["cancel"] = True

    def observe(self, service, status, now):
        """Record a service's status; a worsening starts a pending pop.

        The first reading of a service is the baseline, so a device that
        boots during a warning does not jump the list.
        """
        status = normalise_status(status)
        previous = self._seen.get(service)
        self._seen[service] = status
        self._forget_shown()
        if previous is None:
            return
        if alert_rank(status) > alert_rank(previous):
            pending = self._pending.get(service)
            if pending is None:
                self._pending[service] = {"status": status, "since": now}
            else:
                pending["status"] = status
        elif alert_rank(status) < alert_rank(previous):
            # It recovered: the pending alarm is void.
            self._pending.pop(service, None)

    def _forget_shown(self):
        """Clear the worst alarm shown once no service is alarmed any more.

        "Worse than the one already shown" is about alarms that are still
        active. Without this the first critical pop would silence every
        later warning for good.
        """
        if self._shown is None:
            return
        floor = alert_rank("warning")
        for status in self._seen.values():
            if alert_rank(status) >= floor:
                return
        self._shown = None

    # -- what the app asks for --------------------------------------------

    def poll(self, now, layout=None, offset=0):
        """The action the app should carry out, or None.

        The action is a dict with a "kind" of "scroll_to" or "restore", the
        scroll "offset" to use, and the "service" the action is about.
        """
        action = self._restore(now)
        if action is not None:
            return action
        return self._move(now, layout, offset)

    def _restore(self, now):
        """Put the user's own position back, unless they scrolled since."""
        if self._pop is None:
            return None
        if now - self._pop["at"] < RETURN_SECONDS:
            return None
        pop = self._pop
        self._pop = None
        if pop["cancel"]:
            return None
        return {"kind": "restore", "offset": pop["previous"], "service": pop["service"]}

    def _move(self, now, layout, offset):
        """Move the list to the worst alarm that is allowed to act."""
        best = None
        for service, pending in self._pending.items():
            if now - pending["since"] < PERSIST_SECONDS:
                # The alarm has not persisted long enough yet.
                continue
            if (self._last_touch is not None and
                    now - self._last_touch < TOUCH_GUARD_SECONDS):
                # The user is at the screen; do not move the list.
                continue
            last = self._cooldown.get(service)
            if last is not None and now - last < COOLDOWN_SECONDS:
                # This service has moved the list recently enough.
                continue
            rank = alert_rank(pending["status"])
            if self._shown is not None and rank <= alert_rank(self._shown):
                # Only a worse alarm than the one already shown may act.
                continue
            if best is None or rank > alert_rank(best[1]["status"]):
                best = (service, pending)
        if best is None:
            return None
        service, pending = best
        status = pending["status"]
        target = row_offset(layout, service, offset)
        self._cooldown[service] = now
        self._shown = status
        self._pending.pop(service, None)
        if target is None or target == offset:
            # The service is already on screen, or is not in the list at
            # all: there is nothing to move.
            return None
        self._pop = {"service": service, "offset": target, "previous": offset,
                     "at": now, "cancel": False}
        return {"kind": "scroll_to", "offset": target, "service": service}
