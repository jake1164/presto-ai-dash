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
import lifecycle
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
    current_device_id,
    current_revision_hex,
)
from popup import PopToTop
from ticks import Uptime
from ui import (Palette, bell_hit, build_home_layout, dot_hit, draw_boot,
                draw_card, draw_lifecycle_view, draw_sources, draw_message,
                lifecycle_hit)


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

# Set by the bootstrap (``presto/bootstrap/boot.py``) when this app runs from an
# A/B slot: the durable journal and this start's context. It stays None for the
# legacy launcher, which has neither a slot nor a journal, so an old install
# never claims a slot it does not have.
BOOT_CONTEXT = None


# The board has no battery-backed clock: every hard reset restarts the RTC at
# 2021-01-01 (measured), which fails the updater's TLS date gate. A warm reset
# keeps RAMFS, so the app carries the time across its own restart here.
CLOCK_CARRY = '/ramfs/ai_dash_clock.txt'
# What the RTC reads at a hard reset: 2021-01-01T00:00:00Z.
RESET_EPOCH = 1609459200


def _mount_ramfs():
    """Mount RAMFS; the stock launcher has usually mounted it already."""
    try:
        import psram
        psram.mkramfs()
    except Exception:
        # A second mount raises EPERM (measured). That is not a failure.
        pass


def _carry_clock():
    """Leave a usable wall-clock time for the app after a warm reset."""
    try:
        from updater import MIN_USABLE_EPOCH
        now = int(time.time())
        if now >= MIN_USABLE_EPOCH:
            with open(CLOCK_CARRY, 'w') as handle:
                handle.write('%d' % now)
    except Exception:
        pass


def _restore_clock():
    """Set an unusable RTC from the time carried across a warm reset.

    Only a reset clock is changed, never a usable one. The seconds the RTC has
    counted since the reset are added back, so the result is close to right.
    The carried value is used once.
    """
    try:
        from updater import MIN_USABLE_EPOCH
        now = int(time.time())
        if now >= MIN_USABLE_EPOCH:
            return
        _mount_ramfs()
        try:
            with open(CLOCK_CARRY, 'r') as handle:
                text = handle.read(16)
        except OSError:
            return
        try:
            import os
            os.remove(CLOCK_CARRY)
        except OSError:
            pass
        carried = int(text)
        if not MIN_USABLE_EPOCH <= carried < 2147483647:
            return
        if RESET_EPOCH <= now < MIN_USABLE_EPOCH:
            carried += now - RESET_EPOCH
        _set_clock(carried)
        print("AI Dash: clock restored after reset")
    except Exception:
        pass


def _set_clock(epoch):
    """Set the RTC to a unix time."""
    import machine
    value = time.gmtime(epoch)
    machine.RTC().datetime((value[0], value[1], value[2], value[6],
                            value[3], value[4], value[5], 0))


def _reboot():
    """Restart the board so the bootstrap enters the staged slot.

    The bootstrap owns the choice of slot; the app only records the candidate
    in the journal first, so a power cut here still leaves a recoverable state.
    The handoff and clock are best effort: the reset happens either way, and
    without the handoff the next launch still finds the candidate.
    """
    try:
        import machine
    except Exception:
        return
    if BOOT_CONTEXT is not None:
        _mount_ramfs()
        try:
            with open('/ramfs/launch.txt', 'w') as handle:
                handle.write('ai_dash_boot.py')
        except Exception:
            pass
        _carry_clock()
    time.sleep_ms(50)
    machine.reset()


class _FlashWindow(object):
    """Internal-flash writes, made with the display's core1 scan-out stopped.

    Measured on the deployed firmware (October 29, 2026): once ``Presto()``
    starts its core1 display driver, the first internal-flash write never
    returns, and the watchdog resets the board. With core1 stopped the same
    write takes milliseconds. Every journal save and slot write the running
    app makes goes through this window; nothing may draw inside it. Windows
    nest, and only the outermost one stops and restarts the display.
    """

    def __init__(self, app):
        self.app = app

    def __enter__(self):
        self.app._flash_depth += 1
        if self.app._flash_depth == 1:
            self.app._pause_display()
        return self

    def __exit__(self, kind, value, trace):
        self.app._flash_depth -= 1
        if self.app._flash_depth == 0:
            self.app._resume_display()
        return False
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
        self.sound_volume = float(getattr(config, "SOUND_VOLUME", 0.3))
        self.chirp_hz = int(getattr(config, "CHIRP_HZ", 3000))
        self.double_chirp_hz = int(getattr(config, "DOUBLE_CHIRP_HZ", 3000))
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
        # The device tells the daemon what it can about itself, so the Status
        # tab can say why a device is unhappy without a serial cable.
        self.client.diag_provider = self._diag_state
        # The lifecycle report: which app revision this device runs, which
        # boot and report this is, and that it is not updating its own app.
        # A device without a generated identity sends none of it, and the
        # daemon keeps its legacy name-and-revision view.
        self.device_id = current_device_id()
        self.seq = 0
        context = BOOT_CONTEXT if isinstance(BOOT_CONTEXT, dict) else None
        self.journal = None
        self._activation = None
        self._store = None
        # OTA remains inert unless an explicit, matching-firmware TLS profile
        # is provisioned outside the replaceable slots. Configure it only
        # after the bootstrap journal and real filesystem are available.
        self.ota = None
        self._watchdog = context.get("watchdog") if context else None
        self._confirmation_started = _now_ms() if self._watchdog is not None else 0
        self._transfer = None
        self._ota_prepare_task = None
        self._ota_slot = None
        self._ota_reset = None
        # The display pause around flash writes (see _FlashWindow).
        self._flash_depth = 0
        self._display_stopped = False
        self.slot = None
        self.bootstrap = None
        self.boot_confirmed = True
        self.app_scheme = lifecycle.SCHEME
        if context is not None and isinstance(context.get("context"), dict):
            # Running from a slot: the bootstrap owns the durable boot count,
            # so this app must not keep a second one.
            self.journal = context.get("journal")
            self.slot = context["context"].get("slot")
            self.bootstrap = context["context"].get("bootstrap")
            self.boot_confirmed = bool(context["context"].get("boot_confirmed"))
            self.boot = int(context["context"].get("boot") or 1)
            self.app_scheme = lifecycle.SCHEME_SLOT
            try:
                import activation
                self._activation = activation
                self._store = activation.FileStore()
            except Exception:
                # A missing journal module costs the slot report, never the
                # dashboard: the daemon then just sees an ordinary revision.
                self._activation = None
        else:
            # The counter is a flash write, and Presto() has already started the
            # display driver. Without the window that first write never returns
            # and the screen stays on "Starting..." (see _FlashWindow).
            if self.device_id:
                with _FlashWindow(self):
                    self.boot = lifecycle.bump_boot(getattr(config, "BOOT_COUNTER_FILE", None))
            else:
                self.boot = 1
        self.client.lifecycle_provider = self._lifecycle_headers
        # A board with no clock battery restarts at 2021; the daemon proves the
        # real time with a nonce, and only while the clock is unusable.
        self._time_nonce = None
        self.client.time_nonce_provider = self._clock_nonce
        if self.journal is not None:
            # The TLS date gate needs a real clock; a candidate's own restart
            # carried it across the reset.
            _restore_clock()
            import ota_transport
            ota_transport.configure(self)

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
        # The failures since boot. The count above is the consecutive one and
        # resets on a good poll; this one does not, so the daemon can show how
        # many times the device has been unable to reach it.
        self.fail_total = 0
        self.wifi_drops = 0
        self.started_at = None
        self._last_screen = None
        # The shared bottom-right indicator: what the daemon says this device
        # or its release needs, and whether the read-only view is open.
        self.lifecycle_notice = None
        self.lifecycle_open = False
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
        # What the shared bottom-right indicator should show: a device problem,
        # a daemon release, both, or nothing. An absent or unusable extension
        # means nothing, so an older daemon costs the screen nothing.
        self.lifecycle_notice = lifecycle.notice(payload)
        if error is None:
            # A complete answer is the only evidence of health this app has;
            # it is what turns a staged slot into the confirmed one.
            self._maybe_confirm(payload)
            self._ota_offer(payload)
        if self.lifecycle_open and self.lifecycle_notice is None:
            # The condition cleared while the view was open: go back to the
            # card the user was on.
            self.lifecycle_open = False
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
                                                 overflow_rows(payload), self.width,
                                                 dock=bool(self.lifecycle_notice))
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
            self.fail_total += 1
            self._poll_failed("Unauthorised", "Check shared token", immediate=True)
            return False
        except StatusError as exc:
            # Retain recent valid readings during a failed refresh, as for a
            # connection interruption. Never replace them with invalid data.
            self.fail_total += 1
            print("AI Dash: rejected refresh: %s" % exc)
            self._poll_failed("Bad data", str(exc))
            return False
        except NetworkUnavailable as exc:
            reason = getattr(exc, "reason", None) or "no reason"
            self._note_failure(reason)
            self._poll_failed("Daemon offline", reason)
            return False
        self._apply_clock(payload)
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

    def _clock_usable(self):
        from updater import MIN_USABLE_EPOCH
        return time.time() >= MIN_USABLE_EPOCH

    def _clock_nonce(self):
        """A fresh nonce to ask the daemon for the time, only while it is needed."""
        if self._clock_usable():
            self._time_nonce = None
            return None
        import clockproof
        self._time_nonce = clockproof.new_nonce()
        return self._time_nonce

    def _apply_clock(self, payload):
        """Set a reset clock from the daemon's authenticated time, then enable OTA.

        Only while the clock is unusable, only for the reply to this device's
        own nonce (used once), and only when the HMAC under the shared token
        verifies, so a forged or replayed reply changes nothing. A clock that
        is already real is never touched. The updater is built at start-up
        only if the clock was real then, so it is built here once it is.
        """
        nonce, self._time_nonce = self._time_nonce, None
        if nonce is None or not isinstance(payload, dict) or self._clock_usable():
            return
        try:
            import clockproof
            from updater import MIN_USABLE_EPOCH
            epoch = clockproof.verify(self.client.token, nonce,
                                      payload.get(clockproof.PAYLOAD_KEY), MIN_USABLE_EPOCH)
            if epoch is None:
                return
            _set_clock(epoch)
            print("AI Dash: clock set from daemon")
            if self.ota is None and self.journal is not None:
                import ota_transport
                ota_transport.configure(self)
        except Exception as exc:
            print("AI Dash: clock not set (%s)" % type(exc).__name__)

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
        self.fail_total += 1
        print("AI Dash: fetch failed (%d): %s; %s" %
              (self.fail_count, reason, " | ".join(self._diag_lines())))

    def _lifecycle_headers(self):
        """The device ID and the lifecycle report for one status poll, or None.

        ``seq`` counts the reports of this run in RAM; ``boot`` is the durable
        count of application starts. Nothing is sent for a device with no
        generated identity, and nothing is sent when the report cannot be
        built: a malformed header would cost the whole poll its lifecycle
        data, which is worse than saying nothing.
        """
        if not self.device_id:
            return None
        self.seq += 1
        text = lifecycle.build(self.device_id, self.boot, self.seq,
                               current_revision_hex(),
                               bootstrap=self.bootstrap, slot=self.slot,
                               boot_confirmed=self.boot_confirmed,
                               caps=["ota-v1"] if self.ota is not None and self.ota.readiness() is None else [],
                               scheme=self.app_scheme,
                               update=self._update_block())
        if text is None:
            return None
        return (self.device_id, text)

    def _update_block(self):
        """What this device is doing to its own app, from the durable journal.

        With no journal there is no updater, so the block is idle. A retained
        terminal attempt keeps reporting until the daemon settles it, because a
        reboot must not turn a failure into a clean idle report.
        """
        if self._activation is not None and self.journal is not None:
            try:
                return self._activation.update_block(self.journal)
            except Exception:
                return None
        return None

    def _ota_offer(self, payload):
        """Stage a permitted update, if there is an updater to do it with.

        ``self.ota`` is only created by ``updater.make_updater`` with a
        verified TLS transport, a provisioned trust root and the measured
        firmware capabilities. Missing provisioning leaves this inert.
        When it does run it only starts a transfer: the bytes are
        verified by hash before anything becomes a candidate.
        """
        if (self.ota is None or self._transfer is not None or self.journal is None
                or self._ota_prepare_task is not None):
            return
        device = payload.get("device_lifecycle") if isinstance(payload, dict) else None
        command = device.get("command") if isinstance(device, dict) else None
        if not isinstance(command, dict) or command.get("kind") != "start_update":
            return
        if command.get("manifest_path") != "/api/v1/device-app/manifest":
            return
        for name in ("transfer", "pending", "failure", "success"):
            recorded = self.journal.get(name)
            if recorded and recorded.get("attempt") == command.get("attempt"):
                return
        if self.journal.get("pending") is not None:
            return
        try:
            import updater
            error = self.ota.readiness()
            if error:
                self._ota_failed(command, error, blocked=True)
                return
            if hasattr(self.ota.transport, 'manifest_async'):
                print('AI Dash: update manifest scheduled')
                self._ota_prepare_task = asyncio.create_task(self._prepare_ota_async(command))
                return
            manifest_text = self.ota.transport.manifest(command.get("manifest_path"))
            self._prepare_ota(command, manifest_text)
        except Exception as exc:
            print("AI Dash: update could not be staged (%s)" % type(exc).__name__)
            self._transfer = None

    async def _prepare_ota_async(self, command):
        try:
            text = await self.ota.transport.manifest_async(command['manifest_path'])
            print('AI Dash: update manifest received')
            self._prepare_ota(command, text)
        except Exception as exc:
            print('AI Dash: update manifest failed (%s)' % type(exc).__name__)
            self._ota_failed(command, 'transport_untrusted', blocked=True)
        finally:
            self._ota_prepare_task = None

    def _prepare_ota(self, command, manifest_text):
        try:
            self.ota.journal = self.journal
            transfer, error = self.ota.prepare(command, manifest_text, self.slot)
            if error:
                self._ota_failed(command, error, blocked=True)
                return
            if transfer.manifest["revision"] == current_revision_hex():
                return
            self._ota_slot = "b" if self.slot == "a" else "a"
            accepted = self._activation.begin_transfer(self.journal, self._ota_slot,
                transfer.manifest["revision"], command["generation"], command["attempt"], command["id"])
            with _FlashWindow(self):
                self._activation.save(self._store, accepted)
            print('AI Dash: update transfer recorded')
            self.journal = accepted
            self._transfer = transfer
        except Exception as exc:
            print("AI Dash: update could not be staged (%s)" % type(exc).__name__)
            self._transfer = None

    def _ota_failed(self, command, error, blocked=False):
        try:
            value = self._activation.record_failure(self.journal,
                "blocked" if blocked else "failed", error, command["target_revision"],
                command["generation"], command["attempt"])
            with _FlashWindow(self):
                self._activation.save(self._store, value)
            self.journal = value
        except Exception:
            pass

    def _ota_tick(self):
        """Advance a staged transfer by one bounded chunk per frame.

        The display keeps updating between steps: one step is at most one
        1024-byte read into RAM. Verified files reach flash in a batch inside
        one display pause (see _FlashWindow); the last batch also verifies
        the slot from flash and records the candidate, and the board restarts
        into it.
        """
        transfer = self._transfer
        if transfer is None:
            return
        try:
            if not transfer.done and not transfer.flash_due:
                transfer.step()
            if not transfer.done and not transfer.flash_due:
                return
            staged = False
            with _FlashWindow(self):
                if not transfer.done:
                    if transfer.downloaded and self.journal["transfer"]["phase"] != "verifying":
                        verifying = dict(self.journal)
                        verifying["transfer"] = dict(self.journal["transfer"], phase="verifying")
                        self._activation.save(self._store, verifying)
                        self.journal = verifying
                    print("AI Dash: update writing slot " + self._ota_slot)
                    transfer.flash(self._service_watchdog)
                if transfer.done:
                    self._transfer = None
                    staged = self._finish_transfer(transfer)
            if staged:
                print("AI Dash: update staged in slot " + self._ota_slot)
                reset = self._ota_reset or _reboot
                reset()
        except Exception as exc:
            print("AI Dash: update step failed (%s)" % type(exc).__name__)
            self._transfer = None

    def _finish_transfer(self, transfer):
        """Record a finished transfer; True when a candidate is ready to enter.

        Called inside a flash window.
        """
        try:
            import json
            self._store.write('/ai_dash_state/ota-metrics.json',
                              json.dumps(transfer.metrics).encode())
        except Exception:
            pass
        if transfer.error is not None:
            print("AI Dash: update staging stopped (%s)" % transfer.error)
            accepted = self.journal.get("transfer")
            if accepted:
                self._ota_failed({"target_revision": accepted["target"],
                    "generation": accepted["generation"], "attempt": accepted["attempt"]}, transfer.error)
            return False
        if self._activation is None:
            return False
        accepted = self.journal["transfer"]
        candidate = self._activation.begin_candidate(self.journal, self._ota_slot,
            transfer.manifest["revision"], accepted["generation"], accepted["attempt"], accepted["command"])
        self._activation.save(self._store, candidate)
        self.journal = candidate
        return True

    def _pause_display(self):
        """Stop the core1 display scan-out so internal flash can be written.

        The native object's own finaliser stops core1 (measured: about 15 ms).
        A stand-in display without one has nothing to stop.
        """
        native = getattr(self.presto, "presto", None)
        stop = getattr(native, "__del__", None)
        if stop is None:
            return
        stop()
        self._display_stopped = True

    def _resume_display(self):
        """Restart the scan-out with a new native object of the same kind.

        It draws from the same static framebuffer, so the existing PicoGraphics
        surface stays valid (measured: about 370 ms). A second finaliser call
        on the old object is a no-op on this firmware (measured), so letting it
        be collected is safe. If the display cannot be restarted the board is
        reset rather than left drawing to a stopped driver.
        """
        if not self._display_stopped:
            return
        try:
            native = self.presto.presto
            replacement = type(native)(full_res=self.full_res, palette=False)
            self.presto.presto = replacement
            if getattr(self.presto, "buffer", None) is not None:
                self.presto.buffer = memoryview(replacement)
            self._display_stopped = False
            self._needs_redraw = True
        except Exception as exc:
            print("AI Dash: display restart failed (%s)" % type(exc).__name__)
            _reboot()

    def _maybe_confirm(self, payload):
        """Write the durable healthy marker once this app has proved itself.

        Two paths reach here. A daemon-issued attempt needs the acknowledgment
        that only arrives with a complete, validated status answer; a local
        activation (a USB install) has no attempt and confirms on its first
        successful poll. Writing the marker is what makes a reboot keep the
        new slot instead of rolling back to the old one.
        """
        if self._activation is None or self.journal is None:
            return
        pending = self.journal.get("pending")
        if not pending or pending.get("slot") != self.slot:
            return
        revision = current_revision_hex()
        if not revision or pending["target"] != revision:
            # This is not the app the activation staged, so it must not confirm
            # it: the target is the exact revision, not a prefix or a label.
            return
        if pending.get("attempt") is not None:
            device = payload.get("device_lifecycle") if isinstance(payload, dict) else None
            ack = device.get("confirm_attempt_id") if isinstance(device, dict) else None
            if ack != pending["attempt"]:
                return
        try:
            confirmed = self._activation.confirm(self.journal, pending.get("target"))
            with _FlashWindow(self):
                self._activation.save(self._store, confirmed)
            self.journal = confirmed
            self.boot_confirmed = True
            print("AI Dash: slot %s confirmed" % self.slot)
        except Exception as exc:
            print("AI Dash: slot confirmation could not be stored (%s)" % type(exc).__name__)

    def _diag_state(self):
        """Everything the device knows about itself, in one dict.

        The offline screen and the diagnostics header read the same values,
        so the two cannot drift apart. Every probe is optional: the
        firmware wrapper and MicroPython differ, and a diagnostic must
        never raise. A probe that could not run is left out of the dict
        instead of being guessed.

        The header uses ``err``, ``st``, ``n``, ``free``, ``up``, ``rssi``
        and ``drops``. The rest is for the screen alone: ``fails`` counts
        the consecutive failures, ``wifi`` says whether the radio is up,
        and ``wlan`` is the radio's own status code.
        """
        if self.started_at is None:
            self.started_at = self.uptime.ms()
        up = (self.uptime.ms() - self.started_at) // 1000
        state = {"fails": self.fail_count, "n": self.fail_total, "up": up,
                 "drops": self.wifi_drops}
        client = self.client
        error = getattr(client, "last_error", None)
        if error:
            state["err"] = error
        stage = getattr(client, "last_stage", None)
        if stage:
            state["st"] = stage
        try:
            import gc
            free = getattr(gc, "mem_free", None)
            if free:
                state["free"] = free() // 1024
        except Exception:
            pass
        try:
            state["wifi"] = bool(self.presto.wifi.isconnected())
        except Exception:
            pass
        try:
            import network
            wlan = network.WLAN(network.STA_IF)
        except Exception:
            wlan = None
        if wlan is not None:
            try:
                state["wlan"] = wlan.status()
            except Exception:
                pass
            try:
                state["rssi"] = wlan.status("rssi")
            except Exception:
                pass
        return state

    def _diag_lines(self):
        """Short ASCII lines on the link and memory, for the offline screen.

        Every probe is optional: the firmware wrapper and MicroPython differ,
        and a diagnostic must never raise.
        """
        state = self._diag_state()
        up = state["up"]
        lines = ["fails %d  up %dh%02dm" % (state["fails"], up // 3600,
                                             up // 60 % 60)]
        parts = []
        if "free" in state:
            parts.append("free %dK" % state["free"])
        if "wifi" in state:
            parts.append("wifi " + ("up" if state["wifi"] else "DOWN"))
        if "wlan" in state:
            parts.append("st %s" % (state["wlan"],))
        if "rssi" in state:
            parts.append("rssi %s" % (state["rssi"],))
        if state["drops"]:
            parts.append("drops %d" % state["drops"])
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

    def _lifecycle_tap(self):
        """A tap on the shared dock opens the read-only information view.

        It is checked before the bell, the page dots, Home rows and the card's
        tap-to-expand, so one area always means one thing. It starts no update,
        spends no retry and dismisses no warning; those are admin actions.
        """
        if self._connection_screen() is not None or not self.lifecycle_notice:
            return False
        if not lifecycle_hit(self._touch_x, self._touch_y, self.width, self.height):
            return False
        self.lifecycle_open = True
        self._needs_redraw = True
        return True

    def _close_lifecycle(self):
        self.lifecycle_open = False
        self._needs_redraw = True

    def _handle_gestures(self, gestures):
        for gesture in gestures:
            if gesture == SWIPE_LEFT:
                self.lifecycle_open = False
                self._next_card()
            elif gesture == SWIPE_RIGHT:
                self.lifecycle_open = False
                self._prev_card()
            elif gesture == TAP:
                if self.lifecycle_open:
                    self._close_lifecycle()
                elif self._lifecycle_tap():
                    pass
                elif self._bell_tap():
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
                                enabled=(self._current_card().id == "dash" and
                                         self._connection_screen() is None and
                                         not self.lifecycle_open))
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
        elif self.lifecycle_open and self.lifecycle_notice:
            draw_lifecycle_view(self.display, self.palette, self.lifecycle_notice,
                                self.width, self.height, demo=self.demo)
        elif card.id == "dash" and self.live_rows is not None:
            draw_sources(self.display, self.palette, self.live_rows,
                         self.index, len(self.cards), status=card.status,
                         muted=self.alerts.muted, width=self.width, height=self.height,
                         scroll=self.home_scroll.offset, stale=bool(self.connection_error),
                         layout=self.home_layout,
                         mute_text=self.alerts.mute_label(),
                         demo=self.demo, notice=self.lifecycle_notice)
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
                notice=self.lifecycle_notice,
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

    def _service_watchdog(self):
        if self._watchdog is not None:
            if self.boot_confirmed or _ticks_diff(_now_ms(), self._confirmation_started) < 180000:
                self._watchdog.feed()

    def _tick(self):
        """One pass of the main loop: touch, timers, and a redraw if needed.

        The device calls it every 20 ms from _run_live. The documentation
        site's demo calls it from a browser timer, so the page runs this same
        pass instead of a copy of it.
        """
        try:
            self._service_watchdog()
            self._ota_tick()
            self._handle_gestures(self._poll_touch())
            self._poll_popup()
            if self._expire_alerts():
                self._needs_redraw = True
            if self._last_screen != self._connection_screen():
                self._needs_redraw = True
            if self.detail_open and _ticks_diff(_now_ms(), self.detail_opened_at) > DETAIL_TIMEOUT_MS:
                # Not while the lifecycle view is open: closing it must return
                # to the card exactly as the user left it.
                if not self.lifecycle_open:
                    self._close_detail()
            if self.lifecycle_open and self._connection_screen() is not None:
                # A full-screen connecting/offline/error screen owns the
                # display and hides the dock with it, so the view goes too.
                # The local record of the problem is kept either way.
                self.lifecycle_open = False
                self._needs_redraw = True
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
    import machine
    sys.modules["machine"] = machine
    from presto import Presto

    presto = Presto(full_res=bool(getattr(config, "FULL_RES", False)))
    try:
        display = presto.display
        palette = Palette(display)

        draw_boot(display, palette, "Starting...")
        presto.update()

        app = App(presto)
        app.run()
    except BaseException:
        # The bootstrap records a crashed candidate in flash, which cannot be
        # written while the display runs (see _FlashWindow).
        try:
            presto.presto.__del__()
        except Exception:
            pass
        raise


if __name__ == "__main__":
    main()
