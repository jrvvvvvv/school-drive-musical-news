#!/usr/bin/env python3
"""Render the show artwork (docs/artwork.jpg, 3000x3000 RGB JPEG) with Pillow.

Original design: a newspaper page whose right-hand column of type lines runs
off the page and becomes a five-line musical staff carrying notes, over the
show wordmark. Wordmark and tagline come from podcast/show.json
("artwork": {"wordmark", "tagline"}), falling back to the show title.

Fonts: uses the first available of Poppins Bold (SIL OFL), DejaVu Sans Bold
(Bitstream Vera/DejaVu license), Liberation Sans Bold (SIL OFL), or a macOS
system font. No third-party logos or images are used.

    pip install pillow   # (add --break-system-packages on Debian/Ubuntu)
    python3 podcast/make_artwork.py [--out docs/artwork.jpg] [--size 3000]
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent

INK = (17, 19, 24)
PAPER = (246, 240, 226)
RED = (219, 58, 44)
GREY = (176, 168, 152)

BOLD_FONTS = [
    "/usr/share/fonts/truetype/google-fonts/Poppins-Bold.ttf",
    "/Library/Fonts/Poppins-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
]
MEDIUM_FONTS = [
    "/usr/share/fonts/truetype/google-fonts/Poppins-Medium.ttf",
    "/Library/Fonts/Poppins-Medium.ttf",
] + BOLD_FONTS


def first_font(paths: list[str]) -> str:
    for p in paths:
        if Path(p).exists():
            return p
    raise SystemExit("make_artwork: no usable font found; install Poppins or DejaVu Sans")


def fit_font(path: str, text: str, max_w: float, max_h: float, tracking: float = 0.0):
    """Largest font size whose rendered text fits max_w x max_h."""
    lo, hi = 10, 4000
    while lo < hi:
        mid = (lo + hi + 1) // 2
        f = ImageFont.truetype(path, mid)
        w = text_width(f, text, tracking * mid)
        b = f.getbbox(text)
        if w <= max_w and (b[3] - b[1]) <= max_h:
            lo = mid
        else:
            hi = mid - 1
    return ImageFont.truetype(path, lo)


def text_width(font, text: str, track: float) -> float:
    if not track:
        b = font.getbbox(text)
        return b[2] - b[0]
    return sum(font.getlength(ch) for ch in text) + track * (len(text) - 1)


def draw_tracked(d: ImageDraw.ImageDraw, xy, text: str, font, fill, track: float):
    x, y = xy
    for ch in text:
        d.text((x, y), ch, font=font, fill=fill)
        x += font.getlength(ch) + track


def ellipse_poly(cx, cy, rx, ry, angle_deg, n=64):
    a = math.radians(angle_deg)
    ca, sa = math.cos(a), math.sin(a)
    pts = []
    for i in range(n):
        t = 2 * math.pi * i / n
        x, y = rx * math.cos(t), ry * math.sin(t)
        pts.append((cx + x * ca - y * sa, cy + x * sa + y * ca))
    return pts


def render(size: int, wordmark: str, tagline: str) -> Image.Image:
    SS = 2  # supersample for smooth edges
    W = size * SS
    u = W / 3000.0  # design units are on a 3000 grid

    def U(v):
        return v * u

    img = Image.new("RGB", (W, W), RED)
    d = ImageDraw.Draw(img)

    # ---- newspaper page -------------------------------------------------
    px0, py0, px1, py1 = U(260), U(250), U(1560), U(1640)
    shadow = U(34)
    d.rectangle([px0 + shadow, py0 + shadow, px1 + shadow, py1 + shadow], fill=(150, 34, 26))
    d.rectangle([px0, py0, px1, py1], fill=PAPER)

    m = U(80)  # page margin
    ix0, ix1 = px0 + m, px1 - m
    # masthead: heavy rule, nameplate bar, thin rule
    d.rectangle([ix0, py0 + U(80), ix1, py0 + U(96)], fill=INK)
    d.rectangle([ix0 + U(140), py0 + U(130), ix1 - U(140), py0 + U(230)], fill=INK)
    d.rectangle([ix0, py0 + U(266), ix1, py0 + U(274)], fill=INK)
    # headline bars
    d.rectangle([ix0, py0 + U(330), ix1 - U(60), py0 + U(410)], fill=INK)
    d.rectangle([ix0, py0 + U(440), ix0 + (ix1 - ix0) * 0.62, py0 + U(520)], fill=INK)

    # three columns of type
    gutter = U(60)
    col_w = (ix1 - ix0 - 2 * gutter) / 3
    line_h = U(22)
    pitch = U(64)
    top = py0 + U(600)
    bottom = py1 - m
    rows = int((bottom - top) // pitch) + 1
    staff_rows = list(range(4, 9))  # rows of the right column that become the staff
    import random
    rng = random.Random(7)
    for c in range(3):
        cx0 = ix0 + c * (col_w + gutter)
        for r in range(rows):
            y = top + r * pitch
            if y + line_h > bottom:
                break
            if c == 2 and r in staff_rows:
                continue  # drawn as staff below
            frac = 1.0 if (r + c) % 5 else rng.uniform(0.45, 0.75)
            if c == 0 and r == 0:
                frac = 0.8
            d.rectangle([cx0, y, cx0 + col_w * frac, y + line_h], fill=GREY)

    # ---- staff: right column lines run off the page and start to sing ----
    col3_x0 = ix0 + 2 * (col_w + gutter)
    staff_y0 = top + staff_rows[0] * pitch + line_h / 2
    gap = pitch  # staff line spacing equals type pitch on the page
    x_start = col3_x0
    x_page_end = px1 + shadow
    x_end = U(3000) + U(40)
    lw = U(16)

    def wave(x):
        # flat on the page, then a rising, gently curving sweep
        if x <= x_page_end:
            return 0.0
        t = (x - x_page_end) / (x_end - x_page_end)
        return -U(560) * (t ** 1.35) + U(70) * math.sin(t * math.pi * 1.6)

    def staff_y(i, x):
        return staff_y0 + i * gap + wave(x)

    steps = 400
    for i in range(5):
        pts = []
        for k in range(steps + 1):
            x = x_start + (x_end - x_start) * k / steps
            pts.append((x, staff_y(i, x)))
        d.line(pts, fill=INK, width=int(lw), joint="curve")

    # notes riding the staff (position given as staff step: 0 = top line,
    # 1 = first space, ... 8 = bottom line)
    def note(x, step, stem_up=True, filled=True):
        yc = staff_y(0, x) + step * gap / 2
        rx, ry = gap * 0.80, gap * 0.58
        slope = math.degrees(math.atan2(staff_y(0, x + 5) - staff_y(0, x - 5), 10))
        poly = ellipse_poly(x, yc, rx, ry, -22 + slope)
        d.polygon(poly, fill=INK)
        if not filled:
            d.polygon(ellipse_poly(x, yc, rx * 0.55, ry * 0.42, -38 + slope), fill=RED)
        sw = U(24)
        if stem_up:
            sx = x + rx * 0.86
            return (sx, yc - U(10)), (sx, yc - gap * 3.6), sw
        sx = x - rx * 0.86
        return (sx, yc + U(10)), (sx, yc + gap * 3.6), sw

    # a pair of beamed eighths, a quarter, then a beamed rising pair of sixteenths
    def stem(p0, p1, sw):
        d.rectangle([p1[0] - sw / 2, min(p0[1], p1[1]), p1[0] + sw / 2, max(p0[1], p1[1])], fill=INK)

    def beam(a, b, thick):
        (x0, y0), (x1, y1) = a, b
        d.polygon([(x0 - U(9), y0), (x1 + U(9), y1), (x1 + U(9), y1 + thick), (x0 - U(9), y0 + thick)], fill=INK)

    xs = [U(1800), U(2050), U(2330), U(2590), U(2830)]
    steps_ = [5, 3, 4, 2, 0]
    stems = [note(x, s) for x, s in zip(xs, steps_)]
    for (p0, p1, sw) in stems:
        stem(p0, p1, sw)
    bt = gap * 0.62
    beam(stems[0][1], stems[1][1], bt)
    beam(stems[3][1], stems[4][1], bt)
    beam((stems[3][1][0], stems[3][1][1] + bt * 1.55), (stems[4][1][0], stems[4][1][1] + bt * 1.55), bt)

    # ---- wordmark ------------------------------------------------------
    bold = first_font(BOLD_FONTS)
    medium = first_font(MEDIUM_FONTS)
    wm_box_w, wm_box_h = U(2640), U(620)
    wf = fit_font(bold, wordmark, wm_box_w, wm_box_h, tracking=-0.01)
    track = -0.01 * wf.size
    ww = text_width(wf, wordmark, track)
    bb = wf.getbbox(wordmark)
    wx = (W - ww) / 2 - bb[0]
    wy = U(1900) - bb[1]
    draw_tracked(d, (wx, wy), wordmark, wf, PAPER, track)

    # tagline between two short rules
    tf = fit_font(medium, tagline, U(1700), U(140), tracking=0.30)
    ttrack = 0.30 * tf.size
    tw = text_width(tf, tagline, ttrack)
    tb = tf.getbbox(tagline)
    ty = U(2640)
    tx = (W - tw) / 2
    draw_tracked(d, (tx - tb[0], ty - tb[1]), tagline, tf, INK, ttrack)
    th = tb[3] - tb[1]
    rule_y = ty + th / 2
    d.rectangle([tx - U(260), rule_y - U(7), tx - U(70), rule_y + U(7)], fill=INK)
    d.rectangle([tx + tw + U(70), rule_y - U(7), tx + tw + U(260), rule_y + U(7)], fill=INK)

    return img.resize((size, size), Image.LANCZOS)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=str(ROOT / "docs" / "artwork.jpg"))
    ap.add_argument("--size", type=int, default=3000)
    ap.add_argument("--quality", type=int, default=88)
    args = ap.parse_args()

    show = json.loads((ROOT / "podcast" / "show.json").read_text(encoding="utf-8"))
    art = show.get("artwork", {})
    wordmark = art.get("wordmark") or show["title"].upper()
    tagline = art.get("tagline") or ""

    img = render(args.size, wordmark, tagline)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out, "JPEG", quality=args.quality, optimize=True, progressive=False, subsampling=0)
    print(f"make_artwork: wrote {out} {img.size[0]}x{img.size[1]} {out.stat().st_size // 1024} KB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
