"""RESEARCH PROBE 3: physical button events from BLE advertisements alone.

NEVER opens a GATT connection: it only listens to the display's advertisements
(passive observation), decodes the dynamic bytes with the SDK
(parse_advertisement / AdvertisementTracker) and shows which button_id each
physical key produces. Read-only; does not touch the panel or the config.

  py -3.13 tools/opendisplay_probe/probe_buttons.py --baseline 60      # no presses: adv rate, idle bytes
  py -3.13 tools/opendisplay_probe/probe_buttons.py                    # interactive: KEY1, KEY2, KEY3
  py -3.13 tools/opendisplay_probe/probe_buttons.py --extra            # + double-tap and 2 s hold

Close any browser tab connected to the display first (a connected display
stops advertising).
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
import time

from bleak import BleakScanner
from opendisplay import MANUFACTURER_ID, AdvertisementTracker, parse_advertisement

from common import Recorder, add_target_args, mask, resolve_target


class Listener:
    def __init__(self, address: str, rec: Recorder):
        self.address = address.upper()
        self.rec = rec
        self.tracker = AdvertisementTracker()          # all 11 dynamic bytes
        self.arrivals: list[float] = []
        self.loop_counters: list[int] = []
        self.last_dyn: bytes | None = None
        self.window: list[dict] = []                   # events since last begin_window()
        self.printing = True

    def begin_window(self):
        self.window = []

    def on_adv(self, device, adv_data):
        if device.address.upper() != self.address:
            return
        raw = adv_data.manufacturer_data.get(MANUFACTURER_ID)
        if raw is None:
            return
        now = time.perf_counter()
        try:
            adv = parse_advertisement(raw)
        except Exception as e:  # noqa: BLE001
            self.rec.write(event="parse_error", err=repr(e), raw=bytes(raw).hex())
            return
        self.arrivals.append(now)
        self.rec.write(event="adv", loop=raw[13] >> 4 if len(raw) >= 14 else None, rssi=adv_data.rssi)
        self.loop_counters.append(adv.loop_counter)
        events = self.tracker.update(self.address, adv, timestamp=now)
        changed = adv.dynamic_data != self.last_dyn and self.last_dyn is not None
        if changed or events:
            diff = [i for i, (a, b) in enumerate(zip(self.last_dyn or b"", adv.dynamic_data)) if a != b]
            rec = {
                "event": "dyn_change", "raw": bytes(raw).hex(), "dyn": adv.dynamic_data.hex(),
                "changed_idx": diff, "loop": adv.loop_counter,
                "decoded": [(e.byte_index, e.button_id, e.press_count, e.pressed)
                            for e in adv.button_events if e.byte_index in diff],
                "tracker": [(e.event_type, e.byte_index, e.button_id, e.press_count) for e in events],
            }
            self.rec.write(**rec)
            self.window.append(rec)
            if self.printing:
                dec = ", ".join(f"byte{i}: id={e.button_id} count={e.press_count} down={e.pressed} raw=0x{e.raw:02x}"
                                for i in diff for e in [adv.button_events[i]])
                print(f"    adv dyn={adv.dynamic_data.hex()}  {dec}   tracker={[(e.event_type, e.button_id) for e in events]}")
        self.last_dyn = adv.dynamic_data

    def adv_stats(self):
        a = self.arrivals
        if len(a) < 3:
            return "too few advertisements"
        gaps = [b - x for x, b in zip(a, a[1:])]
        # loop counter is 4-bit and increments per transmitted advertisement: gaps in it = packets we missed
        missed = 0
        for p, c in zip(self.loop_counters, self.loop_counters[1:]):
            step = (c - p) % 16
            if step > 1:
                missed += step - 1
        gs = sorted(gaps)
        return (f"{len(a)} adverts in {a[-1]-a[0]:.0f}s; gap min={gs[0]:.2f} med={statistics.median(gs):.2f} "
                f"p90={gs[int(0.9*(len(gs)-1))]:.2f} max={gs[-1]:.2f}s; loop-counter jumps imply ~{missed} "
                f"transmitted adverts never reached this PC ({100*missed/max(1,len(a)+missed):.0f}%)")


def ids_in(window):
    s = set()
    for r in window:
        for idx, bid, cnt, down in r["decoded"]:
            s.add((idx, bid, down))
    return s


async def ask(prompt: str):
    await asyncio.to_thread(input, prompt)


async def main():
    ap = argparse.ArgumentParser()
    add_target_args(ap)
    ap.add_argument("--baseline", type=float, default=0, help="just listen this many seconds, no presses")
    ap.add_argument("--window", type=float, default=15.0, help="seconds to capture per press prompt")
    ap.add_argument("--presses", type=int, default=2, help="presses per key")
    ap.add_argument("--extra", action="store_true", help="also test double-tap and a 2 s hold on KEY2/KEY3")
    args = ap.parse_args()

    address = await resolve_target(args.address, args.name)
    rec = Recorder("buttons")
    lis = Listener(address, rec)
    print(f"target {mask(address)}  (passive scan only; no connection)  log={rec.path}")

    async with BleakScanner(detection_callback=lis.on_adv):
        if args.baseline:
            print(f"listening {args.baseline:.0f}s with NO presses ...")
            await asyncio.sleep(args.baseline)
            print("idle dynamic bytes:", lis.last_dyn.hex() if lis.last_dyn else None)
            print("changes while idle:", len(lis.window))
            print(lis.adv_stats())
            return

        print("waiting up to 60 s for the display to advertise (it advertises in bursts)...")
        for _ in range(600):
            if lis.last_dyn is not None:
                break
            await asyncio.sleep(0.1)
        if lis.last_dyn is None:
            print("The display is not being heard. Close browser BLE tabs / wake the device and retry.")
            return
        print("idle dynamic bytes:", lis.last_dyn.hex())
        observed: dict[str, set] = {}

        async def phase(label: str, instruction: str):
            # Hands-off: no ENTER needed. Announce, count down, listen.
            print(f"\n[{label}] get ready: {instruction}")
            for n in (3, 2, 1):
                print(f"   {n}...", end="", flush=True)
                await asyncio.sleep(1)
            lis.begin_window()
            lis.printing = True
            print(f"\n   >>> GO:{instruction}  (listening {args.window:.0f}s) <<<")
            await asyncio.sleep(args.window)
            lis.printing = False
            ids = ids_in(lis.window)
            print(f"   events captured: {len(lis.window)}   (byte, button_id, pressed) seen: {sorted(ids)}")
            observed.setdefault(label, set()).update(ids)
            rec.write(event="phase", label=label, ids=sorted(ids), n=len(lis.window))
            return lis.window

        lis.printing = False
        print("Starting in 5 s. Do NOT press anything until a GO line appears.")
        await asyncio.sleep(5)

        for key in ("KEY1", "KEY2", "KEY3"):
            for n in range(1, args.presses + 1):
                await phase(key, f"press and release {key} ONCE (attempt {n}/{args.presses})")

        if args.extra:
            for key in ("KEY2", "KEY3"):
                await phase(f"{key}-double", f"double-tap {key} quickly")
                await phase(f"{key}-hold", f"press and HOLD {key} for about 2 seconds, then release")

        print("\n== result ==")
        keys = ("KEY1", "KEY2", "KEY3")
        sets = {k: {(i, b) for (i, b, _d) in observed.get(k, set())} for k in keys}
        for k in keys:
            print(f"{k}: byte/button_id seen = {sorted(sets[k]) or 'NOTHING'}")
        nonempty = [k for k in keys if sets[k]]
        distinct = all(not (sets[a] & sets[b]) for a in nonempty for b in nonempty if a < b)
        if len(nonempty) < 3:
            print(f"-> no advertisement change for: {[k for k in keys if not sets[k]]}")
        print("-> keys distinguishable by button_id:", "YES" if distinct and len(nonempty) >= 2 else "NO / inconclusive")
        print(lis.adv_stats())


if __name__ == "__main__":
    asyncio.run(main())
