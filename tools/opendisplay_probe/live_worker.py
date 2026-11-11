"""RESEARCH PROTOTYPE: live OpenDisplay worker. NOT production code.

Polls the daemon's status endpoint (read-only, same call the Presto makes),
renders one 800x480 1-bit page per card, uploads to the OpenDisplay panel only
when what is drawn actually changes, and pages with the physical keys:
KEY2 = previous, KEY3 = next, KEY1 = logged only (reserved).

Design under test (from the probe results):
  * passive advert listener always on (buttons + liveness, no GATT connection)
  * connect only to upload; direct connect by address; bounded retry
  * FAST refresh for normal updates, FULL every Nth upload / every 30 min
  * rapid presses are coalesced into one navigation (a refresh takes seconds)
  * presses are counted from the 4-bit per-key counter, not from events

Secrets: the daemon URL and token come from PRESTO_DAEMON_URL and PRESTO_TOKEN
in the environment. They are never printed or logged. The panel address comes
from --address / OD_PROBE_ADDRESS.

  py -3.13 tools/opendisplay_probe/live_worker.py --minutes 60
  py -3.13 tools/opendisplay_probe/live_worker.py --render-only OUTDIR --payload-file sample.json   (no BLE)
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

W, H = 800, 480


# ----------------------------------------------------------------- rendering
def font(n):
    return ImageFont.load_default(size=n)


def _local(iso):
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone()
    except Exception:  # noqa: BLE001
        return None


def _bar(d, x, y, w, h, pct, marks=()):
    d.rectangle([x, y, x + w, y + h], outline=0, width=2)
    if not isinstance(pct, (int, float)):
        return
    fw = int((w - 4) * max(0, min(100, pct)) / 100)
    if fw > 0:
        d.rectangle([x + 2, y + 2, x + 2 + fw, y + h - 2], fill=0)
    for m in marks:  # threshold ticks: black on white, white on black
        mx = x + 2 + int((w - 4) * m / 100)
        d.line([mx, y + 2, mx, y + h - 2], fill=1 if mx <= x + 2 + fw else 0, width=1)


_FLAG = {"critical": "CRIT", "warning": "WARN", "stale": "STALE", "error": "ERR", "offline": "OFF"}


def _overview(d, payload):
    cards = {c.get("id"): c for c in payload.get("cards", []) if isinstance(c, dict)}
    order = [i for i in payload.get("card_order", []) if i in cards]
    sources = {s.get("id"): s for s in payload.get("sources", []) if isinstance(s, dict)}
    rows = [sources[i] for i in order if i in sources] or list(sources.values())
    y = 94
    for s in rows[:6]:
        status = str(s.get("status") or "")
        wins = [w for w in ((s.get("quota") or {}).get("windows") or [])
                if isinstance(w.get("used_percent"), (int, float))]
        name = str(s.get("name") or s.get("id") or "")
        size = 28
        while size > 18 and d.textlength(name, font=font(size)) > 230:
            size -= 2
        d.text((24, y + 4), name, font=font(size), fill=0)
        if wins:
            w = max(wins, key=lambda x: x["used_percent"])      # the tightest window
            _bar(d, 270, y + 6, 250, 28, w["used_percent"], s.get("marks") or ())
            d.text((534, y + 2), f"{w['used_percent']:.0f}%", font=font(30), fill=0)
            d.text((612, y + 12), str(w.get("label") or "")[:8], font=font(20), fill=0)
        else:
            primary = str((cards.get(s.get("id")) or {}).get("primary") or "")
            d.text((270, y + 8), primary[:34], font=font(22), fill=0)
        flag = _FLAG.get(status)
        if flag:
            fw = int(d.textlength(flag, font=font(20))) + 14
            d.rectangle([W - 24 - fw, y + 6, W - 24, y + 36], fill=0)
            d.text((W - 24 - fw + 7, y + 10), flag, font=font(20), fill=1)
        y += 56


def pages_from(payload):
    """One page per card, in the daemon's card order, joined to its source by id."""
    cards = {c.get("id"): c for c in payload.get("cards", []) if isinstance(c, dict)}
    order = [i for i in payload.get("card_order", []) if i in cards] or list(cards)
    sources = {s.get("id"): s for s in payload.get("sources", []) if isinstance(s, dict)}
    return [(cards[i], sources.get(i)) for i in order] or [({"title": "No data", "status": "unknown"}, None)]


def render(card, source, idx, n, updated_iso, stale_since, show_clock=True, payload=None):
    img = Image.new("1", (W, H), 1)
    d = ImageDraw.Draw(img)
    status = str(card.get("status") or "unknown")
    d.text((24, 12), str(card.get("title") or "")[:28], font=font(44), fill=0)
    chip = status.upper()[:8]
    cw = int(d.textlength(chip, font=font(30))) + 24
    if status in ("ok", "healthy", "unknown"):
        d.rectangle([W - 24 - cw, 18, W - 24, 62], outline=0, width=3)
        d.text((W - 24 - cw + 12, 24), chip, font=font(30), fill=0)
    else:
        d.rectangle([W - 24 - cw, 18, W - 24, 62], fill=0)
        d.text((W - 24 - cw + 12, 24), chip, font=font(30), fill=1)
    d.line([24, 76, W - 24, 76], fill=0, width=2)

    windows = ((source or {}).get("quota") or {}).get("windows") or []
    if card.get("id") == "dash" and payload:
        _overview(d, payload)
    elif windows:
        marks = (source or {}).get("marks") or []
        y = 96
        for w in windows[:4]:
            pct = w.get("used_percent")
            d.text((24, y), str(w.get("label") or "")[:10], font=font(28), fill=0)
            bx, bw, bh = 190, 430, 34
            _bar(d, bx, y + 2, bw, bh, pct, marks)
            d.text((bx + bw + 16, y), f"{pct:.0f}%" if isinstance(pct, (int, float)) else "n/a", font=font(32), fill=0)
            reset = _local(w.get("resets_at") or "")
            stale = "   (stale)" if w.get("state") == "stale" else ""
            if reset:
                d.text((bx, y + 42), "resets " + reset.strftime("%a %H:%M") + stale, font=font(20), fill=0)
            y += 84
            if y > 370:
                break
    else:
        y = 96
        if card.get("primary"):
            d.text((24, y), str(card["primary"])[:46], font=font(34), fill=0)
            y += 50
        if card.get("secondary"):
            d.text((24, y), str(card["secondary"])[:70], font=font(22), fill=0)
            y += 40
        for line in (card.get("detail") or [])[:6]:
            d.text((24, y), str(line)[:60], font=font(26), fill=0)
            y += 36

    # footer
    d.line([24, 436, W - 24, 436], fill=0, width=1)
    d.text((24, 446), "KEY2 < prev    next > KEY3", font=font(20), fill=0)
    d.text((W // 2 - 30, 446), f"{idx + 1}/{n}", font=font(22), fill=0)
    if stale_since:
        d.text((W - 330, 446), "DATA STALE", font=font(20), fill=0)
    elif show_clock:
        t = _local(updated_iso or "")
        if t:
            d.text((W - 170, 446), "upd " + t.strftime("%H:%M"), font=font(20), fill=0)
    return img


def content_key(card, source, idx, n, stale, payload=None):
    img = render(card, source, idx, n, None, stale, show_clock=False, payload=payload)
    return hashlib.sha1(img.tobytes()).hexdigest()


# ------------------------------------------------------------------ network
def describe_error(e):
    """A short reason that never contains the URL or the token."""
    if isinstance(e, urllib.error.HTTPError):
        hint = {401: " (token rejected)", 403: " (forbidden)", 404: " (wrong path or not the daemon)"}.get(e.code, "")
        return f"HTTP {e.code}{hint}"
    if isinstance(e, urllib.error.URLError):
        return f"URLError: {e.reason}"
    return f"{type(e).__name__}: {e}"


def fetch_status(url, token, timeout=10):
    req = urllib.request.Request(url.rstrip("/") + "/api/v1/status",
                                 headers={"Authorization": "Bearer " + token})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


# --------------------------------------------------------------------- main
def check_daemon():
    url, token = os.environ.get("PRESTO_DAEMON_URL"), os.environ.get("PRESTO_TOKEN")
    print("PRESTO_DAEMON_URL set:", bool(url), "| PRESTO_TOKEN set:", bool(token))
    if not (url and token):
        return
    from urllib.parse import urlsplit
    u = urlsplit(url if "://" in url else "//" + url)
    print(f"scheme={u.scheme or '(none - add http://)'}  port={u.port or '(default)'}  path={u.path or '(none)'}")
    t = time.perf_counter()
    try:
        p = fetch_status(url if "://" in url else "http://" + url, token)
    except Exception as e:  # noqa: BLE001
        print("FAILED:", describe_error(e))
        return
    print(f"OK in {(time.perf_counter() - t) * 1000:.0f} ms; keys={sorted(p)}; "
          f"cards={len(p.get('cards', []))}; sources={len(p.get('sources', []))}")


def render_only(args):
    payload = json.loads(Path(args.payload_file).read_text(encoding="utf-8"))
    pages = pages_from(payload)
    out = Path(args.render_only)
    out.mkdir(parents=True, exist_ok=True)
    for i, (card, source) in enumerate(pages):
        render(card, source, i, len(pages), payload.get("updated_at"), None, payload=payload).convert("L").save(out / f"page{i + 1}.png")
    print(f"wrote {len(pages)} pages to {out}")


async def live(args):
    from bleak import BleakScanner
    from opendisplay import MANUFACTURER_ID, RefreshMode, parse_advertisement, prepare_image

    from common import Recorder, SdkEvents, connect_with_recovery, mask, resolve_target, stats

    url, token = os.environ.get("PRESTO_DAEMON_URL"), os.environ.get("PRESTO_TOKEN")
    if not url or not token:
        raise SystemExit("Set PRESTO_DAEMON_URL and PRESTO_TOKEN in this shell (they are never printed).")
    address = await resolve_target(args.address, args.name)
    rec = Recorder("live")
    ev = SdkEvents()
    print(f"panel {mask(address)}  daemon: configured (not shown)  minutes={args.minutes}  log={rec.path}")

    keymap = dict(kv.split("=") for kv in args.key_map.split(","))
    id_to_key = {int(keymap["prev"]): "prev", int(keymap["next"]): "next", int(keymap["reserved"]): "reserved"}

    st = {"payload": None, "fails": 0, "stale_since": None, "target": 0, "shown": None, "uploads": 0,
          "last_upload": 0.0, "last_full": 0.0, "pending_press_t": None, "nav_dirty": False, "adv_gaps": [],
          "last_adv": None, "battery_mv": None, "presses": {"prev": 0, "next": 0, "reserved": 0}}
    wake = asyncio.Event()
    t_start = time.monotonic()
    deadline = t_start + args.minutes * 60

    # --- listener: button presses from the per-key 4-bit counter ----------
    counts, last_raw, t_listen = {}, [None], time.monotonic()

    def on_adv(device, adv_data):
        if device.address.upper() != address.upper():
            return
        raw = adv_data.manufacturer_data.get(MANUFACTURER_ID)
        if raw is None:
            return
        now = time.perf_counter()
        if st["last_adv"] is not None and now - st["last_adv"] > 5:
            st["adv_gaps"].append(round(now - st["last_adv"], 1))
        st["last_adv"] = now
        try:
            adv = parse_advertisement(raw)
        except Exception:  # noqa: BLE001
            return
        st["battery_mv"] = adv.battery_mv
        b = adv.dynamic_data[0]
        if b == last_raw[0]:
            return
        last_raw[0] = b
        bid, cnt, down = b & 7, (b >> 3) & 0xF, bool(b & 0x80)
        prev = counts.get(bid)
        if prev is None:
            # First sight of this key. At start-up the byte may hold a stale 'down' flag,
            # so only trust it once the listener has been up for a few seconds.
            presses = 1 if (down and time.monotonic() - t_listen > 8) else 0
        else:
            presses = (cnt - prev) % 16
        counts[bid] = cnt
        key = id_to_key.get(bid)
        rec.write(event="advert_change", id=bid, count=cnt, down=down, presses=presses, key=key)
        if presses and key:
            st["presses"][key] += presses
            if key in ("prev", "next") and st["payload"]:
                n = len(pages_from(st["payload"]))
                st["target"] = (st["target"] + (presses if key == "next" else -presses)) % n
                st["nav_dirty"] = True
                st["pending_press_t"] = st["pending_press_t"] or now
                wake.set()

    # --- poller -------------------------------------------------------------
    async def poller():
        while time.monotonic() < deadline:
            t = time.perf_counter()
            try:
                st["payload"] = await asyncio.to_thread(fetch_status, url, token)
                st["fails"], st["stale_since"] = 0, None
                rec.write(event="poll", ok=True, ms=round((time.perf_counter() - t) * 1000))
            except Exception as e:  # noqa: BLE001
                st["fails"] += 1
                if st["fails"] >= 3 and not st["stale_since"]:
                    st["stale_since"] = datetime.now(timezone.utc).isoformat()
                why = describe_error(e)
                rec.write(event="poll", ok=False, err=why, fails=st["fails"])
                if st["fails"] in (1, 3) or st["fails"] % 10 == 0:
                    print(f"poll failed ({st['fails']}x): {why}", flush=True)
            wake.set()
            await asyncio.sleep(args.poll)

    # --- uploader -------------------------------------------------------------
    config = None
    results = []

    async def uploader():
        nonlocal config
        backoff = 0.0
        while time.monotonic() < deadline:
            try:
                await asyncio.wait_for(wake.wait(), 5)
            except asyncio.TimeoutError:
                pass
            wake.clear()
            if st["payload"] is None and st["fails"] < 3:
                continue
            if st["payload"] is None:
                payload = {"cards": [{"id": "x", "title": "Presto AI Dash", "status": "unknown",
                                      "primary": "Daemon unreachable", "secondary": "Retrying; no data yet"}]}
            else:
                payload = st["payload"]
            if st["nav_dirty"]:
                await asyncio.sleep(0.8)  # coalesce rapid presses
                wake.clear()
            pages = pages_from(payload)
            idx = st["target"] % len(pages)
            card, source = pages[idx]
            key = content_key(card, source, idx, len(pages), bool(st["stale_since"]), payload)
            if key == st["shown"]:
                st["nav_dirty"], st["pending_press_t"] = False, None
                continue
            nav = st["nav_dirty"]
            if not nav and time.monotonic() - st["last_upload"] < args.min_data_interval:
                continue  # data-only change: rate-limited
            if backoff:
                await asyncio.sleep(backoff)
            full = (st["uploads"] % args.full_every == 0) or (time.monotonic() - st["last_full"] > 1800)
            img = render(card, source, idx, len(pages), payload.get("updated_at"), st["stale_since"], payload=payload)
            t_press, t0 = st["pending_press_t"], time.perf_counter()
            r = {"page": idx + 1, "of": len(pages), "mode": "full" if full else "fast", "nav": nav}
            try:
                dev, cinfo = await connect_with_recovery(address, config, rec=rec)
                config = config or dev.config
                r["connect_s"] = round(time.perf_counter() - t0, 2)
                prepared = await asyncio.to_thread(prepare_image, img, config=config)
                t_up = time.perf_counter()
                await dev.upload_prepared_image(
                    prepared, refresh_mode=RefreshMode.FULL if full else RefreshMode.FAST)
                done = time.perf_counter()
                r["confirmed"] = ev.first("Display refresh complete", t_up) is not None
                r["upload_s"] = round(done - t_up, 2)
                await dev.__aexit__(None, None, None)
                r["total_s"] = round(time.perf_counter() - t0, 2)
                if t_press:
                    r["press_to_panel_s"] = round(time.perf_counter() - t_press, 2)
                r["ok"] = r["confirmed"]
                if r["ok"]:
                    st["shown"], st["uploads"] = key, st["uploads"] + 1
                    st["last_upload"] = time.monotonic()
                    if full:
                        st["last_full"] = time.monotonic()
                    st["nav_dirty"], st["pending_press_t"], backoff = False, None, 0.0
            except Exception as e:  # noqa: BLE001
                r["ok"], r["err"] = False, f"{type(e).__name__}: {e}"
                backoff = min(60.0, (backoff or 5.0) * 2)   # bounded, growing
                wake.set()
            results.append(r)
            rec.write(event="upload", **r)
            print("upload:", {k: v for k, v in r.items() if k != "nav"}, flush=True)

    async def ticker():
        while time.monotonic() < deadline:
            await asyncio.sleep(300)
            rec.write(event="heartbeat", battery_mv=st["battery_mv"], uploads=st["uploads"], presses=st["presses"],
                      adv_gaps_over_5s=len(st["adv_gaps"]), max_gap=max(st["adv_gaps"], default=0))
            print(f"[{(time.monotonic() - t_start) / 60:.0f} min] battery={st['battery_mv']}mV uploads={st['uploads']} "
                  f"presses={st['presses']} max_adv_gap={max(st['adv_gaps'], default=0)}s", flush=True)

    # interrogate once
    dev, _ = await connect_with_recovery(address, None, rec=rec)
    config = dev.config
    await dev.__aexit__(None, None, None)

    async with BleakScanner(detection_callback=on_adv):
        print("running. Use KEY2 (prev) / KEY3 (next) on the display; Ctrl+C to stop early.", flush=True)
        try:
            await asyncio.gather(poller(), uploader(), ticker())
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass

    ok = [r for r in results if r.get("ok")]
    nav = [r for r in ok if r.get("nav") and "press_to_panel_s" in r]
    print("\n== summary ==")
    print(f"uploads ok {len(ok)}/{len(results)}; failures: {[r.get('err') for r in results if not r.get('ok')]}")
    print("upload total_s:", stats([r["total_s"] for r in ok]))
    print("press-to-panel (host-side, from advert that revealed the press):", stats([r["press_to_panel_s"] for r in nav]))
    print(f"presses {st['presses']}; advert gaps >5s: {len(st['adv_gaps'])}, max {max(st['adv_gaps'], default=0)}s; "
          f"battery {st['battery_mv']} mV")
    print("raw log:", rec.path)


def main():
    from common import add_target_args
    ap = argparse.ArgumentParser()
    add_target_args(ap)
    ap.add_argument("--minutes", type=float, default=60)
    ap.add_argument("--poll", type=float, default=60, help="seconds between status polls")
    ap.add_argument("--min-data-interval", type=float, default=120, help="min seconds between data-only refreshes")
    ap.add_argument("--full-every", type=int, default=10, help="every Nth upload is a FULL refresh")
    ap.add_argument("--key-map", default="prev=1,next=2,reserved=0", help="button_id per role (measured on this unit)")
    ap.add_argument("--render-only", metavar="OUTDIR")
    ap.add_argument("--check", action="store_true", help="call the daemon once, report the result, no Bluetooth")
    ap.add_argument("--payload-file")
    args = ap.parse_args()
    if args.check:
        check_daemon()
    elif args.render_only:
        render_only(args)
    else:
        asyncio.run(live(args))


if __name__ == "__main__":
    main()
