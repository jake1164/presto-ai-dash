"""RESEARCH PROBE 1: discovery / connect / disconnect / reconnect reliability.

Each cycle: scan -> connect -> read firmware version -> disconnect, with every
phase timed and any failure attributed to its phase. The SDK's own retry is
turned down to one attempt so the numbers show raw failures; the probe's bounded
retry (3 tries, 1s/2s backoff) then shows whether a retry recovers.

Never refreshes the panel. Read-only.

  py -3.13 tools/opendisplay_probe/probe_connect.py --cycles 10 --gap 2
"""

from __future__ import annotations

import argparse
import asyncio
import time

from bleak.backends.device import BLEDevice
from opendisplay import OpenDisplayDevice

from common import Recorder, add_target_args, mask, resolve_target, retry, scan_for, stats


async def one_cycle(n, address, config, rec, args):
    row = {"cycle": n, "cold": config is None}

    # --- discovery (bounded: 3 scans of 6/8/10 s, backoff between) -----------
    scan_log = []
    if args.direct:
        # No scan at all: WinRT bleak only needs the address to connect.
        class _Seen:
            device = BLEDevice(address, "direct", None)
            rssi = None
        seen = _Seen()
        row["discover"] = {"ok": True, "attempts": 0, "scans_s": [], "total_s": 0.0, "direct": True}
        return await _connect_phase(row, seen, address, config, rec)

    async def do_scan(i):
        t = time.perf_counter()
        seen = await scan_for(address, (6, 8, 10)[i])
        dt = time.perf_counter() - t
        scan_log.append(round(dt, 2))
        if seen is None:
            raise TimeoutError(f"not advertising within {(6, 8, 10)[i]}s")
        return seen, dt

    t_phase = time.perf_counter()
    try:
        seen, first_hit = await retry(do_scan, attempts=3, backoff=(1.0, 2.0))
        row["discover"] = {"ok": True, "attempts": len(scan_log), "scans_s": scan_log,
                           "total_s": round(time.perf_counter() - t_phase, 2), "rssi": seen.rssi}
    except Exception as e:  # noqa: BLE001
        row["discover"] = {"ok": False, "attempts": len(scan_log), "scans_s": scan_log, "err": repr(e)}
        rec.write(**row)
        return row, config

    return await _connect_phase(row, seen, address, config, rec)


async def _connect_phase(row, seen, address, config, rec):
    # --- connect (raw: SDK max_attempts=1; probe retry on top) ---------------
    conn_log = []
    holder = {}

    async def do_connect(i):
        dev = OpenDisplayDevice(address, ble_device=seen.device, config=config,
                                timeout=10.0, max_attempts=1)
        t = time.perf_counter()
        try:
            await dev.__aenter__()
        except Exception as e:  # noqa: BLE001
            conn_log.append({"ok": False, "s": round(time.perf_counter() - t, 2), "err": f"{type(e).__name__}: {e}"})
            raise
        conn_log.append({"ok": True, "s": round(time.perf_counter() - t, 2)})
        holder["dev"] = dev

    try:
        await retry(do_connect, attempts=3, backoff=(1.0, 2.0))
        row["connect"] = {"ok": True, "first_attempt_ok": conn_log[0]["ok"], "attempts": conn_log,
                          "s": conn_log[-1]["s"]}
    except Exception as e:  # noqa: BLE001
        row["connect"] = {"ok": False, "first_attempt_ok": False, "attempts": conn_log, "err": repr(e)}
        rec.write(**row)
        return row, config
    dev = holder["dev"]

    # --- communication ------------------------------------------------------
    t = time.perf_counter()
    try:
        fw = await asyncio.wait_for(dev.read_firmware_version(), 10)
        row["comm"] = {"ok": True, "s": round(time.perf_counter() - t, 2), "fw": f"{fw['major']}.{fw['minor']}.{fw['patch']}"}
        if config is None:
            config = dev.config
    except Exception as e:  # noqa: BLE001
        row["comm"] = {"ok": False, "s": round(time.perf_counter() - t, 2), "err": f"{type(e).__name__}: {e}"}

    # --- disconnect ---------------------------------------------------------
    t = time.perf_counter()
    try:
        await asyncio.wait_for(dev.__aexit__(None, None, None), 10)
        row["disconnect"] = {"ok": True, "s": round(time.perf_counter() - t, 2)}
    except Exception as e:  # noqa: BLE001
        row["disconnect"] = {"ok": False, "s": round(time.perf_counter() - t, 2), "err": repr(e)}

    rec.write(**row)
    return row, config


async def main():
    ap = argparse.ArgumentParser()
    add_target_args(ap)
    ap.add_argument("--direct", action="store_true", help="skip the scan and connect by address alone")
    ap.add_argument("--cycles", type=int, default=10)
    ap.add_argument("--gap", type=float, default=2.0, help="seconds to wait between cycles (0 = reconnect immediately)")
    args = ap.parse_args()

    address = await resolve_target(args.address, args.name)
    rec = Recorder(f"connect-{'direct' if args.direct else 'scan'}-gap{args.gap:g}")
    print(f"target {mask(address)}  cycles={args.cycles} gap={args.gap}s  log={rec.path}")

    # Cold connect: no cached config, so the SDK interrogates (this is the slow one).
    config = None
    rows = []
    for n in range(args.cycles):
        row, config = await one_cycle(n, address, config, rec, args)
        rows.append(row)
        d, c, m, x = (row.get(k, {}) for k in ("discover", "connect", "comm", "disconnect"))
        print(f"#{n:02d} discover{'(skipped)' if d.get('direct') else ''} {'ok ' if d.get('ok') else 'FAIL'} {d.get('total_s', '-')}s (scans {d.get('scans_s')})"
              f" | connect{'(cold)' if row.get('cold') else ''} {'ok ' if c.get('ok') else 'FAIL'} {c.get('s', '-')}s first_try={c.get('first_attempt_ok')}"
              f" | comm {'ok ' if m.get('ok') else ('FAIL' if m else '-')} {m.get('s', '-')}s"
              f" | disc {'ok ' if x.get('ok') else ('FAIL' if x else '-')} {x.get('s', '-')}s")
        if c and not c.get("ok"):
            print("    connect errors:", [a.get("err") for a in c["attempts"]])
        if n < args.cycles - 1 and args.gap:
            await asyncio.sleep(args.gap)

    def col(phase, key="s", only_ok=True):
        return [r[phase][key] for r in rows if phase in r and (r[phase].get("ok") or not only_ok) and key in r[phase]]

    n = len(rows)
    print("\n== summary ==")
    print(f"discover: first-scan success {sum(1 for r in rows if r['discover'].get('attempts') == 1 and r['discover']['ok'])}/{n},"
          f" recovered-by-retry {sum(1 for r in rows if r['discover']['ok'] and r['discover']['attempts'] > 1)},"
          f" gave up {sum(1 for r in rows if not r['discover']['ok'])}")
    print("  time-to-seen (incl. retries):", stats(col("discover", "total_s")))
    have_c = [r for r in rows if "connect" in r]
    print(f"connect : first-attempt success {sum(1 for r in have_c if r['connect']['first_attempt_ok'])}/{len(have_c)},"
          f" eventual success {sum(1 for r in have_c if r['connect']['ok'])}/{len(have_c)}")
    warm = [r["connect"]["s"] for r in have_c if r["connect"]["ok"] and not r["cold"]]
    cold = [r["connect"]["s"] for r in have_c if r["connect"]["ok"] and r["cold"]]
    print("  warm time (cached config, successful attempt):", stats(warm))
    print("  cold time (connect + interrogate):", stats(cold))
    have_m = [r for r in rows if "comm" in r]
    print(f"comm    : success {sum(1 for r in have_m if r['comm']['ok'])}/{len(have_m)}  ", stats(col("comm")))
    have_x = [r for r in rows if "disconnect" in r]
    print(f"discon. : success {sum(1 for r in have_x if r['disconnect']['ok'])}/{len(have_x)}  ", stats(col("disconnect")))
    print("raw log:", rec.path)


if __name__ == "__main__":
    asyncio.run(main())
