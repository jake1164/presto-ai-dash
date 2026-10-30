"""The app running from a slot: what it reports, and when it confirms itself.

The bootstrap hands the app its start context (``BOOT_CONTEXT``). These tests
run the real ``presto/main.py`` App with that context, an in-memory journal and
the shared sample payload, so the header, the two-poll acknowledgement and the
legacy fallback are all the shipped code.
"""

import importlib.util
import io
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import presto_path
import activation
import net
import updater
from payloads import sample_payload

DEVICE = "0123456789abcdef" * 2
SLOT_HASH = "a" * 64
GEN = "1" * 32
ATTEMPT = "2" * 32


class Display(object):
    def __init__(self, width=240, height=240):
        self.width, self.height = width, height
        self.font = "bitmap8"

    def get_bounds(self):
        return self.width, self.height

    def create_pen(self, *rgb):
        return rgb

    def set_pen(self, pen):
        pass

    def set_font(self, name):
        self.font = name

    def set_clip(self, *args):
        pass

    def remove_clip(self):
        pass

    def measure_text(self, text, scale=1):
        return len(str(text)) * 6 * scale

    def text(self, *args, **kwargs):
        pass

    def rectangle(self, *args):
        pass

    def circle(self, *args):
        pass

    def clear(self):
        pass


class MemoryStore(object):
    def __init__(self):
        self.files = {}

    def exists(self, path):
        return path in self.files

    def read(self, path):
        return self.files.get(path)

    def write(self, path, text):
        self.files[path] = text

    def replace(self, source, target):
        self.files[target] = self.files.pop(source)

    def remove(self, path):
        self.files.pop(path, None)

    def sync(self):
        pass

    def free_bytes(self):
        return 8 * 1024 * 1024

    def open(self, path, mode="wb"):
        store = self
        if "r" in mode:
            return io.BytesIO(store.files[path])
        self.files[path] = b""

        class Handle(object):
            def write(self, chunk):
                store.files[path] = store.files.get(path, b"") + chunk

            def close(self):
                pass

        return Handle()

    def listdir(self, folder):
        return [path.rsplit('/', 1)[1] for path in self.files if path.rsplit('/', 1)[0] == folder]


def load_app(values):
    patcher = patch.dict(sys.modules, {"config": SimpleNamespace(**values)})
    patcher.start()
    spec = importlib.util.spec_from_file_location(
        "slot_app_for_test", os.path.join(presto_path.PRESTO_DIR, "main.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, patcher


def parse_header(text):
    """The lifecycle header as a dict, read back with the daemon's own parser."""
    import json
    return json.loads(text)


class SlotAppBase(unittest.TestCase):
    extra_config = {}

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        values = {"DEVICE_ID": DEVICE, "DEVICE_REVISION": SLOT_HASH, "DEVICE_NAME": "presto",
                  "BOOT_COUNTER_FILE": os.path.join(self.temp.name, "boot.txt")}
        values.update(self.extra_config)
        self.store = MemoryStore()
        self.module, patcher = load_app(values)
        self.addCleanup(patcher.stop)
        self.store_patch = patch.object(activation, "FileStore", lambda: self.store)
        self.store_patch.start()
        self.addCleanup(self.store_patch.stop)
        self.revision_patch = patch.object(net, "REVISION_OVERRIDE", SLOT_HASH)
        self.revision_patch.start()
        self.addCleanup(self.revision_patch.stop)
        self.device = SimpleNamespace(
            display=Display(), update=lambda: None, NUM_LEDS=7,
            set_led_rgb=lambda *args: None,
            wifi=SimpleNamespace(isconnected=lambda: True),
            touch=SimpleNamespace(state=False, poll=lambda: None, x=0, y=0))
        self.clock = patch.object(self.module, "_now_ms", return_value=0)
        self.clock.start()
        self.addCleanup(self.clock.stop)
        diff = patch.object(self.module, "_ticks_diff", side_effect=lambda a, b: a - b)
        diff.start()
        self.addCleanup(diff.stop)

    def make_app(self, journal, context):
        if context is not None:
            journal = dict(journal, active=context["slot"])
        self.store.files[activation.JOURNAL_PATH] = activation.encode_journal(journal)
        self.module.BOOT_CONTEXT = {"journal": journal, "context": context}
        app = self.module.App(self.device)
        app._render = lambda: None
        return app

    def slot_context(self, journal, boot_confirmed=False):
        slot = journal["pending"]["slot"] if journal["pending"] else journal["active"]
        return {"boot": journal["boots"], "bootstrap": 1, "slot": slot,
                "boot_confirmed": boot_confirmed, "target": SLOT_HASH, "source": "primary"}


class SlotReportTests(SlotAppBase):

    def test_a_slot_app_reports_the_slot_scheme_and_its_full_hash(self):
        pending = {"slot": "b", "target": SLOT_HASH, "generation": None,
                   "attempt": None, "command": None, "tries": 1}
        journal = activation.begin_candidate(activation.blank_journal(), "b", SLOT_HASH)
        journal["pending"] = pending
        app = self.make_app(journal, self.slot_context(journal))
        device_id, text = app._lifecycle_headers()
        self.assertEqual(device_id, DEVICE)
        report = parse_header(text)
        self.assertEqual(report["scheme"], "slot-v1")
        self.assertEqual(report["revision"], SLOT_HASH)
        self.assertEqual((report["bootstrap"], report["slot"]), (1, "b"))
        self.assertFalse(report["boot_confirmed"])
        self.assertEqual(report["boot"], journal["boots"])
        self.assertEqual(report["update"]["phase"], "idle")

    def test_a_legacy_app_still_reports_the_legacy_scheme(self):
        app = self.make_app(activation.blank_journal(), None)
        app.boot_context = None
        app.slot = None
        app.bootstrap = None
        app.app_scheme = "legacy-v1"
        _device_id, text = app._lifecycle_headers()
        report = parse_header(text)
        self.assertEqual(report["scheme"], "legacy-v1")
        self.assertIsNone(report["slot"])
        self.assertIsNone(report["bootstrap"])

    def test_the_boot_count_comes_from_the_bootstrap_not_a_second_counter(self):
        journal = activation.blank_journal()
        app = self.make_app(journal, self.slot_context(journal))
        self.assertEqual(app.boot, journal["boots"])
        self.assertEqual(app.slot, "a")
        self.assertEqual(app.bootstrap, 1)


class SlotConfirmationTests(SlotAppBase):
    def test_watchdog_stops_feeding_an_unconfirmed_candidate_at_its_deadline(self):
        from unittest.mock import Mock
        app = self.candidate()
        app._watchdog = Mock()
        app._service_watchdog()
        app._watchdog.feed.assert_called_once()
        with patch.object(self.module, '_now_ms', return_value=180001):
            app._service_watchdog()
        app._watchdog.feed.assert_called_once()
        app.boot_confirmed = True
        with patch.object(self.module, '_now_ms', return_value=180002):
            app._service_watchdog()
        self.assertEqual(app._watchdog.feed.call_count, 2)
    def test_failed_confirmation_write_can_retry_the_same_acknowledgement(self):
        app = self.candidate()
        payload = sample_payload()
        payload["device_lifecycle"] = {"confirm_attempt_id": ATTEMPT}
        with patch.object(self.store, "write", side_effect=OSError("full")):
            app._set_payload(payload)
        self.assertIsNotNone(app.journal["pending"])
        self.assertFalse(app.boot_confirmed)
        app._set_payload(payload)
        self.assertTrue(app.boot_confirmed)

    def candidate(self):
        journal = activation.begin_candidate(activation.blank_journal(), "b", SLOT_HASH,
                                             GEN, ATTEMPT, "3" * 32)
        return self.make_app(journal, self.slot_context(journal))

    def test_a_daemon_acknowledgement_confirms_and_is_reported_as_succeeded(self):
        app = self.candidate()
        payload = sample_payload()
        payload["device_lifecycle"] = {"update": {"state": "updating"},
                                       "confirm_attempt_id": ATTEMPT}
        app._set_payload(payload)
        stored = activation.parse_journal(self.store.files[activation.JOURNAL_PATH])
        self.assertIsNone(stored["pending"])
        self.assertEqual(stored["confirmed"], "b")
        self.assertEqual(stored["success"]["attempt"], ATTEMPT)
        self.assertTrue(app.boot_confirmed)
        _device_id, text = app._lifecycle_headers()
        report = parse_header(text)
        self.assertEqual(report["update"]["phase"], "succeeded")
        self.assertTrue(report["boot_confirmed"])

    def test_an_acknowledgement_for_another_attempt_confirms_nothing(self):
        app = self.candidate()
        payload = sample_payload()
        payload["device_lifecycle"] = {"confirm_attempt_id": "9" * 32}
        app._set_payload(payload)
        stored = activation.parse_journal(self.store.files[activation.JOURNAL_PATH])
        self.assertIsNotNone(stored["pending"])
        self.assertFalse(app.boot_confirmed)

    def test_a_local_activation_confirms_on_its_first_good_payload(self):
        journal = activation.begin_candidate(activation.blank_journal(), "b", SLOT_HASH)
        app = self.make_app(journal, self.slot_context(journal))
        app._set_payload(sample_payload())
        stored = activation.parse_journal(self.store.files[activation.JOURNAL_PATH])
        self.assertEqual(stored["confirmed"], "b")
        self.assertIsNone(stored["pending"])
        self.assertTrue(app.boot_confirmed)

    def test_a_failed_payload_never_confirms_a_candidate(self):
        journal = activation.begin_candidate(activation.blank_journal(), "b", SLOT_HASH)
        app = self.make_app(journal, self.slot_context(journal))
        app._set_payload({"cards": "not a list"})
        stored = activation.parse_journal(self.store.files[activation.JOURNAL_PATH])
        self.assertIsNotNone(stored["pending"])
        self.assertFalse(app.boot_confirmed)

    def test_a_different_revision_never_confirms_the_staged_target(self):
        journal = activation.begin_candidate(activation.blank_journal(), "b", "b" * 64)
        app = self.make_app(journal, self.slot_context(journal))
        app._set_payload(sample_payload())
        stored = activation.parse_journal(self.store.files[activation.JOURNAL_PATH])
        self.assertIsNotNone(stored["pending"])


class SlotTransport(object):
    """A verified-TLS transport stand-in: only the two calls the app makes."""

    def __init__(self, files):
        self.files = files
        self.trusted = True
        self.hostname = "dash.lan"
        self.requested = []

    def manifest(self, path):
        self.requested.append(path)
        import updater
        return updater.manifest_text(updater.manifest_for_bundle(self.files))

    def stream(self, name, chunk):
        data = self.files[name]
        for start in range(0, len(data), chunk):
            yield data[start:start + chunk]


class SlotStagingTests(SlotAppBase):
    def test_different_revision_round_trip_through_real_coordinator(self):
        from presto_ai_dash.device_lifecycle import DeviceLifecycle
        from presto_ai_dash.storage import Storage
        from core.test_device_ota import bundle, headers
        storage = Storage(os.path.join(self.temp.name, 'round-trip.sqlite'))
        self.addCleanup(storage.close)
        target = self.manifest()["revision"]
        coordinator = DeviceLifecycle(storage, expected_provider=lambda: bundle(target),
                                      ota_enabled=True, running_version='test', channel='development')
        def poll(app):
            device_id, text = app._lifecycle_headers()
            return coordinator.observe(headers(('X-Presto-Device-ID', device_id),
                ('X-Presto-Lifecycle', text), ('X-Presto-Revision', net.current_revision_hex()[:12]),
                ('X-Presto-Client', 'synthetic-presto')))
        app = self.app()
        offered = poll(app)
        self.assertIsNotNone(offered["command"])
        app._ota_offer({"device_lifecycle": offered})
        self.assertEqual(poll(app)["update"]["attempts_started"], 1)
        for _ in range(100):
            app._ota_tick()
            if app._transfer is None:
                break
        entered, decision = activation.on_boot(activation.load(self.store)[0])
        self.assertTrue(decision["candidate"])
        with patch.object(net, 'REVISION_OVERRIDE', target):
            candidate = self.make_app(entered, activation.context(entered))
            confirming = poll(candidate)
            self.assertEqual(confirming["confirm_attempt_id"], offered["command"]["attempt"])
            payload = sample_payload()
            payload['device_lifecycle'] = confirming
            candidate._set_payload(payload)
            success = poll(candidate)
            self.assertEqual(success['update']['state'], 'current')
            self.assertFalse(success['update']['needs_attention'])
        self.assertEqual(activation.load(self.store)[0]['success']['attempt'], offered['command']['attempt'])
    def test_unchanged_revision_starts_no_transfer(self):
        app = self.app()
        with patch.object(net, "REVISION_OVERRIDE", self.manifest()["revision"]):
            app._ota_offer({"device_lifecycle": {"command": self.command(self.manifest())}})
        self.assertIsNone(app._transfer)
        self.assertIsNone(activation.load(self.store)[0]["transfer"])

    def test_untrusted_transport_is_blocked_before_fetching_manifest(self):
        app = self.app()
        self.transport.trusted = False
        app._ota_offer({"device_lifecycle": {"command": self.command(self.manifest())}})
        self.assertEqual(self.transport.requested, [])
        self.assertIsNone(app._transfer)
        self.assertEqual(activation.load(self.store)[0]["failure"]["error"], "transport_untrusted")

    def test_incompatible_bootstrap_cannot_write_slot_files(self):
        app = self.app()
        app.journal["bootstrap"] = 0
        app._ota_offer({"device_lifecycle": {"command": self.command(self.manifest())}})
        self.assertIsNone(app._transfer)
        self.assertFalse(any(name.startswith('/ai_dash_slots/') for name in self.store.files))

    def test_permit_is_durable_and_advertised_before_the_first_download_step(self):
        app = self.app()
        command = self.command(self.manifest())
        app._ota_offer({"device_lifecycle": {"command": command}})
        persisted = activation.load(self.store)[0]["transfer"]
        self.assertEqual(persisted["attempt"], command["attempt"])
        self.assertEqual(persisted["command"], command["id"])
        self.assertFalse(any(name.startswith('/ai_dash_slots/') for name in self.store.files))
        header = parse_header(app._lifecycle_headers()[1])
        self.assertEqual(header["caps"], ["ota-v1"])
        self.assertEqual(header["update"]["phase"], "downloading")
        original = app._transfer
        app._ota_offer({"device_lifecycle": {"command": command}})
        self.assertIs(app._transfer, original)

    def test_failed_acceptance_write_prevents_transfer(self):
        app = self.app()
        with patch.object(self.store, "write", side_effect=OSError("full")):
            app._ota_offer({"device_lifecycle": {"command": self.command(self.manifest())}})
        self.assertIsNone(app._transfer)


    def manifest(self):
        return updater.manifest_for_bundle(self.files)

    def setUp(self):
        super().setUp()
        self.files = {"ai_dash_app.py": b"print('new')\n", "ui.py": b"value = 2\n"}
        manifest = self.manifest()
        self.assertNotEqual(manifest["revision"], SLOT_HASH)
        self.transport = SlotTransport(self.files)
        self.resets = []

    def command(self, manifest):
        return {"id": "3" * 32, "kind": "start_update", "generation": "1" * 32,
                "attempt": "2" * 32, "attempt_no": 1,
                "target_scheme": "slot-v1",
                "target_revision": manifest["revision"],
                "manifest_path": "/api/v1/device-app/manifest"}

    def app(self):
        journal = activation.blank_journal()
        app = self.make_app(journal, self.slot_context(journal, boot_confirmed=True))
        app.ota = updater.Updater(self.store, self.transport, lambda: 1800000000,
            firmware={"sha256": True, "rename": True, "statvfs": True, "watchdog": True}, enabled=True, journal=app.journal)
        app._ota_reset = lambda: self.resets.append(1)
        return app

    def test_a_permitted_update_stages_across_frames_and_activates(self):
        manifest = self.manifest()
        app = self.app()
        payload = sample_payload()
        payload["device_lifecycle"] = {"command": self.command(manifest)}
        app._ota_offer(payload)
        self.assertIsNotNone(app._transfer)
        for _ in range(20):
            app._ota_tick()
            if app._transfer is None:
                break
        self.assertIsNone(app._transfer)
        self.assertEqual(self.resets, [1])
        stored = activation.parse_journal(self.store.files[activation.JOURNAL_PATH])
        self.assertEqual(stored["pending"]["slot"], "b")
        self.assertEqual(stored["pending"]["target"], manifest["revision"])
        self.assertEqual(stored["pending"]["attempt"], self.command(manifest)["attempt"])

    def test_a_manifest_for_another_revision_is_not_staged(self):
        manifest = dict(self.manifest(), revision="0" * 64)
        app = self.app()
        payload = sample_payload()
        payload["device_lifecycle"] = {"command": self.command(manifest)}
        app._ota_offer(payload)
        self.assertIsNone(app._transfer)

    def test_without_an_updater_nothing_is_staged_or_written(self):
        app = self.app()
        app.ota = None
        payload = sample_payload()
        payload["device_lifecycle"] = {"command": self.command(self.manifest())}
        app._ota_offer(payload)
        self.assertIsNone(app._transfer)
        app._ota_tick()
        self.assertNotIn("/ai_dash_slots/a/ai_dash_app.py", self.store.files)


class NativeDisplay(object):
    """The firmware's core1 display object, as measured on the device.

    Its finaliser stops the scan-out; a second call is a no-op. A new object
    restarts the scan-out. ``live`` lists the objects scanning out now.
    """
    live = []
    made = []

    def __init__(self, full_res=False, palette=False):
        self.args = (full_res, palette)
        self.stopped = False
        NativeDisplay.live.append(self)
        NativeDisplay.made.append(self)

    def __del__(self):
        # CPython also calls this when the object is collected.
        if not self.stopped:
            self.stopped = True
            if self in NativeDisplay.live:
                NativeDisplay.live.remove(self)


class DisplayGuardedStore(MemoryStore):
    """Refuses every flash mutation while the display scans out (it hangs)."""

    def __init__(self):
        super().__init__()
        self.mutations = 0

    def _guard(self):
        if NativeDisplay.live:
            raise AssertionError("flash written while the display was running")
        self.mutations += 1

    def write(self, path, text):
        self._guard()
        super().write(path, text)

    def replace(self, source, target):
        self._guard()
        super().replace(source, target)

    def remove(self, path):
        self._guard()
        super().remove(path)

    def sync(self):
        self._guard()

    def open(self, path, mode="wb"):
        if "r" not in mode:
            self._guard()
        handle = super().open(path, mode)
        if "r" in mode:
            return handle
        store = self

        class Guarded(object):
            def write(self, chunk):
                store._guard()
                handle.write(bytes(chunk))

            def close(self):
                handle.close()

        return Guarded()


class FlashWindowTests(SlotStagingTests):
    """Every flash write of the running app happens with core1 stopped."""

    def setUp(self):
        super().setUp()
        NativeDisplay.live = []
        NativeDisplay.made = []
        self.store = DisplayGuardedStore()
        self.store_patch.stop()
        self.store_patch = patch.object(activation, "FileStore", lambda: self.store)
        self.store_patch.start()
        self.addCleanup(self.store_patch.stop)
        self.device.presto = NativeDisplay()
        self.device.buffer = None

    def make_app(self, journal, context):
        # The stored journal is the bootstrap's, written before the display.
        live, NativeDisplay.live = NativeDisplay.live, []
        try:
            return super().make_app(journal, context)
        finally:
            NativeDisplay.live = live

    def test_a_whole_update_writes_flash_only_with_the_display_stopped(self):
        app = self.app()
        command = self.command(self.manifest())
        app._ota_offer({"device_lifecycle": {"command": command}})
        self.assertIsNotNone(app._transfer)
        for _ in range(50):
            app._ota_tick()
            if app._transfer is None:
                break
        self.assertEqual(self.resets, [1])
        self.assertGreater(self.store.mutations, 5)
        stored = activation.load(self.store)[0]
        self.assertEqual(stored["pending"]["target"], self.manifest()["revision"])
        for name, data in self.files.items():
            self.assertEqual(self.store.files["/ai_dash_slots/b/" + name], data)
        # Restarted after each window, on the same configuration.
        self.assertEqual(len(NativeDisplay.live), 1)
        self.assertIs(app.presto.presto, NativeDisplay.live[0])
        self.assertEqual(app.presto.presto.args, (False, False))
        self.assertEqual(app._flash_depth, 0)
        self.assertTrue(app._needs_redraw)

    def test_the_two_poll_confirmation_writes_with_the_display_stopped(self):
        journal = activation.begin_candidate(activation.blank_journal(), "b", SLOT_HASH,
                                             GEN, ATTEMPT, "3" * 32)
        app = self.make_app(journal, self.slot_context(journal))
        payload = sample_payload()
        payload["device_lifecycle"] = {"confirm_attempt_id": ATTEMPT}
        app._set_payload(payload)
        self.assertTrue(app.boot_confirmed)
        self.assertEqual(activation.load(self.store)[0]["confirmed"], "b")
        self.assertEqual(len(NativeDisplay.live), 1)

    def test_a_failed_write_still_restarts_the_display(self):
        app = self.app()
        with patch.object(MemoryStore, "write", side_effect=OSError("full")):
            app._ota_offer({"device_lifecycle": {"command": self.command(self.manifest())}})
        self.assertIsNone(app._transfer)
        self.assertEqual(len(NativeDisplay.live), 1)
        self.assertEqual(app._flash_depth, 0)

    def test_a_half_resolution_buffer_follows_the_new_native_object(self):
        app = self.app()
        app.presto.buffer = "old view"
        with patch.object(self.module, "memoryview", create=True,
                          side_effect=lambda native: ("view", native)):
            with self.module._FlashWindow(app):
                with self.module._FlashWindow(app):
                    self.assertEqual(NativeDisplay.live, [])
        self.assertEqual(app.presto.buffer, ("view", app.presto.presto))
        # Nested windows restart the display once.
        self.assertEqual(len(NativeDisplay.made), 2)


class LegacyStartTests(SlotAppBase):
    """A legacy install counts its boots with the display stopped.

    Measured on the device: with the display driver running, the first flash write
    never returns, and the screen stayed on "Starting..." for good.
    """

    def test_the_boot_counter_is_written_with_the_display_stopped(self):
        NativeDisplay.live, NativeDisplay.made = [], []
        self.device.presto = NativeDisplay()
        self.device.buffer = None
        seen = []

        def bump(file=None):
            seen.append(list(NativeDisplay.live))
            return 7

        self.module.BOOT_CONTEXT = None
        with patch.object(self.module.lifecycle, "bump_boot", side_effect=bump):
            app = self.module.App(self.device)
        self.assertEqual(seen, [[]])
        self.assertEqual(app.boot, 7)
        # The display is running again afterwards, and the screen is redrawn.
        self.assertEqual(len(NativeDisplay.live), 1)
        self.assertEqual(app._flash_depth, 0)
        self.assertTrue(app._needs_redraw)

    def test_without_a_device_identity_nothing_is_written(self):
        self.module.BOOT_CONTEXT = None
        with patch.object(self.module, "current_device_id", return_value=None), \
                patch.object(self.module.lifecycle, "bump_boot") as bump:
            app = self.module.App(self.device)
        bump.assert_not_called()
        self.assertEqual(app.boot, 1)


class ClockCarryTests(unittest.TestCase):
    """The RTC restarts at 2021 on every reset; the app's own restart carries it."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.module, patcher = load_app({})
        self.addCleanup(patcher.stop)
        self.carry = os.path.join(self.temp.name, "clock.txt")
        patch.object(self.module, "CLOCK_CARRY", self.carry).start()
        self.addCleanup(patch.stopall)
        self.set_to = []
        self.resets = []
        self.mounts = []
        rtc = SimpleNamespace(datetime=lambda value: self.set_to.append(value))

        def mount():
            self.mounts.append(1)
            if len(self.mounts) > 1:
                raise OSError(1)  # EPERM, as measured for a second mount

        machine = SimpleNamespace(RTC=lambda: rtc, reset=lambda: self.resets.append(1))
        patch.dict(sys.modules, {"machine": machine,
                                 "psram": SimpleNamespace(mkramfs=mount)}).start()

    def test_reboot_resets_even_when_ramfs_is_already_mounted(self):
        self.module.BOOT_CONTEXT = {"journal": {}}
        self.addCleanup(setattr, self.module, "BOOT_CONTEXT", None)
        self.mounts.append(1)  # The stock launcher mounted it.
        launch = os.path.join(self.temp.name, "launch.txt")
        real_open = open

        def redirected(path, *args, **kwargs):
            return real_open(launch if path == "/ramfs/launch.txt" else path, *args, **kwargs)

        with patch("builtins.open", redirected), \
                patch.object(self.module.time, "sleep_ms", create=True, new=lambda ms: None), \
                patch.object(self.module.time, "time", return_value=1800000000):
            self.module._reboot()
        self.assertEqual(self.resets, [1])
        with real_open(launch) as handle:
            self.assertEqual(handle.read(), "ai_dash_boot.py")
        with real_open(self.carry) as handle:
            self.assertEqual(handle.read(), "1800000000")

    def test_an_unusable_clock_is_not_carried(self):
        with patch.object(self.module.time, "time", return_value=1609459300):
            self.module._carry_clock()
        self.assertFalse(os.path.exists(self.carry))

    def test_a_reset_clock_is_restored_with_the_seconds_since_the_reset(self):
        with open(self.carry, "w") as handle:
            handle.write("1800000000")
        with patch.object(self.module.time, "time", return_value=1609459200 + 7):
            self.module._restore_clock()
        import time as host_time
        expected = host_time.gmtime(1800000007)
        self.assertEqual(self.set_to, [(expected[0], expected[1], expected[2], expected[6],
                                        expected[3], expected[4], expected[5], 0)])
        self.assertFalse(os.path.exists(self.carry))

    def test_a_usable_clock_is_never_changed(self):
        with open(self.carry, "w") as handle:
            handle.write("1800000000")
        with patch.object(self.module.time, "time", return_value=1790000000):
            self.module._restore_clock()
        self.assertEqual(self.set_to, [])

    def test_a_corrupt_or_unusable_carry_sets_nothing(self):
        for text in ("garbage", "1609459300"):
            with open(self.carry, "w") as handle:
                handle.write(text)
            with patch.object(self.module.time, "time", return_value=1609459210):
                self.module._restore_clock()
        self.assertEqual(self.set_to, [])


if __name__ == "__main__":
    unittest.main()
