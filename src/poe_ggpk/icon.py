"""Application icon, drawn with Pillow (no external image files)."""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

ICO_SIZES = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]


def icon_image(size: int = 256) -> Image.Image:
    """A faceted gold gem on a dark rounded square."""
    scale = 4  # supersampling for smooth edges
    s = size * scale
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((0, 0, s - 1, s - 1), radius=s // 5, fill=(28, 30, 38, 255))

    cx, top, mid, bottom = s / 2, s * 0.18, s * 0.40, s * 0.84
    left, right = s * 0.16, s * 0.84
    inner_l, inner_r = s * 0.36, s * 0.64
    crown = [(left, mid), (s * 0.30, top), (s * 0.70, top), (right, mid)]
    facets = [
        ([(s * 0.30, top), (inner_l, mid), (left, mid)], (232, 180, 70)),
        ([(s * 0.30, top), (s * 0.70, top), (inner_r, mid), (inner_l, mid)], (250, 214, 120)),
        ([(s * 0.70, top), (right, mid), (inner_r, mid)], (214, 158, 52)),
        ([(left, mid), (inner_l, mid), (cx, bottom)], (196, 138, 40)),
        ([(inner_l, mid), (inner_r, mid), (cx, bottom)], (238, 190, 84)),
        ([(inner_r, mid), (right, mid), (cx, bottom)], (170, 116, 30)),
    ]
    for poly, color in facets:
        d.polygon(poly, fill=color)
    d.line(crown + [(cx, bottom), (left, mid)], fill=(110, 72, 18), width=max(1, s // 64), joint="curve")
    return img.resize((size, size), Image.LANCZOS)


def write_ico(path: str | Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    icon_image(256).save(path, format="ICO", sizes=ICO_SIZES)
    return path
