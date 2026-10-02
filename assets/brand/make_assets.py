"""Regenerate Tempo's derived brand images from the source artwork in assets/.

    python3 assets/brand/make_assets.py

Needs Pillow (`python3 -m pip install pillow`) and macOS's `iconutil`. Pillow is a tool for
this script only, not an app dependency, so it is not in requirements.txt.

Sources (generated artwork, kept as delivered):
  assets/app_icon.png       the spiral mark (its squircle and shadow are redrawn, see below)
  assets/banner.jpeg        README banner
  assets/link_preview.jpeg  GitHub social preview

Outputs:
  assets/brand/icon.png                  1024×1024 master icon
  desktop/icons/icon.icns, 32x32.png, 128x128.png, 128x128@2x.png
  assets/brand/banner.jpg                1600×400
  assets/brand/social-preview.jpg        1280×640, with the real icon in it

The delivered icon's drop shadow came out as a ragged gray halo, so only the spiral is lifted
from it and set on a clean squircle drawn to Apple's grid: an 824px rounded square centered on a
1024px canvas. The social preview's generator drew a different spiral; the real icon is pasted
over it so the two always match.
"""

from __future__ import annotations

import math
import shutil
import subprocess
import tempfile
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter

ROOT = Path(__file__).resolve().parents[2]
ASSETS = ROOT / "assets"
BRAND = ASSETS / "brand"
ICONS = ROOT / "desktop" / "icons"

INK = (17, 17, 17)  # #111111, the brand's black
CANVAS = 1024
FACE = 824  # Apple's macOS icon grid: the rounded square inside the canvas
SUPERSAMPLE = 4

# Where the delivered artwork's pieces are, measured once from the source files.
SPIRAL_BOX = (296, 334, 956, 958)  # assets/app_icon.png, the spiral with a 10px margin
PREVIEW_ICON_CENTER = (728, 220)  # assets/link_preview.jpeg, the drawn icon's center
PREVIEW_ICON_FACE = 258  # covers its 238px face and the halo around it


def squircle_mask(size: int, n: float = 5.0) -> Image.Image:
    """An anti-aliased superellipse, close to Apple's continuous-corner rounded square."""
    big = size * SUPERSAMPLE
    half = big / 2
    points = []
    steps = 720
    for i in range(steps):
        t = 2 * math.pi * i / steps
        c, s = math.cos(t), math.sin(t)
        x = half + half * (abs(c) ** (2 / n)) * (1 if c >= 0 else -1)
        y = half + half * (abs(s) ** (2 / n)) * (1 if s >= 0 else -1)
        points.append((x, y))
    mask = Image.new("L", (big, big), 0)
    ImageDraw.Draw(mask).polygon(points, fill=255)
    return mask.resize((size, size), Image.LANCZOS)


def spiral_alpha() -> Image.Image:
    """The spiral from the delivered icon as a soft alpha mask (dark ink → opaque)."""
    src = Image.open(ASSETS / "app_icon.png").convert("RGBA").crop(SPIRAL_BOX)
    lum = src.convert("L")
    # Ink is ~20, the face ~250; map that range onto 255..0, keeping the anti-aliased edge.
    alpha = lum.point(lambda v: max(0, min(255, round((235 - v) * 255 / (235 - 40)))))
    return ImageChops.multiply(alpha, src.getchannel("A")).crop(alpha.getbbox())


def master_icon() -> Image.Image:
    canvas = Image.new("RGBA", (CANVAS, CANVAS), (0, 0, 0, 0))
    offset = (CANVAS - FACE) // 2
    face = squircle_mask(FACE)

    # A soft shadow under the face, as on Apple's own icons: low, slightly offset, well inside
    # the 100px margin so nothing is clipped.
    shadow = Image.new("L", (CANVAS, CANVAS), 0)
    shadow.paste(face.point(lambda v: v * 0.28), (offset, offset + 10))
    shadow = shadow.filter(ImageFilter.GaussianBlur(14))
    canvas.paste(Image.new("RGBA", (CANVAS, CANVAS), (0, 0, 0, 255)), (0, 0), shadow)

    canvas.paste(Image.new("RGBA", (FACE, FACE), (255, 255, 255, 255)), (offset, offset), face)

    # The spiral at 66% of the face width, centered, with breathing room for small sizes.
    spiral = spiral_alpha()
    scale = FACE * 0.66 / spiral.width
    spiral = spiral.resize((round(spiral.width * scale), round(spiral.height * scale)), Image.LANCZOS)
    pos = ((CANVAS - spiral.width) // 2, (CANVAS - spiral.height) // 2)
    canvas.paste(Image.new("RGBA", spiral.size, INK + (255,)), pos, spiral)
    return canvas


def write_icons(icon: Image.Image) -> None:
    BRAND.mkdir(parents=True, exist_ok=True)
    icon.save(BRAND / "icon.png")

    with tempfile.TemporaryDirectory() as tmp:
        iconset = Path(tmp) / "icon.iconset"
        iconset.mkdir()
        for points in (16, 32, 128, 256, 512):
            for scale in (1, 2):
                px = points * scale
                suffix = "" if scale == 1 else "@2x"
                icon.resize((px, px), Image.LANCZOS).save(iconset / f"icon_{points}x{points}{suffix}.png")
        subprocess.run(
            ["iconutil", "-c", "icns", str(iconset), "-o", str(ICONS / "icon.icns")], check=True
        )
        shutil.copy(iconset / "icon_32x32.png", ICONS / "32x32.png")
        shutil.copy(iconset / "icon_128x128.png", ICONS / "128x128.png")
        shutil.copy(iconset / "icon_128x128@2x.png", ICONS / "128x128@2x.png")


def fit(image: Image.Image, size: tuple[int, int]) -> Image.Image:
    """Center-crop to the target aspect ratio, then resize."""
    tw, th = size
    w, h = image.size
    if w * th > h * tw:  # too wide
        new_w = round(h * tw / th)
        image = image.crop(((w - new_w) // 2, 0, (w - new_w) // 2 + new_w, h))
    else:
        new_h = round(w * th / tw)
        image = image.crop((0, (h - new_h) // 2, w, (h - new_h) // 2 + new_h))
    return image.resize(size, Image.LANCZOS)


def write_banner() -> None:
    banner = Image.open(ASSETS / "banner.jpeg").convert("RGB")
    fit(banner, (1600, 400)).save(BRAND / "banner.jpg", quality=90, optimize=True)


def write_social_preview(icon: Image.Image) -> None:
    preview = Image.open(ASSETS / "link_preview.jpeg").convert("RGB")
    size = round(CANVAS * PREVIEW_ICON_FACE / FACE)
    small = icon.resize((size, size), Image.LANCZOS)
    cx, cy = PREVIEW_ICON_CENTER
    preview.paste(small, (cx - size // 2, cy - size // 2), small)
    # GitHub's recommended 1280×640; the source is 1456×720 (2.02:1), so a few px come off.
    fit(preview, (1280, 640)).save(BRAND / "social-preview.jpg", quality=90, optimize=True)


def main() -> None:
    icon = master_icon()
    write_icons(icon)
    write_banner()
    write_social_preview(icon)
    for path in (BRAND / "icon.png", BRAND / "banner.jpg", BRAND / "social-preview.jpg", ICONS / "icon.icns"):
        print(f"wrote {path.relative_to(ROOT)} ({path.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()
