#!/usr/bin/env python3
"""Generate the PlanaLyrics hicolor icon set.

Reads packaging/icon-source.png when present (any square artwork works), and
otherwise draws the built-in placeholder. Artwork is normalised so the result
sits in the dock at the same optical size as the icons around it:

* an image with no transparency is treated as a picture on a solid background --
  it is trimmed to its content, re-centred on a square tile of that background
  colour, given the platform's rounded corners and scaled down;
* an image with transparency is assumed to be a shaped icon already, and is only
  trimmed of empty margin and scaled.

Run it after replacing the artwork, then re-run packaging/install.sh.
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

import cairo
import gi
import numpy as np

gi.require_version("Gtk", "3.0")
gi.require_version("Pango", "1.0")
gi.require_version("PangoCairo", "1.0")

from gi.repository import Pango, PangoCairo  # noqa: E402

SIZES = (16, 24, 32, 48, 64, 128, 256, 512, 1024)
MASTER = 1024
OUT_DIR = Path(__file__).resolve().parent / "icons"
SOURCE = Path(__file__).resolve().parent / "icon-source.png"
TOP = (0.28, 0.35, 0.78)      # placeholder gradient: indigo
BOTTOM = (0.58, 0.30, 0.76)   # ... to violet
GLYPH = "词"
CORNER_RATIO = 0.22           # rounded-tile corner radius, as in Adwaita
TILE_FILL = 0.80              # a rounded tile occupies 80% of the canvas
ART_FILL = 0.94               # shaped artwork gets almost the whole canvas
EDGE_PAD = 0.06               # breathing room inside the tile
CONTENT_THRESHOLD = 10


def rounded_rect(cr, x: float, y: float, width: float, height: float, radius: float) -> None:
    cr.new_sub_path()
    cr.arc(x + width - radius, y + radius, radius, -math.pi / 2, 0)
    cr.arc(x + width - radius, y + height - radius, radius, 0, math.pi / 2)
    cr.arc(x + radius, y + height - radius, radius, math.pi / 2, math.pi)
    cr.arc(x + radius, y + radius, radius, math.pi, 3 * math.pi / 2)
    cr.close_path()


def _pixels(surface: cairo.ImageSurface) -> np.ndarray:
    width, height = surface.get_width(), surface.get_height()
    stride = surface.get_stride()
    raw = np.frombuffer(surface.get_data(), np.uint8)
    usable = (height - 1) * stride + width * 4
    padded = np.zeros(stride * height, np.uint8)
    padded[: min(raw.size, usable)] = raw[:usable]
    return padded.reshape(height, stride)[:, : width * 4].reshape(height, width, 4)


def content_box(surface: cairo.ImageSurface) -> tuple[int, int, int, int] | None:
    """Bounding box of everything that is not the background colour."""
    pixels = _pixels(surface)
    corners = np.array([
        pixels[1, 1, :3], pixels[1, -2, :3], pixels[-2, 1, :3], pixels[-2, -2, :3],
    ])
    background = np.median(corners, axis=0)
    difference = np.abs(pixels[..., :3].astype(int) - background).max(axis=2)
    opaque = pixels[..., 3] > 8
    mask = (difference > CONTENT_THRESHOLD) & opaque
    if not mask.any():
        return None
    rows = np.where(mask.any(axis=1))[0]
    cols = np.where(mask.any(axis=0))[0]
    return int(cols.min()), int(rows.min()), int(cols.max()), int(rows.max())


def has_transparency(surface: cairo.ImageSurface) -> bool:
    return bool((_pixels(surface)[..., 3] < 250).any())


def on_canvas(surface: cairo.ImageSurface, size: int) -> cairo.ImageSurface:
    """Draw `surface` onto a transparent square of `size`."""
    out = cairo.ImageSurface(cairo.FORMAT_ARGB32, size, size)
    cr = cairo.Context(out)
    cr.set_source_surface(surface, (size - surface.get_width()) / 2,
                          (size - surface.get_height()) / 2)
    cr.paint()
    out.flush()
    return out


def prepare(source: cairo.ImageSurface) -> cairo.ImageSurface:
    """Normalise arbitrary artwork onto the icon canvas."""
    shaped = has_transparency(source)
    box = content_box(source)
    if box is None:
        return on_canvas(source, MASTER)

    left, top, right, bottom = box
    art_w, art_h = right - left + 1, bottom - top + 1
    fill = ART_FILL if shaped else TILE_FILL

    if shaped:
        # already a shaped icon: trim the empty margin and scale close to full size
        target = int(MASTER * fill)
        scale = target / max(art_w, art_h)
        scaled_w, scaled_h = int(art_w * scale), int(art_h * scale)
        cropped = cairo.ImageSurface(cairo.FORMAT_ARGB32, scaled_w, scaled_h)
        cr = cairo.Context(cropped)
        cr.scale(scale, scale)
        cr.set_source_surface(source, -left, -top)
        cr.get_source().set_filter(cairo.Filter.BEST)
        cr.paint()
        cropped.flush()
        return on_canvas(cropped, MASTER)

    # a picture on a solid background: rebuild it as a rounded tile
    pixels = _pixels(source)
    background = np.median(np.array([
        pixels[1, 1, :3], pixels[1, -2, :3], pixels[-2, 1, :3], pixels[-2, -2, :3],
    ]), axis=0) / 255.0

    side = int(max(art_w, art_h) * (1 + 2 * EDGE_PAD))
    tile = cairo.ImageSurface(cairo.FORMAT_ARGB32, side, side)
    cr = cairo.Context(tile)
    cr.set_source_rgb(*background)
    cr.paint()
    cr.set_source_surface(source, (side - art_w) / 2 - left, (side - art_h) / 2 - top)
    cr.get_source().set_filter(cairo.Filter.BEST)
    cr.paint()
    tile.flush()

    rounded = cairo.ImageSurface(cairo.FORMAT_ARGB32, side, side)
    cr = cairo.Context(rounded)
    rounded_rect(cr, 0, 0, side, side, side * CORNER_RATIO)
    cr.clip()
    cr.set_source_surface(tile, 0, 0)
    cr.paint()
    rounded.flush()

    target = int(MASTER * TILE_FILL)
    scaled = cairo.ImageSurface(cairo.FORMAT_ARGB32, target, target)
    cr = cairo.Context(scaled)
    cr.scale(target / side, target / side)
    cr.set_source_surface(rounded, 0, 0)
    cr.get_source().set_filter(cairo.Filter.BEST)
    cr.paint()
    scaled.flush()
    return on_canvas(scaled, MASTER)


def draw_placeholder() -> cairo.ImageSurface:
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, MASTER, MASTER)
    cr = cairo.Context(surface)
    margin = 56.0
    side = MASTER - 2 * margin
    gradient = cairo.LinearGradient(0, margin, 0, margin + side)
    gradient.add_color_stop_rgb(0.0, *TOP)
    gradient.add_color_stop_rgb(1.0, *BOTTOM)
    cr.set_source(gradient)
    rounded_rect(cr, margin, margin, side, side, side * CORNER_RATIO)
    cr.fill()

    layout = PangoCairo.create_layout(cr)
    description = Pango.FontDescription()
    description.set_family("Noto Sans CJK SC")
    description.set_weight(Pango.Weight.ULTRAHEAVY)
    description.set_absolute_size(560 * Pango.SCALE)
    layout.set_font_description(description)
    layout.set_text(GLYPH, -1)
    PangoCairo.context_set_font_options(layout.get_context(), cr.get_font_options())
    width, height = layout.get_pixel_size()
    cr.set_source_rgba(1, 1, 1, 1)
    cr.move_to((MASTER - width) / 2, (MASTER - height) / 2)
    PangoCairo.show_layout(cr, layout)
    surface.flush()
    return surface


def main() -> int:
    if SOURCE.is_file():
        source = cairo.ImageSurface.create_from_png(str(SOURCE))
        print(f"artwork {SOURCE.name}: {source.get_width()}x{source.get_height()}, "
              f"{'shaped (has transparency)' if has_transparency(source) else 'opaque background'}")
        box = content_box(source)
        if box:
            print(f"  content trimmed to x {box[0]}..{box[2]}, y {box[1]}..{box[3]}")
        master = prepare(source)
    else:
        print("no icon-source.png — drawing the placeholder")
        master = draw_placeholder()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for size in SIZES:
        surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, size, size)
        cr = cairo.Context(surface)
        cr.scale(size / MASTER, size / MASTER)
        cr.set_source_surface(master, 0, 0)
        cr.get_source().set_filter(cairo.Filter.BEST)
        cr.paint()
        surface.write_to_png(str(OUT_DIR / f"planalyrics-{size}.png"))
    print(f"wrote {len(SIZES)} sizes to {OUT_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
