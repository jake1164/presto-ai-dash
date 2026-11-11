"""Shared helpers for the OpenDisplay research probes.

RESEARCH ONLY. Nothing here is production code and nothing in the daemon or the
Presto app imports it. Run with a Python that has ``py-opendisplay`` installed
(the repo .venv does not), e.g. ``py -3.13 tools/opendisplay_probe/probe_connect.py``.

The display is chosen at run time: ``--address``, the ``OD_PROBE_ADDRESS``
environment variable, or, when exactly one OpenDisplay device is advertising,
that device. No address is stored in this repository.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import statistics
import time
from dataclasses import dataclass
from pathlib import Path

from bleak import BleakScanner
from opendisplay import MANUFACTURER_ID, parse_advertisement

OUT_DIR = Path(__file__).resolve().parents[2] / ".ai_docs" / "opendisplay-probe"


def mask(address: str) -> str:
    return "..:" + address[-5:]


def add_target_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--address", default=os.environ.get("OD_PROBE_ADDRESS"),
                   help="BLE address (default: $OD_PROBE_ADDRESS, else the only advertising OpenDisplay device)")
    p.add_argument("--name", default=None, help="only consider devices whose name starts with this")


class Recorder:
    """Append-only JSON-lines log, one file per probe run."""

    def __init__(self, probe: str):
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        self.path = OUT_DIR / f"{probe}-{stamp}.jsonl"
        self.t0 = time.perf_counter()

    def write(self, **kw) -> None:
        kw = {"t": round(time.perf_counter() - self.t0, 3), **kw}
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(kw, default=str) + "\n")


@dataclass
class Seen:
    device: object  # bleak BLEDevice
    adv: object | None  # opendisplay AdvertisementData
    rssi: int
    name: str


def _parse(adv_data):
    raw = adv_data.manufacturer_data.get(MANUFACTURER_ID)
    if raw is None:
        return None, False
    try:
        return parse_advertisement(raw), True
    except Exception:
        return None, True


async def resolve_target(address: str | None, name: str | None, window: float = 8.0) -> str:
    """Return the address of the one OpenDisplay device to test."""
    if address:
        return address
    found: dict[str, Seen] = {}

    def cb(device, adv_data):
        adv, ours = _parse(adv_data)
        if ours and (not name or (device.name or "").startswith(name)):
            found[device.address] = Seen(device, adv, adv_data.rssi, device.name or "?")

    async with BleakScanner(detection_callback=cb):
        await asyncio.sleep(window)
    if len(found) != 1:
        raise SystemExit(
            f"Expected exactly one OpenDisplay device, saw {len(found)}. Pass --address or --name."
        )
    return next(iter(found))


async def scan_for(address: str, timeout: float) -> Seen | None:
    """Passive scan until ``address`` advertises (or ``timeout``). Returns None on a miss."""
    hit: asyncio.Future = asyncio.get_running_loop().create_future()
    target = address.upper()

    def cb(device, adv_data):
        if device.address.upper() != target or hit.done():
            return
        adv, ours = _parse(adv_data)
        if ours:
            hit.set_result(Seen(device, adv, adv_data.rssi, device.name or "?"))

    async with BleakScanner(detection_callback=cb):
        try:
            return await asyncio.wait_for(hit, timeout)
        except asyncio.TimeoutError:
            return None


class SdkEvents(logging.Handler):
    """Timestamps the SDK's own progress messages (upload start, refresh start/complete)."""

    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.events: list[tuple[float, str]] = []
        lg = logging.getLogger("opendisplay")
        lg.setLevel(logging.DEBUG)
        lg.addHandler(self)

    def emit(self, record):
        self.events.append((time.perf_counter(), record.getMessage()))

    def first(self, needle: str, after: float = 0.0) -> float | None:
        for t, m in self.events:
            if t >= after and needle in m:
                return t
        return None

    def close_handler(self):
        logging.getLogger("opendisplay").removeHandler(self)


async def retry(fn, *, attempts=3, backoff=(1.0, 2.0, 4.0), on_fail=None):
    """Bounded retry with backoff. Re-raises the last error; never loops forever."""
    for i in range(attempts):
        try:
            return await fn(i)
        except Exception as e:  # noqa: BLE001 - probe records everything
            if on_fail:
                on_fail(i, e)
            if i == attempts - 1:
                raise
            await asyncio.sleep(backoff[min(i, len(backoff) - 1)])


def stats(values: list[float]) -> str:
    if not values:
        return "n=0"
    v = sorted(values)
    p90 = v[min(len(v) - 1, int(round(0.9 * (len(v) - 1))))]
    return (f"n={len(v)} min={v[0]:.2f} med={statistics.median(v):.2f} "
            f"p90={p90:.2f} max={v[-1]:.2f} (s)")


async def connect_with_recovery(address: str, config=None, *, rec: Recorder | None = None,
                                attempts: int = 5, backoff=(1.0, 2.0, 4.0)):
    """Connect to the display, bounded. Returns (device, info).

    Strategy (derived from probe_connect results): try a direct connect by address
    first (no scan), and fall back to a fresh passive scan + connect on failure.
    The SDK's own retry is left at one attempt so every failure is visible here.
    """
    from bleak.backends.device import BLEDevice
    from opendisplay import OpenDisplayDevice

    info = {"attempts": []}

    async def attempt(i):
        t = time.perf_counter()
        via = "direct"
        ble = BLEDevice(address, "direct", None)
        if i > 0:  # recovery: re-acquire the device from a scan
            via = "scan"
            seen = await scan_for(address, 8.0)
            if seen is None:
                info["attempts"].append({"via": via, "ok": False, "phase": "discover",
                                         "s": round(time.perf_counter() - t, 2)})
                raise TimeoutError("not advertising")
            ble = seen.device
        dev = OpenDisplayDevice(address, ble_device=ble, config=config, timeout=10.0, max_attempts=1)
        try:
            await dev.__aenter__()
        except Exception as e:  # noqa: BLE001
            info["attempts"].append({"via": via, "ok": False, "phase": "connect",
                                     "s": round(time.perf_counter() - t, 2), "err": f"{type(e).__name__}: {e}"})
            raise
        info["attempts"].append({"via": via, "ok": True, "s": round(time.perf_counter() - t, 2)})
        return dev

    def on_fail(i, e):
        if rec:
            rec.write(event="connect_retry", attempt=i, err=repr(e))

    dev = await retry(attempt, attempts=attempts, backoff=backoff, on_fail=on_fail)
    return dev, info
