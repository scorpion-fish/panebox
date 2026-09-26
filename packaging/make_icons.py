#!/usr/bin/env python3
"""Generate PaneBox launcher icons (hicolor PNG set) with PIL.

Rounded orange tile with a 2x2 white pane grid — one pane offset so the
"pane" metaphor reads at 16 px.
"""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw

SIZES = [512, 256, 128, 64, 48, 32, 24, 16]
BG = (224, 93, 32, 255)  # the widget accent orange
BG_DARK = (198, 74, 20, 255)  # lower edge
PANE = (255, 255, 255, 255)
PANE_DROPPED = (255, 224, 204, 255)


def rounded_tile(size: int) -> Image.Image:
    im = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    # vertical two-tone fill for a little depth
    r = max(2, size * 42 // 256)
    d.rounded_rectangle([0, 0, size - 1, size // 2], radius=r, fill=BG)
    d.rounded_rectangle([0, size // 2, size - 1, size - 1], radius=r, fill=BG_DARK)
    d.rectangle([0, size // 4, size - 1, size * 3 // 4], fill=BG)
    d.rounded_rectangle([0, 0, size - 1, size - 1], radius=r, outline=BG, width=1)

    pad = size * 48 // 256
    gap = size * 18 // 256
    pane = (size - 2 * pad - gap) // 2
    rad = max(1, size * 14 // 256)
    x0, y0 = pad, pad
    x1, y1 = x0 + pane, y0 + pane
    # top-left, top-right, bottom-left: white; bottom-right: shifted out
    d.rounded_rectangle([x0, y0, x0 + pane, y0 + pane], radius=rad, fill=PANE)
    d.rounded_rectangle([x1 + gap, y0, x1 + gap + pane, y0 + pane], radius=rad, fill=PANE)
    d.rounded_rectangle([x0, y1 + gap, x0 + pane, y1 + gap + pane], radius=rad, fill=PANE)
    ox = x1 + gap + pane - pane * 12 // 100
    oy = y1 + gap + pane - pane * 12 // 100
    d.rounded_rectangle(
        [
            min(ox, size - pad - pane),
            min(oy, size - pad - pane),
            min(ox, size - pad - pane) + pane,
            min(oy, size - pad - pane) + pane,
        ],
        radius=rad,
        fill=PANE_DROPPED,
    )
    return im


def main(out_dir: str) -> None:
    base = Path(out_dir)
    base.mkdir(parents=True, exist_ok=True)
    for size in SIZES:
        target = base / f"panebox-{size}x{size}.png" if size != 512 else base / "panebox.png"
        rounded_tile(size).save(target)
        print(f"wrote {target}")
    (base / ".DirIconPlaceholder").unlink(missing_ok=True)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "dist/icons")
