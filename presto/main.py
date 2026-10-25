# ICON monitoring
# NAME AI Dash
# DESC A tiny AI usage dashboard for Pimoroni Presto.

"""Presto AI Dash - Presto application.

The only module in this app that imports ``presto`` or ``machine``. Everything
else (cards, gestures, alerts, net, ui) is pure Python and is unit-tested on a
development machine. See docs/limitations.md for what cannot be automated.

The app polls the daemon over the LAN with the shared token. Sample data has
no place on the device: the demo is served by the daemon (see demo.py there),
so what the screen shows is the same code path either way.
Network tasks yield to the touch/render loop. No cloud services are contacted
directly by this device.
"""

import time
import asyncio
import sys

try:
    import config
except ImportError:
    class config:
        # The launcher should always deploy config.py. A missing one must not
        # crash on a dotted filename: every setting falls back to its default.
        pass

from alerts import AlertTracker, led_brightness, led_pattern, worst_accent
from cards import (clean_id, error_card, normalise_status, offline_card, overall_status,
                   parse_cards, parse_lines, parse_tiles)
from gestures import (
    LONG_PRESS,
    SWIPE_LEFT,
    SWIPE_RIGHT,
    TAP,
    GestureRecogniser,
)
from home import (HomeScroll, SOURCE_HEIGHT, SOURCE_TOP,
                  alert_settings, alerts_flag, alerts_state, overflow_rows, row_at,
                  source_rows)
from net import (
    StatusClient,
    StatusError,
    Unauthorised,
    NetworkUnavailable,
)
from popup import PopToTop
from ticks import Uptime
from ui import (Palette, bell_hit, build_home_layout, dot_hit, draw_boot,
                draw_card, draw_sources, draw_message)


# How long a detail view stays open before returning to the card.
DETAIL_TIMEOUT_MS = 15000

# The shortest demo states fit between normal 15-second polls.
DEMO_POLL_INTERVAL_S = 3

# How long the optimistic bell state is held before the payload wins again.
ALERT_HOLD_MS = 20000
# How long a one-line message stays above the dots.
ALERT_MESSAGE_MS = 4000
# The messages are drawn in the detail line's font, which fits 34 characters.
ALERT_MESSAGE_MAX = 34
SAVE_FAILED_MESSAGE = "COULD NOT SAVE - DAEMON OFFLINE"
ALERTS_OFF_MESSAGE = "ALERTS OFF FOR ALL - SEE ADMIN"


def _now_ms():
    return time.ticks_ms()


def _ticks_diff(a, b):
    """``a - b`` in milliseconds, safe across a ticks rollover."""
    return time.ticks_diff(a, b)


class App(object):
    """The Presto application.

    ``presto`` is the Pimoroni Presto instance. It is only touched inside this
    class, which keeps the rest of the app testable.
    """

    def __init__(self, presto):
        self.presto = presto
        self.display = presto.display
        self.palette = Palette(self.display)

        self.full_res = bool(getattr(config, "FULL_RES", False))
        self.width, self.height = self.display.get_bounds()

        self.led_brightness = float(getattr(config, "LED_BRIGHTNESS", 0.35))
        self.led_dim_when_muted = bool(
            getattr(config, "LED_DIM_WHEN_MUTED", True))

        self.sound_enabled = bool(getattr(config, "SOUND_ENABLED", True))
        self.sound_volume = float(getattr(config, "SOUND_VOLUME", 0.15))
        self.chirp_hz = int(getattr(config, "CHIRP_HZ", 880))
        self.double_chirp_hz = int(getattr(config, "DOUBLE_CHIRP_HZ", 1320))
        self.chirp_ms = int(getattr(config, "CHIRP_MS", 90))
        self.sound_repeat = bool(getattr(config, "SOUND_REPEAT", False))

        self.long_press_ms = int(getattr(config, "LONG_PRESS_MS", 700))
        self.swipe_min_px = int(getattr(config, "SWIPE_MIN_PX", 40))
        self.tap_debounce_ms = int(getattr(config, "TAP_DEBOUNCE_MS", 180))

        self.offline_after_s = int(getattr(config, "OFFLINE_AFTER_S", 30))
        self.poll_interval = max(1, int(getattr(config, "POLL_INTERVAL", 15)))
        self.retry_interval = max(1, int(getattr(config, "RETRY_INTERVAL", 12)))
        self.wifi_timeout = max(1, int(getattr(config, "WIFI_TIMEOUT", 10)))
        self.client = StatusClient(
            getattr(config, "DAEMON_HOST", "home-server"),
            int(getattr(config, "DAEMON_PORT", 8787)),
            getattr(config, "SHARED_TOKEN", ""),
            timeout=max(1, int(getattr(config, "HTTP_TIMEOUT", 8))),
            # The small summary: the card's lines and tiles are fetched when
            # the card is opened, so one poll stays small.
            path="/api/v1/status?cards=lazy")

        self.gestures = GestureRecogniser(
            long_press_ms=self.long_press_ms,
            swipe_min_px=self.swipe_min_px,
            debounce_ms=self.tap_debounce_ms,
        )
        self.alerts = AlertTracker(clock=lambda: _now_ms(),
                                   ticks_diff=lambda a, b: _ticks_diff(a, b))
        # A counter that goes on through the 12-day rollover of the ticks. The
        # gestures, the pop-to-top and the offline timer subtract times, and
        # raw ticks make that wrong at the wrap or after six days.
        self.uptime = Uptime(lambda: _now_ms(), lambda a, b: _ticks_diff(a, b))
        # The alarm pop-to-top: Home scrolls to a service that got worse.
        self.popup = PopToTop()
        # What the daemon says about alerts, and whether it is quiet now.
        self.quiet = False
        self.alert_sound = True
        self.pop_to_top = True
        # The master switch: with it off, no service alerts at all.
        self.alerts_master = True
        # What each service's alerts are doing, as the payload says.
        self.service_alerts = {}
        # The optimistic state of a bell the user has just tapped, and the
        # services whose write is still on its way.
        self.alert_holds = {}
        self.alert_inflight = set()
        self.alert_message = None
        # The lazily fetched detail of the open card: its tiles and its
        # lines. Only the open card's detail is kept.
        self.card_details = {}
        self.card_pending = set()
        self.card_failed = set()
        # The LED shows the worst of every service, not just the open card.
        self.overall_accent = None
        # The payload says the reading is sample data (a daemon in demo
        # mode); the screen then shows a chip. Strictly a boolean: anything
        # else is treated as real data.
        self.demo = False

        self.cards = []
        self.index = 0
        self.home_scroll = HomeScroll(0)
        self.home_layout = None
        self.live_rows = None
        self.card_rows = {}
        self._touch_x = 0
        self._touch_y = 0
        self.detail_open = False
        self.detail_opened_at = 0
        self.error = None
        self.last_good_at = None
        self.connection_error = None
        self.fail_count = 0
        self.wifi_drops = 0
        self.started_at = None
        self._last_screen = None
        # The kind of error the main loop last raised, so it is shown once.
        self._loop_error = None

        self._buzzer = None
        self._needs_redraw = True

    # -- hardware helpers ---------------------------------------------------

    def _setup_buzzer(self):
        """The Presto piezo is on GPIO 43."""
        try:
            from presto import Buzzer
            self._buzzer = Buzzer(43)
        except Exception:
            self._buzzer = None

    def _set_leds(self, rgb):
        for i in range(self.presto.NUM_LEDS):
            self.presto.set_led_rgb(i, rgb[0], rgb[1], rgb[2])

    def _play_cue(self, cue):
        """Play a sound cue. Quiet by design."""
        if not self.sound_enabled or not self.alert_sound or self.quiet:
            return
        if self._buzzer is None:
            return
        from alerts import chirp_count

        count = chirp_count(cue)
        if count == 0:
            return

        hz = self.chirp_hz if count == 1 else self.double_chirp_hz
        for _ in range(count):
            self._buzzer.set_tone(hz, self.sound_volume)
            time.sleep_ms(self.chirp_ms)
            self._buzzer.set_tone(-1)
            time.sleep_ms(60)

    # -- data ---------------------------------------------------------------

    def _set_payload(self, payload):
        selected = self._current_card().id if self.cards else None
        cards, error = parse_cards(payload)
        self.cards = cards
        self.error = error
        self.demo = payload.get("demo") is True
        if error is None:
            self.last_good_at = self.uptime.ms()
            # The rows carry the structured quota of every service, so a
            # card can draw its windows the way Home draws them.
            self.live_rows = source_rows(payload)
            self.card_rows = {}
            for row in self.live_rows or ():
                if row.get("id"):
                    self.card_rows[row["id"]] = row
            # The list scrolls by its total height: quota rows and the
            # compact local rows are not the same height. The overflow
            # strip takes its band out of the list, and the band is
            # decided by the layout, not by the scroll position.
            self.home_layout = build_home_layout(self.display, self.live_rows or [],
                                                 overflow_rows(payload), self.width)
            self.home_scroll.set_height(self.home_layout.total, SOURCE_TOP,
                                        SOURCE_HEIGHT, self.home_layout.region)
        self.index = next((i for i, card in enumerate(cards) if card.id == selected), 0)
        if selected is not None and self._current_card().id != selected:
            self.detail_open = False
        self._free_details()
        if error is None:
            self._apply_alerts(payload)
        self._needs_redraw = True

    def _apply_alerts(self, payload):
        """Sound, LED and pop-to-top decisions for a new payload.

        The daemon sends the alert settings; the device trusts nothing but
        a real boolean, and falls back to its own defaults for the rest.

        A service whose own alerts are off takes no part: it makes no
        sound, it does not colour the light, and it never moves the list.
        Its numbers and its own card are unchanged.
        """
        settings = alert_settings(payload)
        self.quiet = settings["quiet"]
        self.alert_sound = settings["sound"]
        self.pop_to_top = settings["pop_to_top"]
        self.alerts_master = settings["enabled"]
        # The mute length applies to the next mute: a mute that is running
        # keeps the length it started with.
        self.alerts.set_mute_hours(settings["mute_hours"])
        self.alerts.quiet = self.quiet
        statuses = {}
        alerting = {}
        sources = payload.get("sources") if isinstance(payload, dict) else None
        if not isinstance(sources, list):
            sources = []
        for source in sources:
            service = clean_id(source.get("id")) if isinstance(source, dict) else None
            if service:
                statuses[service] = normalise_status(source.get("status"))
                if alerts_flag(source):
                    alerting[service] = statuses[service]
        if not statuses:
            # An older daemon sends no sources: the card carries the status.
            card = self._current_card()
            statuses = {card.id: card.status}
            alerting = dict(statuses)
        # The per-service switch, for the bell on the card.
        self.service_alerts = {}
        for source in sources:
            service = clean_id(source.get("id")) if isinstance(source, dict) else None
            if service:
                state = alerts_state(source)
                if state is not None:
                    self.service_alerts[service] = state
        # A service whose alerts were turned off stops counting at once:
        # its baseline goes with it, so turning them back on starts from
        # the status as it is now and never chirps for an old alarm.
        for service in list(self.alerts.statuses):
            if service not in alerting:
                self.alerts.forget(service)
                self.popup.forget(service)
        # The LED is a status meter: it shows the worst service, not only
        # the one the user happens to be looking at.
        self.overall_accent = worst_accent(alerting.values())
        for _service, cue in self.alerts.update(alerting):
            self._play_cue(cue)
        now = self.uptime.seconds()
        for service, status in alerting.items():
            self.popup.observe(service, status, now)
        self._confirm_holds()

    def _confirm_holds(self):
        """A payload that agrees with a pending write confirms it."""
        for service in list(self.alert_holds):
            hold = self.alert_holds[service]
            if service in self.service_alerts and \
                    self.service_alerts[service] == hold["enabled"]:
                del self.alert_holds[service]

    def _card_alerts(self, card):
        """The bell state of a card, or None when there is no bell.

        The bell is on a service's own card: never on Home, never on the
        summary card, and never while a detail view is open.
        """
        if card is None or card.id == "dash" or self.detail_open:
            return None
        if card.id not in self.service_alerts:
            return None
        enabled = self.service_alerts[card.id]
        hold = self.alert_holds.get(card.id)
        if hold is not None:
            # The optimistic state, until the payload confirms it.
            enabled = hold["enabled"]
        return {"enabled": bool(enabled), "master_off": not self.alerts_master}

    def _bell_tap(self):
        """A tap on the bell switches one service's alerts.

        Returns True when the tap was the bell's, so that it never opens
        the card's detail as well.
        """
        if self.detail_open or self._connection_screen() is not None:
            return False
        card = self._current_card()
        state = self._card_alerts(card)
        if state is None:
            return False
        if not bell_hit(self._touch_x, self._touch_y, self.width, self.height):
            return False
        if state["master_off"]:
            # The master switch is off: nothing can be turned on here.
            self._set_alert_message(ALERTS_OFF_MESSAGE)
            return True
        service = card.id
        if service in self.alert_inflight:
            # A write for this service is still on its way: ignore it.
            return True
        enabled = not state["enabled"]
        self.alert_holds[service] = {"enabled": enabled, "at": _now_ms()}
        self.alert_inflight.add(service)
        self._needs_redraw = True
        self._start_alert_write(service, enabled)
        return True

    def _start_alert_write(self, service, enabled):
        """Send the change to the daemon without blocking the touch loop."""
        try:
            import asyncio
            asyncio.create_task(self._write_alert(service, enabled))
        except Exception:
            self._fail_alert_write(service)

    async def _write_alert(self, service, enabled):
        """The device's one write: one service's alerts on or off."""
        response = None
        try:
            response = await self.client.post_alert(service, enabled)
        except Exception:
            response = None
        self.alert_inflight.discard(service)
        if not isinstance(response, dict) or not response.get("ok"):
            self._fail_alert_write(service)
            return
        # The optimistic state stays until a payload confirms it: a payload
        # that was fetched before the write still carries the old value.
        if type(response.get("alerts")) is bool:
            self.service_alerts[service] = response["alerts"]
        if type(response.get("master")) is bool:
            self.alerts_master = response["master"]
        self._needs_redraw = True

    def _fail_alert_write(self, service):
        """The write did not happen: show the old state and say why."""
        self.alert_inflight.discard(service)
        self.alert_holds.pop(service, None)
        self._set_alert_message(SAVE_FAILED_MESSAGE)

    def _set_alert_message(self, text):
        """Show a one-line message above the dots for a few seconds."""
        text = str(text)[:ALERT_MESSAGE_MAX]
        self.alert_message = (text, _now_ms())
        self._needs_redraw = True

    def _expire_alerts(self):
        """Drop an expired hold or message; the payload wins again."""
        changed = False
        now = _now_ms()
        for service in list(self.alert_holds):
            if _ticks_diff(now, self.alert_holds[service]["at"]) > ALERT_HOLD_MS:
                del self.alert_holds[service]
                changed = True
        if self.alert_message is not None:
            _text, started = self.alert_message
            if _ticks_diff(now, started) > ALERT_MESSAGE_MS:
                self.alert_message = None
                changed = True
        # The mute lifts itself when its time is up. It says nothing: the
        # statuses were tracked while it was on.
        if self.alerts.mute_expired():
            changed = True
        return changed

    async def poll_once(self):
        """Reconnect when needed and accept only a usable authenticated payload."""
        try:
            if not self.presto.wifi.isconnected():
                self.wifi_drops += 1
                # The carousel runs us from /ai_dash_files. Firmware Wi-Fi
                # imports root /secrets.py lazily when (re)connecting.
                if "/" not in sys.path:
                    sys.path.append("/")
                connected = await asyncio.wait_for(
                    self.presto.wifi.connect(timeout=self.wifi_timeout, retries=1),
                    self.wifi_timeout + 1)
                if not connected:
                    self._poll_failed("No network", "Check Wi-Fi settings")
                    return False
        except Exception:
            self._poll_failed("No network", "Check /secrets.py and Wi-Fi")
            return False
        try:
            payload = await self.client.fetch_async()
            _, error = parse_cards(payload)
            if error:
                raise StatusError("invalid cards")
        except Unauthorised:
            self._poll_failed("Unauthorised", "Check shared token", immediate=True)
            return False
        except StatusError as exc:
            # Retain recent valid readings during a failed refresh, as for a
            # connection interruption. Never replace them with invalid data.
            print("AI Dash: rejected refresh: %s" % exc)
            self._poll_failed("Bad data", str(exc))
            return False
        except NetworkUnavailable as exc:
            reason = getattr(exc, "reason", None) or "no reason"
            self._note_failure(reason)
            self._poll_failed("Daemon offline", reason)
            return False
        saved = self._display_state()
        try:
            self._set_payload(payload)
        except Exception as exc:
            # The cards parsed, but something in the payload is not what the
            # app can use. Keep what was on screen, say so, and carry on: a
            # poll that raised would otherwise end the poller for good while
            # the screen kept showing old numbers as if they were current.
            self._restore_display_state(saved)
            reason = type(exc).__name__
            print("AI Dash: rejected refresh: %s" % reason)
            self._poll_failed("Bad data", reason)
            return False
        self.connection_error = None
        self.fail_count = 0
        return True

    def _display_state(self):
        """What a payload replaces, so a refresh that fails can be put back."""
        return (self.cards, self.error, self.last_good_at, self.live_rows,
                self.card_rows, self.home_layout, self.index, self.detail_open,
                self.demo)

    def _restore_display_state(self, saved):
        (self.cards, self.error, self.last_good_at, self.live_rows,
         self.card_rows, self.home_layout, self.index, self.detail_open,
         self.demo) = saved

    def _note_failure(self, reason):
        """Count the failure and log it to the serial console, not the screen."""
        self.fail_count += 1
        print("AI Dash: fetch failed (%d): %s; %s" %
              (self.fail_count, reason, " | ".join(self._diag_lines())))

    def _diag_lines(self):
        """Short ASCII lines on the link and memory, for the offline screen.

        Every probe is optional: the firmware wrapper and MicroPython differ,
        and a diagnostic must never raise.
        """
        if self.started_at is None:
            self.started_at = self.uptime.ms()
        up = (self.uptime.ms() - self.started_at) // 1000
        lines = ["fails %d  up %dh%02dm" % (self.fail_count, up // 3600, up // 60 % 60)]
        parts = []
        try:
            import gc
            free = getattr(gc, "mem_free", None)
            if free:
                parts.append("free %dK" % (free() // 1024))
        except Exception:
            pass
        try:
            parts.append("wifi " + ("up" if self.presto.wifi.isconnected() else "DOWN"))
        except Exception:
            pass
        try:
            import network
            wlan = network.WLAN(network.STA_IF)
        except Exception:
            wlan = None
        if wlan is not None:
            try:
                parts.append("st %s" % (wlan.status(),))
            except Exception:
                pass
            try:
                parts.append("rssi %s" % (wlan.status("rssi"),))
            except Exception:
                pass
        if self.wifi_drops:
            parts.append("drops %d" % self.wifi_drops)
        if parts:
            lines.append("  ".join(parts))
        return lines

    def _poll_failed(self, title, message, immediate=False):
        self.connection_error = (title, message, immediate)
        self._needs_redraw = True

    def _connection_screen(self):
        if self.connection_error is None:
            return None
        if (self.connection_error[2] or self.last_good_at is None or
                self.uptime.ms() - self.last_good_at >= self.offline_after_s * 1000):
            return self.connection_error[:2]
        return None

    async def _network_loop(self):
        while True:
            try:
                good = await self.poll_once()
            except Exception as exc:
                # Whatever went wrong, the next poll must still happen.
                reason = type(exc).__name__
                print("AI Dash: poll failed: %s" % reason)
                self._poll_failed("Poll error", reason)
                good = False
            await asyncio.sleep(self._poll_delay(good))

    def _poll_delay(self, good=True):
        """Keep short demo scenes visible and restore live cadence on exit."""
        if not good:
            return self.retry_interval
        return min(self.poll_interval, DEMO_POLL_INTERVAL_S) if self.demo else self.poll_interval

    # -- interaction --------------------------------------------------------

    def _current_card(self):
        if not self.cards:
            return offline_card("no data")
        return self.cards[self.index]

    def _next_card(self):
        if self.cards:
            self.index = (self.index + 1) % len(self.cards)
        self._free_details()
        self._needs_redraw = True

    def _prev_card(self):
        if self.cards:
            self.index = (self.index - 1) % len(self.cards)
        self._free_details()
        self._needs_redraw = True

    def _free_details(self):
        """The parsed detail of a card that is closed is freed.

        The device has little memory, and a card the user has left is not
        worth keeping. The card that is open keeps its detail for one poll
        interval.
        """
        current = self._current_card().id if self.cards else None
        for card_id in list(self.card_details):
            if card_id != current:
                del self.card_details[card_id]

    def _maybe_load_detail(self):
        """Fetch the open card's detail, at most once per poll interval."""
        if not self.cards:
            return
        card = self._current_card()
        if card.id == "dash":
            return
        detail = self.card_details.get(card.id)
        now = _now_ms()
        if detail is not None:
            if _ticks_diff(now, detail["at"]) <= self._poll_delay() * 1000:
                return
            # Older than one poll interval: fetch it again.
            del self.card_details[card.id]
        if card.id in self.card_pending:
            return
        self.card_pending.add(card.id)
        self.card_failed.discard(card.id)
        self._needs_redraw = True
        try:
            import asyncio
            asyncio.create_task(self._load_card_detail(card.id))
        except Exception:
            self.card_pending.discard(card.id)
            self.card_failed.add(card.id)

    async def _load_card_detail(self, card_id):
        """One card's tiles and lines. A read: nothing is changed."""
        detail = None
        try:
            detail = await self.client.fetch_card(card_id)
        except Exception:
            detail = None
        self.card_pending.discard(card_id)
        if not isinstance(detail, dict):
            self.card_failed.add(card_id)
            self._needs_redraw = True
            return
        # A response for a card that is no longer open is dropped: the user
        # swiped away while it was on its way.
        if card_id != self._current_card().id:
            return
        tiles = detail.get("tiles")
        lines = detail.get("lines")
        # The same limits as the tiles and lines in the summary: the detail
        # is as untrusted as the poll, and the drawing code expects text.
        self.card_details[card_id] = {
            "tiles": parse_tiles(tiles),
            "lines": parse_lines(lines),
            "at": _now_ms()}
        self.card_failed.discard(card_id)
        self._needs_redraw = True

    def _open_detail(self):
        self.detail_open = True
        self.detail_opened_at = _now_ms()
        self._needs_redraw = True

    def _close_detail(self):
        self.detail_open = False
        self._needs_redraw = True

    def _tap_navigates(self):
        """A tap on a page dot or a Home row goes to that card.

        Swipes and scrolls never reach here: the recogniser reports a tap only
        for a short press that stayed within the drag slop. Returns True when
        the tap was used.
        """
        if self._connection_screen() is not None or not self.cards:
            return False
        x, y = self._touch_x, self._touch_y
        target = dot_hit(x, y, self.index, len(self.cards), self.width, self.height)
        if target is None and self._current_card().id == "dash" and self.home_layout:
            row = row_at(self.home_layout, self.home_scroll.offset, y)
            if row is not None:
                target = next((i for i, card in enumerate(self.cards)
                               if card.id == row.get("id")), None)
        if target is None:
            return False
        self.index = target
        self.detail_open = False
        self._free_details()
        self._needs_redraw = True
        return True

    def _handle_gestures(self, gestures):
        for gesture in gestures:
            if gesture == SWIPE_LEFT:
                self._next_card()
            elif gesture == SWIPE_RIGHT:
                self._prev_card()
            elif gesture == TAP:
                if self._bell_tap():
                    pass
                elif self._tap_navigates():
                    pass
                elif self._current_card().id == "dash":
                    # Both periods are always visible on Home.
                    self._needs_redraw = True
                elif self.detail_open:
                    self._close_detail()
                else:
                    self._open_detail()
            elif gesture == LONG_PRESS:
                # A long press while muted unmutes at once; otherwise it
                # starts a mute that ends after the mute length.
                muted = self.alerts.toggle_mute()
                if muted:
                    self._close_detail()
                self._needs_redraw = True

    def _poll_touch(self):
        """Read the touchscreen and feed the recogniser."""
        touch = self.presto.touch
        touch.poll()
        state = bool(touch.state)
        now = self.uptime.seconds()
        if state:
            self._touch_x = touch.x
            self._touch_y = touch.y
            # The user is at the screen: the list must not move under them.
            self.popup.touch(now)
        previous_offset = self.home_scroll.offset
        self.home_scroll.update(state, self._touch_x, self._touch_y,
                                enabled=self._current_card().id == "dash" and
                                self._connection_screen() is None)
        if self.home_scroll.offset != previous_offset:
            # The user scrolled: that also cancels a pending return.
            self.popup.scrolled(now)
            self._needs_redraw = True
        # Use the final touched position on release, rather than resetting to
        # (0, 0), so left/right swipes use the actual gesture.
        return self.gestures.update(state, self._touch_x, self._touch_y, self.uptime.ms())

    def _poll_popup(self):
        """Move Home to an alarmed service, and back to the user's position.

        Home only: never on a card, never on a detail view, and never while
        the daemon connection is broken.
        """
        if not self.pop_to_top or self.home_layout is None:
            return
        if self._current_card().id != "dash" or self.detail_open:
            return
        if self.connection_error is not None:
            return
        now = self.uptime.seconds()
        action = self.popup.poll(now, self.home_layout, self.home_scroll.offset)
        if action is None:
            return
        offset = max(0, min(int(action["offset"]), self.home_scroll.limit))
        self.home_scroll.offset = offset
        self._needs_redraw = True

    # -- rendering ----------------------------------------------------------

    def _update_leds(self):
        card = self._current_card()
        if self.connection_error:
            accent = "red" if self._connection_screen() else "grey"
        elif self.live_rows is not None and not self.live_rows:
            # No service is set up: amber says "attention", where green would
            # say "all healthy". The payload is unchanged; this is the device
            # reading an empty list the same way it reads the empty screen.
            accent = "yellow"
        else:
            # The worst of every service, so a warning on another account is
            # still visible from across the desk.
            accent = self.overall_accent or card.accent
        rgb = led_pattern(
            accent,
            brightness=led_brightness(self.led_brightness, self.quiet),
            muted=self.alerts.muted,
            dim_when_muted=self.led_dim_when_muted,
        )
        self._set_leds(rgb)

    def _render(self):
        card = self._current_card()
        self._maybe_load_detail()

        screen = self._connection_screen()
        if screen:
            draw_message(self.display, self.palette, screen[0], screen[1],
                         detail=["Retrying..."] + self._diag_lines() +
                         ["RESET returns to carousel"], demo=self.demo)
        elif card.id == "dash" and self.live_rows is not None:
            draw_sources(self.display, self.palette, self.live_rows,
                         self.index, len(self.cards), status=card.status,
                         muted=self.alerts.muted, width=self.width, height=self.height,
                         scroll=self.home_scroll.offset, stale=bool(self.connection_error),
                         layout=self.home_layout,
                         mute_text=self.alerts.mute_label(),
                         demo=self.demo)
        else:
            row = self.card_rows.get(card.id)
            detail = self.card_details.get(card.id)
            draw_card(
                self.display, self.palette, card,
                self.index, len(self.cards),
                muted=self.alerts.muted,
                width=self.width, height=self.height,
                stale=bool(self.connection_error),
                alerts=self._card_alerts(card),
                message=(self.alert_message[0] if self.alert_message else None),
                quota=(row.get("windows") if row else None),
                marks=(row.get("marks") if row else None),
                tiles=(detail.get("tiles") if detail else None),
                detail=(detail.get("lines") if detail else None),
                loading=(card.id in self.card_pending),
                failed=(card.id in self.card_failed),
                detail_open=self.detail_open,
                mute_text=self.alerts.mute_label(),
                demo=self.demo,
            )

        self.presto.update()
        self._needs_redraw = False
        self._last_screen = screen

    def _loop_failed(self, exc):
        """One pass of the main loop raised. Say so once, and keep running.

        An error that ended the loop would end the app, and it would repeat on
        every pass if left to. So the screen says what failed, and the loop
        tries again when something changes.
        """
        name = type(exc).__name__
        self._needs_redraw = False
        if name == self._loop_error:
            return
        self._loop_error = name
        print("AI Dash: screen error: %s" % name)
        try:
            draw_message(self.display, self.palette, "Display error", name,
                         detail=["Retrying...", "RESET returns to carousel"], demo=self.demo)
            self.presto.update()
        except Exception:
            pass

    def _render_offline(self, message):
        from alerts import rgb_for_accent
        draw_message(
            self.display, self.palette,
            "Daemon offline", message,
            accent_rgb=rgb_for_accent("red"),
            detail=["Retrying...", "Check daemon and Wi-Fi"],
            demo=self.demo,
        )
        self.presto.update()

    # -- main loop ----------------------------------------------------------

    def run(self):
        """Run the app against the daemon; there is no offline sample mode."""
        self.started_at = self.uptime.ms()
        self._setup_buzzer()
        self._poll_failed("Connecting...", "Joining Wi-Fi")
        asyncio.run(self._run_live())

    def _tick(self):
        """One pass of the main loop: touch, timers, and a redraw if needed.

        The device calls it every 20 ms from _run_live. The documentation
        site's demo calls it from a browser timer, so the page runs this same
        pass instead of a copy of it.
        """
        try:
            self._handle_gestures(self._poll_touch())
            self._poll_popup()
            if self._expire_alerts():
                self._needs_redraw = True
            if self._last_screen != self._connection_screen():
                self._needs_redraw = True
            if self.detail_open and _ticks_diff(_now_ms(), self.detail_opened_at) > DETAIL_TIMEOUT_MS:
                self._close_detail()
            if self._needs_redraw:
                self._update_leds()
                self._render()
                # Only a draw that worked clears the error, not a pass
                # with nothing to draw.
                self._loop_error = None
        except Exception as exc:
            self._loop_failed(exc)

    async def _run_live(self):
        poller = asyncio.create_task(self._network_loop())
        try:
            while True:
                self._tick()
                await asyncio.sleep(0.02)
        finally:
            poller.cancel()
            try:
                await poller
            except asyncio.CancelledError:
                pass


def main():
    from presto import Presto

    presto = Presto(full_res=bool(getattr(config, "FULL_RES", False)))
    display = presto.display
    palette = Palette(display)

    draw_boot(display, palette, "Starting...")
    presto.update()

    app = App(presto)
    app.run()


if __name__ == "__main__":
    main()
