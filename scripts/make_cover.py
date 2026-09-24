#!/usr/bin/env python3
"""
Generates docs/cover.jpg — the artwork Apple Podcasts and Spotify show.

3000x3000. Run once and commit the result; this does not run in CI.

    python scripts/make_cover.py
"""

import os
from PIL import Image, ImageDraw, ImageFont

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_PATH = os.path.join(ROOT, "docs", "cover.jpg")

SIZE = 3000
MARGIN = 230
INK = (12, 22, 42)
INK_LIGHT = (22, 38, 68)
WHITE = (246, 249, 253)
TEAL = (34, 198, 158)
SLATE = (139, 158, 186)

DISPLAY_FONTS = [
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/System/Library/Fonts/HelveticaNeue.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
]
BODY_FONTS = [
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
]


def load_font(candidates, size):
    for path in candidates:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    return ImageFont.load_default()


def ink(draw, text, font):
    return draw.textbbox((0, 0), text, font=font)


def draw_at(draw, left, top, text, font, fill):
    box = ink(draw, text, font)
    draw.text((left - box[0], top - box[1]), text, font=font, fill=fill)
    return box[2] - box[0], box[3] - box[1]


def tracked_width(draw, text, font, tracking):
    return sum(draw.textlength(c, font=font) for c in text) + tracking * max(len(text) - 1, 0)


def draw_tracked(draw, left, top, text, font, fill, tracking):
    box = ink(draw, text, font)
    x, y = left, top - box[1]
    for char in text:
        draw.text((x, y), char, font=font, fill=fill)
        x += draw.textlength(char, font=font) + tracking
    return box[3] - box[1]


def fit_font(draw, text, candidates, max_width, tracking=0, start=200):
    size = start
    while size > 24:
        font = load_font(candidates, size)
        if tracked_width(draw, text, font, tracking) <= max_width:
            return font
        size -= 4
    return load_font(candidates, 24)


def build():
    img = Image.new("RGB", (SIZE, SIZE), INK)
    draw = ImageDraw.Draw(img)

    # A rooflines-and-nodes motif, low right: property plus the network idea.
    base_y = SIZE - 430
    roof = [(1680, base_y), (1980, base_y - 300), (2280, base_y),
            (2280, base_y), (2580, base_y - 300), (2880, base_y)]
    draw.line(roof[:3], fill=INK_LIGHT, width=26, joint="curve")
    draw.line(roof[3:], fill=INK_LIGHT, width=26, joint="curve")
    for cx, cy in [(1980, base_y - 300), (2580, base_y - 300), (2280, base_y)]:
        draw.ellipse([cx - 26, cy - 26, cx + 26, cy + 26], fill=TEAL)
    draw.line([(1980, base_y - 300), (2580, base_y - 300)], fill=TEAL, width=8)

    draw.rectangle([MARGIN, 520, MARGIN + 320, 520 + 24], fill=TEAL)

    kicker = load_font(BODY_FONTS, 92)
    draw_tracked(draw, MARGIN, 360, "EVERY WEEKDAY", kicker, TEAL, 24)

    title_font = fit_font(draw, "UK PROPERTY", DISPLAY_FONTS, SIZE - 2 * MARGIN, start=460)
    cap = ink(draw, "UK PROPERTY", title_font)[3] - ink(draw, "UK PROPERTY", title_font)[1]
    y = 680
    draw_at(draw, MARGIN, y, "AI &", title_font, WHITE)
    draw_at(draw, MARGIN, y + cap + 64, "UK PROPERTY", title_font, TEAL)

    sub_y = y + 2 * cap + 230
    sub = "THE DAILY BRIEFING"
    sub_font = fit_font(draw, sub, BODY_FONTS, SIZE - 2 * MARGIN, tracking=20, start=150)
    draw_tracked(draw, MARGIN, sub_y, sub, sub_font, WHITE, 20)

    foot = "Estate agency  ·  Conveyancing  ·  Proptech"
    foot_font = fit_font(draw, foot, BODY_FONTS, SIZE - 2 * MARGIN, start=92)
    draw_at(draw, MARGIN, sub_y + 200, foot, foot_font, SLATE)

    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    img.save(OUT_PATH, "JPEG", quality=92, optimize=True, progressive=True)
    print(f"Wrote {OUT_PATH} ({SIZE}x{SIZE}, {os.path.getsize(OUT_PATH) / 1024:.0f} KB)")


if __name__ == "__main__":
    build()
