"""RESEARCH PROBE helper: the 800x480 1-bit test card."""

from datetime import datetime

from PIL import Image, ImageDraw, ImageFont

W, H = 800, 480


def make_card(seq: int, total: int, ok: bool = True, label: str = "") -> Image.Image:
    img = Image.new("1", (W, H), 1)
    d = ImageDraw.Draw(img)
    f = lambda n: ImageFont.load_default(size=n)  # noqa: E731
    d.rectangle([0, 0, W - 1, H - 1], outline=0, width=4)
    d.text((24, 16), "Presto AI Dash", font=f(56), fill=0)
    d.text((24, 84), "OpenDisplay 4.26in test card", font=f(26), fill=0)
    if label:
        d.text((470, 84), f"MODE: {label.upper()}", font=f(30), fill=0)
    d.line([24, 126, W - 24, 126], fill=0, width=2)
    d.text((24, 140), f"Update #{seq:02d} of {total}", font=f(44), fill=0)
    d.text((24, 200), datetime.now().strftime("%Y-%m-%d  %H:%M:%S"), font=f(34), fill=0)
    # text size ladder
    y = 256
    for n in (12, 16, 22, 30):
        d.text((24, y), f"{n}px: Claude 62%  Codex 18%  Ollama 4%", font=f(n), fill=0)
        y += n + 8
    # status indicator (changes with seq so each frame differs)
    cx, cy, r = 700, 190, 44
    if ok:
        d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=0)
        d.text((cx - 26, cy - 18), "OK", font=f(40), fill=1)
    else:
        d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=0, width=6)
    # progress bar
    bx, by, bw, bh = 24, 410, W - 48, 36
    d.rectangle([bx, by, bx + bw, by + bh], outline=0, width=3)
    fill_w = int((bw - 6) * seq / total)
    d.rectangle([bx + 3, by + 3, bx + 3 + fill_w, by + bh - 3], fill=0)
    d.text((bx + bw - 90, by - 30), f"{int(100 * seq / total)}%", font=f(26), fill=0)
    return img
