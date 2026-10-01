#!/usr/bin/env python3
"""Render the docs favicon from the canonical dispatch v2 mark geometry.

The mark is drawn from primitives (no hand-edited pixel art), so the output is
reproducible byte-for-byte. Geometry is the locked v2 mark in the 32x32 unit
viewBox used by the site's icon set:

  ring     rect x=4 y=4 width=24 height=24 rx=7, stroke-width 2.5
  chevron  M10.5 18.5 L16 14 L21.5 18.5, stroke-width 2.4, round cap/join
  stroke   #4ade80

The mask is supersampled at 4x the target size and LANCZOS-downscaled; the
RGBA is a flat #4ade80 layer that receives the mask as its alpha, so no dark
edge fringes appear.

Output: docs/images/favicon.png (48x48 RGBA, transparent background).

Run from anywhere:

    uv run --with Pillow python3 scripts/gen-docs-favicon.py

Re-running is byte-identical (no timestamps, no randomness).
"""

from pathlib import Path

from PIL import Image, ImageChops, ImageDraw

# --- locked mark geometry (32x32 viewBox units) -----------------------------

VIEWBOX = 32.0
RING = {"x": 4.0, "y": 4.0, "size": 24.0, "radius": 7.0, "stroke": 2.5}
CHEVRON = [(10.5, 18.5), (16.0, 14.0), (21.5, 18.5)]
CHEVRON_STROKE = 2.4
SIGNAL = (0x4A, 0xDE, 0x80)

# --- raster standard --------------------------------------------------------

SIZE = 48  # final favicon footprint (matches the theme default)
SUPERSAMPLE = 4  # render the mask at 4x, then downscale
SCALE = SIZE * SUPERSAMPLE / VIEWBOX

OUT = Path(__file__).resolve().parent.parent / "docs" / "images" / "favicon.png"


def _rounded_rect(draw, x0, y0, x1, y1, radius, fill):
    draw.rounded_rectangle(
        [x0 * SCALE, y0 * SCALE, x1 * SCALE, y1 * SCALE],
        radius=radius * SCALE,
        fill=fill,
    )


def ring_mask():
    """Stroke the ring as an outer rounded rect filled 255 with the inner one
    erased to 0, both on a single 'L' mask."""
    mask = Image.new("L", (SIZE * SUPERSAMPLE,) * 2, 0)
    draw = ImageDraw.Draw(mask)
    inset = RING["stroke"] / 2.0
    x0, y0 = RING["x"], RING["y"]
    x1, y1 = x0 + RING["size"], y0 + RING["size"]
    _rounded_rect(draw, x0 - inset, y0 - inset, x1 + inset, y1 + inset,
                  RING["radius"] + inset, 255)
    _rounded_rect(draw, x0 + inset, y0 + inset, x1 - inset, y1 - inset,
                  RING["radius"] - inset, 0)
    return mask


def chevron_mask():
    """Stroke the chevron polyline with round caps and round joins."""
    mask = Image.new("L", (SIZE * SUPERSAMPLE,) * 2, 0)
    draw = ImageDraw.Draw(mask)
    width = CHEVRON_STROKE * SCALE
    points = [(x * SCALE, y * SCALE) for x, y in CHEVRON]
    draw.line(points, fill=255, width=round(width), joint="curve")
    radius = width / 2.0
    for x, y in points:
        draw.ellipse([x - radius, y - radius, x + radius, y + radius], fill=255)
    return mask


def build():
    mask = ImageChops.lighter(ring_mask(), chevron_mask())
    mask = mask.resize((SIZE, SIZE), Image.LANCZOS)
    icon = Image.new("RGBA", (SIZE, SIZE), (*SIGNAL, 255))
    icon.putalpha(mask)
    return icon


def main():
    OUT.parent.mkdir(parents=True, exist_ok=True)
    icon = build()
    icon.save(OUT, "PNG", optimize=True)
    print(f"wrote {OUT.relative_to(OUT.parents[2])} {icon.size} {icon.mode}")


if __name__ == "__main__":
    main()
