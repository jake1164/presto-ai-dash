"""RESEARCH PROBE 2: image delivery and refresh modes (this REFRESHES THE PANEL).

Generates an 800x480 1-bit Presto-AI-Dash test card and sends it with each
refresh mode. One operation = connect -> prepare -> upload (+ device-confirmed
refresh) -> disconnect, with every stage timed. Completion is taken from the
device's refresh-complete frame as reported by the SDK (it raises
RefreshTimeoutError otherwise), never from bytes sent.

Plan items: full, fast, partial (SDK RefreshMode.PARTIAL, full frame on this
panel), state (PartialState diff path: first one baselines, later ones diff).
Default plan = 9 refreshes. Keep it small; e-paper does not need a workout.

  py -3.13 tools/opendisplay_probe/probe_image.py --plan full,full,fast,fast,partial,partial,state,state,state
  py -3.13 tools/opendisplay_probe/probe_image.py --plan full --dump-only    # no hardware, writes the PNG
"""

from __future__ import annotations

import argparse
import asyncio
import time

from opendisplay import PartialState, RefreshMode, prepare_image

from card import make_card
from common import (OUT_DIR, Recorder, SdkEvents, add_target_args, connect_with_recovery, mask,
                    resolve_target, stats)

async def run_op(address, config, mode_name, seq, total, state, rec, ev):
    """One complete operation. Returns a result dict."""
    res = {"op": seq, "mode": mode_name}
    t_all = time.perf_counter()
    img = make_card(seq, total, label=mode_name)

    dev, cinfo = await connect_with_recovery(address, config, rec=rec)
    res["connect_s"] = round(time.perf_counter() - t_all, 2)
    res["connect_attempts"] = cinfo["attempts"]
    cfg = config or dev.config

    t = time.perf_counter()
    prepared = await asyncio.to_thread(prepare_image, img, config=cfg)
    res["prepare_s"] = round(time.perf_counter() - t, 3)
    res["wire_bytes"] = len(prepared[1] or prepared[0])
    res["compressed"] = prepared[1] is not None

    progress = {"first": None, "last": None, "sent": 0, "total": 0}

    def cb(sent, tot):
        now = time.perf_counter()
        progress["first"] = progress["first"] or now
        progress["last"], progress["sent"], progress["total"] = now, sent, tot

    mode = {"full": RefreshMode.FULL, "fast": RefreshMode.FAST,
            "partial": RefreshMode.PARTIAL, "state": RefreshMode.FULL}[mode_name]
    n_ev = len(ev.events)
    t_up = time.perf_counter()
    err = None
    try:
        await dev.upload_prepared_image(prepared, refresh_mode=mode, progress_callback=cb,
                                        state=state if mode_name == "state" else None)
    except Exception as e:  # noqa: BLE001
        err = f"{type(e).__name__}: {e}"
    t_done = time.perf_counter()

    msgs = [m for t_, m in ev.events[n_ev:]]
    t_refresh_start = ev.first("Display refresh started", t_up)
    t_refresh_done = ev.first("Display refresh complete", t_up)
    res["upload_total_s"] = round(t_done - t_up, 2)
    if t_refresh_start:
        res["transfer_s"] = round(t_refresh_start - t_up, 2)        # START .. data .. END ack
        if t_refresh_done:
            res["refresh_s"] = round(t_refresh_done - t_refresh_start, 2)  # END ack .. 0x73
    res["progress"] = f"{progress['sent']}/{progress['total']}"
    res["device_confirmed_refresh"] = t_refresh_done is not None
    if mode_name == "state":
        if any("partial path" in m for m in msgs):
            res["path"] = "partial"
        elif any("skipping" in m for m in msgs):
            res["path"] = "no_change(no refresh)"
        else:
            res["path"] = "full(fallback/baseline)"
    res["ok"] = err is None and (t_refresh_done is not None or res.get("path") == "no_change(no refresh)")
    if err:
        res["err"] = err

    t = time.perf_counter()
    try:
        await asyncio.wait_for(dev.__aexit__(None, None, None), 10)
    except Exception as e:  # noqa: BLE001
        res["disconnect_err"] = repr(e)
    res["disconnect_s"] = round(time.perf_counter() - t, 2)
    res["total_op_s"] = round(time.perf_counter() - t_all, 2)
    return res


async def main():
    ap = argparse.ArgumentParser()
    add_target_args(ap)
    ap.add_argument("--plan", default="full,full,fast,fast,partial,partial,state,state,state")
    ap.add_argument("--pause", type=float, default=5.0, help="seconds between operations")
    ap.add_argument("--dump-only", action="store_true", help="write the test card PNG and exit (no BLE)")
    args = ap.parse_args()

    if args.dump_only:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        p = OUT_DIR / "testcard.png"
        make_card(3, 9).convert("L").save(p)
        print("wrote", p)
        return

    plan = [s.strip() for s in args.plan.split(",") if s.strip()]
    address = await resolve_target(args.address, args.name)
    rec = Recorder("image")
    ev = SdkEvents()
    print(f"target {mask(address)}  plan={plan}  log={rec.path}")

    # Interrogate once (cheap, ~2-5 s) and reuse the config for every operation.
    dev, _ = await connect_with_recovery(address, None, rec=rec)
    config = dev.config
    await dev.__aexit__(None, None, None)

    state = PartialState()
    results = []
    for i, mode in enumerate(plan, 1):
        r = await run_op(address, config, mode, i, len(plan), state, rec, ev)
        results.append(r)
        rec.write(event="op", **r)
        print(f"#{i} {mode:7s} ok={r['ok']} connect={r['connect_s']}s prepare={r['prepare_s']}s "
              f"transfer={r.get('transfer_s')}s refresh={r.get('refresh_s')}s upload={r['upload_total_s']}s "
              f"disc={r['disconnect_s']}s TOTAL={r['total_op_s']}s wire={r['wire_bytes']}B"
              f"{' path=' + r['path'] if 'path' in r else ''}{' ERR ' + r['err'] if 'err' in r else ''}")
        if i < len(plan):
            await asyncio.sleep(args.pause)

    print("\n== summary by mode ==")
    for mode in dict.fromkeys(plan):
        rs = [r for r in results if r["mode"] == mode]
        okr = [r for r in rs if r["ok"]]
        print(f"{mode:7s} ok {len(okr)}/{len(rs)}  total_op {stats([r['total_op_s'] for r in okr])}")
        print(f"        transfer {stats([r['transfer_s'] for r in okr if 'transfer_s' in r])}")
        print(f"        refresh  {stats([r['refresh_s'] for r in okr if 'refresh_s' in r])}")
    print("raw log:", rec.path)


if __name__ == "__main__":
    asyncio.run(main())
