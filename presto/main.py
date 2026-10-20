# ICON monitoring
# NAME AI Dash
# DESC A tiny AI usage dashboard for Pimoroni Presto.

"""Presto AI Dash - Presto application.

The only module in this app that imports ``presto`` or ``machine``. Everything
else (cards, gestures, alerts, net, ui) is pure Python and is unit-tested on a
development machine. See docs/limitations.md for what cannot be automated.

Select static preview or authenticated daemon polling with STATIC_DEMO.
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
        # The launcher should always deploy config.py. A missing config safely
        # starts the static demo, rather than crashing on a dotted filename.
        STATIC_DEMO = True

from alerts import AlertTracker, led_pattern
from cards import error_card, offline_card, overall_status, parse_cards
from gestures import (
    LONG_PRESS,
    SWIPE_LEFT,
    SWIPE_RIGHT,
    TAP,
    GestureRecogniser,
)
from home import (DEMO_QUOTAS, HomeScroll, ROW_HEIGHT, SOURCE_HEIGHT, SOURCE_TOP,
                  overflow_rows, source_rows)
from net import (
    StatusClient,
    StatusError,
    Unauthorised,
    NetworkUnavailable,
    embedded_payload,
    load_static_payload,
)
from ui import (Palette, build_home_layout, draw_boot, draw_card, draw_home,
                draw_sources, draw_message)


# Where to look for a sample payload, in order.
SAMPLE_PATHS = ("sample-status.json", "/sample-status.json")

# How long a detail view stays open before returning to the card.
DETAIL_TIMEOUT_MS = 15000


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
        self.static_demo = bool(getattr(config, "STATIC_DEMO", True))
        self.poll_interval = max(1, int(getattr(config, "POLL_INTERVAL", 15)))
        self.retry_interval = max(1, int(getattr(config, "RETRY_INTERVAL", 12)))
        self.wifi_timeout = max(1, int(getattr(config, "WIFI_TIMEOUT", 10)))
        self.client = StatusClient(
            getattr(config, "DAEMON_HOST", "toontown-int-srv1"),
            int(getattr(config, "DAEMON_PORT", 8787)),
            getattr(config, "SHARED_TOKEN", ""),
            timeout=max(1, int(getattr(config, "HTTP_TIMEOUT", 8))))

        self.gestures = GestureRecogniser(
            long_press_ms=self.long_press_ms,
            swipe_min_px=self.swipe_min_px,
            debounce_ms=self.tap_debounce_ms,
        )
        self.alerts = AlertTracker()

        self.cards = []
        self.index = 0
        self.home_scroll = HomeScroll(len(DEMO_QUOTAS) * ROW_HEIGHT)
        self.home_layout = None
        self._touch_x = 0
        self._touch_y = 0
        self.detail_open = False
        self.detail_opened_at = 0
        self.error = None
        self.last_good_at = None
        self.live_rows = None
        self.connection_error = None
        self._last_screen = None

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
        if not self.sound_enabled or self._buzzer is None:
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

    def load_static(self):
        """Load sample data for the static demo.

        Tries a file first, then falls back to the embedded sample so the
        device always has something to show.
        """
        for path in SAMPLE_PATHS:
            try:
                payload = load_static_payload(path)
                self._set_payload(payload)
                return True
            except StatusError:
                continue

        self._set_payload(embedded_payload())
        return True

    def _set_payload(self, payload):
        selected = self._current_card().id if self.cards else None
        cards, error = parse_cards(payload)
        self.cards = cards
        self.error = error
        if error is None:
            self.last_good_at = _now_ms()
            if not self.static_demo:
                self.live_rows = source_rows(payload)
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
        self._needs_redraw = True

    async def poll_once(self):
        """Reconnect when needed and accept only a usable authenticated payload."""
        try:
            if not self.presto.wifi.isconnected():
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
        except NetworkUnavailable:
            self._poll_failed("Daemon offline", "Check daemon and LAN address")
            return False
        self.connection_error = None
        self._set_payload(payload)
        return True

    def _poll_failed(self, title, message, immediate=False):
        self.connection_error = (title, message, immediate)
        self._needs_redraw = True

    def _connection_screen(self):
        if self.static_demo or self.connection_error is None:
            return None
        if (self.connection_error[2] or self.last_good_at is None or
                _ticks_diff(_now_ms(), self.last_good_at) >= self.offline_after_s * 1000):
            return self.connection_error[:2]
        return None

    async def _network_loop(self):
        while True:
            good = await self.poll_once()
            await asyncio.sleep(self.poll_interval if good else self.retry_interval)

    # -- interaction --------------------------------------------------------

    def _current_card(self):
        if not self.cards:
            return offline_card("no data")
        return self.cards[self.index]

    def _next_card(self):
        if self.cards:
            self.index = (self.index + 1) % len(self.cards)
        self._needs_redraw = True

    def _prev_card(self):
        if self.cards:
            self.index = (self.index - 1) % len(self.cards)
        self._needs_redraw = True

    def _open_detail(self):
        self.detail_open = True
        self.detail_opened_at = _now_ms()
        self._needs_redraw = True

    def _close_detail(self):
        self.detail_open = False
        self._needs_redraw = True

    def _handle_gestures(self, gestures):
        for gesture in gestures:
            if gesture == SWIPE_LEFT:
                self._next_card()
            elif gesture == SWIPE_RIGHT:
                self._prev_card()
            elif gesture == TAP:
                if self._current_card().id == "dash":
                    # Both periods are always visible on Home.
                    self._needs_redraw = True
                elif self.detail_open:
                    self._close_detail()
                else:
                    self._open_detail()
            elif gesture == LONG_PRESS:
                muted = self.alerts.toggle_mute()
                if muted:
                    self._close_detail()
                self._needs_redraw = True

    def _poll_touch(self):
        """Read the touchscreen and feed the recogniser."""
        touch = self.presto.touch
        touch.poll()
        state = bool(touch.state)
        if state:
            self._touch_x = touch.x
            self._touch_y = touch.y
        previous_offset = self.home_scroll.offset
        self.home_scroll.update(state, self._touch_x, self._touch_y,
                                enabled=self._current_card().id == "dash" and
                                self._connection_screen() is None)
        if self.home_scroll.offset != previous_offset:
            self._needs_redraw = True
        # Use the final touched position on release, rather than resetting to
        # (0, 0), so left/right swipes use the actual gesture.
        return self.gestures.update(state, self._touch_x, self._touch_y, _now_ms())

    # -- rendering ----------------------------------------------------------

    def _update_leds(self):
        card = self._current_card()
        accent = ("red" if self._connection_screen() else "grey") if self.connection_error else card.accent
        rgb = led_pattern(
            accent,
            brightness=self.led_brightness,
            muted=self.alerts.muted,
            dim_when_muted=self.led_dim_when_muted,
        )
        self._set_leds(rgb)

    def _render(self):
        card = self._current_card()

        screen = self._connection_screen()
        if screen:
            draw_message(self.display, self.palette, screen[0], screen[1],
                         detail=["Retrying...", "RESET returns to carousel"])
        elif card.id == "dash" and self.static_demo:
            draw_home(self.display, self.palette, DEMO_QUOTAS,
                      self.index, len(self.cards), muted=self.alerts.muted,
                      width=self.width, height=self.height,
                      scroll=self.home_scroll.offset)
        elif card.id == "dash" and self.live_rows is not None:
            draw_sources(self.display, self.palette, self.live_rows,
                         self.index, len(self.cards), status=card.status,
                         muted=self.alerts.muted, width=self.width, height=self.height,
                         scroll=self.home_scroll.offset, stale=bool(self.connection_error),
                         layout=self.home_layout)
        else:
            draw_card(
                self.display, self.palette, card,
                self.index, len(self.cards),
                muted=self.alerts.muted,
                width=self.width, height=self.height,
                stale=bool(self.connection_error),
            )

        self.presto.update()
        self._needs_redraw = False
        self._last_screen = screen

    def _render_offline(self, message):
        from alerts import rgb_for_accent
        draw_message(
            self.display, self.palette,
            "Daemon offline", message,
            accent_rgb=rgb_for_accent("red"),
            detail=["Retrying...", "Check daemon and Wi-Fi"],
        )
        self.presto.update()

    # -- main loop ----------------------------------------------------------

    def run(self):
        """Run the selected mode; live mode never falls back to static values."""
        self._setup_buzzer()
        if not self.static_demo:
            self._poll_failed("Connecting...", "Joining Wi-Fi")
            asyncio.run(self._run_live())
            return
        self.load_static()

        while True:
            gestures = self._poll_touch()
            self._handle_gestures(gestures)

            # A detail view closes itself after a while.
            if self.detail_open:
                if _ticks_diff(_now_ms(), self.detail_opened_at) > DETAIL_TIMEOUT_MS:
                    self._close_detail()

            if self._needs_redraw:
                self._update_leds()
                self._render()

            time.sleep_ms(20)

    async def _run_live(self):
        poller = asyncio.create_task(self._network_loop())
        try:
            while True:
                self._handle_gestures(self._poll_touch())
                if self._last_screen != self._connection_screen():
                    self._needs_redraw = True
                if self.detail_open and _ticks_diff(_now_ms(), self.detail_opened_at) > DETAIL_TIMEOUT_MS:
                    self._close_detail()
                if self._needs_redraw:
                    self._update_leds()
                    self._render()
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
