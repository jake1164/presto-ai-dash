"""RESEARCH PROBE 4: feasibility of a standalone Windows BLE worker (REFRESHES THE PANEL 2-3 times).

Prototype loop pieces, each measured:
  fetch    HTTP fetch with ETag (a local server stands in for the remote daemon;
           this proves the code path, not the network to the real host)
  upload   passive advert listener running the whole time; connect only for the
           upload; verify device-confirmed refresh; disconnect; see how long the
           listener takes to hear the display again
  hold     keep a GATT connection open and watch whether adverts continue
           (decides "persistent connection" vs "connect on demand")
  abort    INJECTED FAULT: cut a transfer off mid-stream, then recover and upload
  badaddr  INJECTED FAULT: connect to an address with nothing behind it; the
           retry must stay bounded and the error classified

  py -3.13 tools/opendisplay_probe/probe_worker.py --phases fetch,upload,hold,abort,badaddr
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import io
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from bleak import BleakScanner
from PIL import Image
from opendisplay import MANUFACTURER_ID, AdvertisementTracker, RefreshMode, parse_advertisement, prepare_image

from card import make_card
from common import (Recorder, SdkEvents, add_target_args, connect_with_recovery, mask, resolve_target)


# --- stand-in for the daemon's image endpoint ---------------------------------
class _Handler(BaseHTTPRequestHandler):
    seq = 1

    def do_GET(self):  # noqa: N802
        buf = io.BytesIO()
        make_card(_Handler.seq, 99).save(buf, "PNG")
        body = buf.getvalue()
        etag = '"' + hashlib.sha1(f"{_Handler.seq}".encode()).hexdigest()[:12] + '"'
        if self.headers.get("If-None-Match") == etag:
            self.send_response(304)
            self.send_header("ETag", etag)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("ETag", etag)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):  # quiet
        pass


def fetch(url: str, etag: str | None = None, timeout: float = 5.0):
    req = urllib.request.Request(url, headers={"If-None-Match": etag} if etag else {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.headers.get("ETag"), r.read()
    except urllib.error.HTTPError as e:
        if e.code == 304:
            return 304, etag, b""
        raise


# --- always-on advert listener --------------------------------------------------
class Bg:
    def __init__(self, address):
        self.address = address.upper()
        self.times: list[float] = []
        self.tracker = AdvertisementTracker()
        self.buttons: list = []

    def cb(self, device, adv_data):
        if device.address.upper() != self.address:
            return
        raw = adv_data.manufacturer_data.get(MANUFACTURER_ID)
        if raw is None:
            return
        now = time.perf_counter()
        self.times.append(now)
        try:
            self.buttons += self.tracker.update(self.address, parse_advertisement(raw), timestamp=now)
        except Exception:  # noqa: BLE001
            pass

    def count_between(self, a, b):
        return sum(1 for t in self.times if a <= t <= b)

    def first_after(self, t0):
        return next((t for t in self.times if t >= t0), None)


async def phase_fetch(url, rec):
    t = time.perf_counter()
    status, etag, body = await asyncio.to_thread(fetch, url)
    t1 = time.perf_counter() - t
    t = time.perf_counter()
    status2, _, body2 = await asyncio.to_thread(fetch, url, etag)
    t2 = time.perf_counter() - t
    img = Image.open(io.BytesIO(body))
    r = {"first": {"status": status, "bytes": len(body), "s": round(t1, 3), "size": img.size, "mode": img.mode},
         "revalidate": {"status": status2, "bytes": len(body2), "s": round(t2, 3)}}
    rec.write(event="fetch", **r)
    print("fetch:", r)
    return body


async def phase_upload(address, config, body, bg, rec, ev):
    img = Image.open(io.BytesIO(body))
    t_all = time.perf_counter()
    dev, cinfo = await connect_with_recovery(address, config, rec=rec)
    t_conn = time.perf_counter()
    prepared = await asyncio.to_thread(prepare_image, img, config=config or dev.config)
    t_up = time.perf_counter()
    n_ev = len(ev.events)
    await dev.upload_prepared_image(prepared, refresh_mode=RefreshMode.FULL)
    t_done = time.perf_counter()
    confirmed = ev.first("Display refresh complete", t_up) is not None
    adv_during = bg.count_between(t_conn, t_done)
    t_disc0 = time.perf_counter()
    await dev.__aexit__(None, None, None)
    t_disc1 = time.perf_counter()
    nxt = bg.first_after(t_disc1)
    # give the listener a moment to hear the display again
    for _ in range(100):
        if nxt:
            break
        await asyncio.sleep(0.1)
        nxt = bg.first_after(t_disc1)
    r = {"connect_s": round(t_conn - t_all, 2), "prepare_s": round(t_up - t_conn, 2),
         "upload_s": round(t_done - t_up, 2), "refresh_confirmed": confirmed,
         "disconnect_s": round(t_disc1 - t_disc0, 2), "total_s": round(t_disc1 - t_all, 2),
         "adverts_heard_while_connected": adv_during,
         "adverts_heard_before_connect_in_prev_5s": bg.count_between(t_all - 5, t_all),
         "listener_resumed_after_disconnect_s": round(nxt - t_disc1, 2) if nxt else None}
    rec.write(event="upload", **r)
    print("upload:", r)


async def phase_hold(address, config, bg, rec, seconds=30):
    dev, _ = await connect_with_recovery(address, config, rec=rec)
    t0 = time.perf_counter()
    await asyncio.sleep(seconds)
    t1 = time.perf_counter()
    try:
        still = bool(dev._conn._client.is_connected)  # noqa: SLF001 - probe only
    except Exception:  # noqa: BLE001
        still = None
    t = time.perf_counter()
    try:
        fw = await asyncio.wait_for(dev.read_firmware_version(), 10)
        comm = f"ok in {time.perf_counter() - t:.2f}s"
    except Exception as e:  # noqa: BLE001
        comm = f"FAILED {type(e).__name__}: {e}"
    await dev.__aexit__(None, None, None)
    r = {"held_s": round(t1 - t0, 1), "still_connected": still, "read_after_hold": comm,
         "adverts_heard_during_hold": bg.count_between(t0, t1)}
    rec.write(event="hold", **r)
    print("hold:", r)


async def phase_abort(address, config, body, rec, ev):
    """Injected fault: cut the transfer off mid-stream, then recover."""
    img = Image.open(io.BytesIO(body))
    dev, _ = await connect_with_recovery(address, config, rec=rec)
    prepared = await asyncio.to_thread(prepare_image, img, config=config or dev.config)
    sent = {"n": 0}
    t = time.perf_counter()
    try:
        await asyncio.wait_for(
            dev.upload_prepared_image(prepared, refresh_mode=RefreshMode.FULL,
                                      progress_callback=lambda s, tot: sent.update(n=s)), 0.6)
        aborted = False
    except asyncio.TimeoutError:
        aborted = True
    except Exception as e:  # noqa: BLE001
        aborted = f"error {type(e).__name__}: {e}"
    r = {"aborted_after_s": round(time.perf_counter() - t, 2), "aborted": aborted, "bytes_sent_before_abort": sent["n"]}
    try:
        await asyncio.wait_for(dev.__aexit__(None, None, None), 10)
        r["cleanup"] = "clean disconnect"
    except Exception as e:  # noqa: BLE001
        r["cleanup"] = f"disconnect error {e!r}"
    # recovery: a fresh connection, then a normal upload
    await asyncio.sleep(1.0)
    t = time.perf_counter()
    dev2, cinfo = await connect_with_recovery(address, config, rec=rec)
    n_ev = len(ev.events)
    t_up = time.perf_counter()
    try:
        await dev2.upload_prepared_image(prepared, refresh_mode=RefreshMode.FULL)
        r["recovery_upload"] = {"ok": ev.first("Display refresh complete", t_up) is not None,
                                "reconnect_s": round(t_up - t, 2), "upload_s": round(time.perf_counter() - t_up, 2),
                                "connect_attempts": len(cinfo["attempts"])}
    except Exception as e:  # noqa: BLE001
        r["recovery_upload"] = {"ok": False, "err": f"{type(e).__name__}: {e}"}
    await dev2.__aexit__(None, None, None)
    rec.write(event="abort", **r)
    print("abort+recover:", r)


async def phase_badaddr(address, rec):
    """Injected fault: nothing at this address. Must fail in bounded time with a clear phase."""
    bogus = address[:-2] + ("00" if not address.upper().endswith("00") else "01")
    t = time.perf_counter()
    info = {}
    try:
        await connect_with_recovery(bogus, None, rec=rec, attempts=2, backoff=(1.0,))
        outcome = "UNEXPECTED SUCCESS"
    except Exception as e:  # noqa: BLE001
        outcome = f"{type(e).__name__}: {e}"
    r = {"outcome": outcome, "elapsed_s": round(time.perf_counter() - t, 1)}
    rec.write(event="badaddr", **r)
    print("badaddr (bounded failure):", r)


async def main():
    ap = argparse.ArgumentParser()
    add_target_args(ap)
    ap.add_argument("--phases", default="fetch,upload,hold,abort,badaddr")
    ap.add_argument("--hold", type=float, default=30)
    args = ap.parse_args()
    phases = args.phases.split(",")

    address = await resolve_target(args.address, args.name)
    rec = Recorder("worker")
    ev = SdkEvents()
    print(f"target {mask(address)} phases={phases} log={rec.path}")

    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_address[1]}/image.png"

    dev, _ = await connect_with_recovery(address, None, rec=rec)   # interrogate once
    config = dev.config
    await dev.__aexit__(None, None, None)

    bg = Bg(address)
    async with BleakScanner(detection_callback=bg.cb):
        await asyncio.sleep(3)
        print(f"listener up; adverts heard in first 3 s: {len(bg.times)}")
        body = await phase_fetch(url, rec) if "fetch" in phases else open_any(url)
        if "upload" in phases:
            await phase_upload(address, config, body, bg, rec, ev)
            await asyncio.sleep(3)
        if "hold" in phases:
            await phase_hold(address, config, bg, rec, args.hold)
            await asyncio.sleep(3)
        if "abort" in phases:
            await phase_abort(address, config, body, rec, ev)
            await asyncio.sleep(3)
        if "badaddr" in phases:
            await phase_badaddr(address, rec)
        print("button events heard by the listener during the run:", len(bg.buttons))
    srv.shutdown()
    print("raw log:", rec.path)


def open_any(url):
    return fetch(url)[2]


if __name__ == "__main__":
    asyncio.run(main())
