"""
Build the icon set + DMG background for the JARVIS Mac installer.

This is a placeholder design — flat cyan reactor disc on a dark
background, matching the in-app HUD palette. Replace with a real brand
asset before charging money.

Run from this directory:

    python build_assets.py

Outputs:
  ./icon.png            (1024x1024 master)
  ./icon.iconset/       (multi-size sources)
  ./icon.icns           (Mac app icon)
  ./icon.ico            (Windows app icon)
  ./dmg-background.png  (540x380 installer backdrop)
"""

from __future__ import annotations

import math
import shutil
import subprocess
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageFilter

HERE = Path(__file__).parent.resolve()

CYAN       = (0, 212, 255, 255)
CYAN_DIM   = (0, 153, 187, 255)
DARK       = (0, 8, 22, 255)
DARK2      = (0, 4, 14, 255)
TEXT       = (200, 230, 246, 255)


def _radial(size: int) -> Image.Image:
    """Dark radial gradient backplate, brighter in the upper-left."""
    img = Image.new("RGBA", (size, size), DARK)
    pixels = img.load()
    cx, cy = size * 0.45, size * 0.40
    rmax = size * 0.85
    for y in range(size):
        for x in range(size):
            d = math.hypot(x - cx, y - cy) / rmax
            t = max(0.0, min(1.0, d))
            r = int(DARK[0] * (1 - t) + DARK2[0] * t)
            g = int(DARK[1] * (1 - t) + DARK2[1] * t)
            b = int(DARK[2] * (1 - t) + DARK2[2] * t)
            pixels[x, y] = (r, g, b, 255)
    return img


def make_icon (size: int = 1024) -> Image.Image:
    """A minimal 'arc reactor' style monogram — cyan rings on dark."""
    img = _radial(size)
    draw = ImageDraw.Draw(img)
    cx = cy = size // 2

    # Outer ring
    r_outer = int(size * 0.42)
    draw.ellipse(
        (cx - r_outer, cy - r_outer, cx + r_outer, cy + r_outer),
        outline=CYAN, width=max(4, size // 96),
    )
    # Inner ring
    r_inner = int(size * 0.28)
    draw.ellipse(
        (cx - r_inner, cy - r_inner, cx + r_inner, cy + r_inner),
        outline=CYAN_DIM, width=max(3, size // 128),
    )
    # Core dot
    r_core = int(size * 0.10)
    draw.ellipse(
        (cx - r_core, cy - r_core, cx + r_core, cy + r_core),
        fill=CYAN,
    )

    # 8 segment ticks between the two rings
    for i in range(8):
        a = i * (math.pi / 4)
        x1 = cx + int((r_inner + size * 0.02) * math.cos(a))
        y1 = cy + int((r_inner + size * 0.02) * math.sin(a))
        x2 = cx + int((r_outer - size * 0.02) * math.cos(a))
        y2 = cy + int((r_outer - size * 0.02) * math.sin(a))
        draw.line((x1, y1, x2, y2), fill=CYAN_DIM, width=max(2, size // 256))

    # Soft outer glow
    glow = Image.new("RGBA", img.size, (0, 0, 0, 0))
    gdraw = ImageDraw.Draw(glow)
    gdraw.ellipse(
        (cx - r_outer - 4, cy - r_outer - 4, cx + r_outer + 4, cy + r_outer + 4),
        outline=(0, 212, 255, 90),
        width=max(8, size // 64),
    )
    glow = glow.filter(ImageFilter.GaussianBlur(size // 80))
    img = Image.alpha_composite(img, glow)

    return img


def make_dmg_bg () -> Image.Image:
    """540×380 dark backdrop with the JARVIS wordmark — matches the HUD."""
    w, h = 540, 380
    img = Image.new("RGBA", (w, h), DARK)
    px = img.load()
    for y in range(h):
        # Vertical fade
        t = y / h
        r = int(DARK2[0] * (1 - t) + DARK[0] * t)
        g = int(DARK2[1] * (1 - t) + DARK[1] * t)
        b = int(DARK2[2] * (1 - t) + DARK[2] * t)
        for x in range(w):
            px[x, y] = (r, g, b, 255)

    draw = ImageDraw.Draw(img)
    # Hairline grid
    grid = (0, 212, 255, 18)
    for x in range(0, w, 30):
        draw.line((x, 0, x, h), fill=grid)
    for y in range(0, h, 30):
        draw.line((0, y, w, y), fill=grid)

    # Wordmark
    try:
        font_big   = ImageFont.truetype("/System/Library/Fonts/Supplemental/Futura.ttc", 32)
        font_small = ImageFont.truetype("/System/Library/Fonts/Supplemental/Menlo.ttc", 11)
    except Exception:
        font_big   = ImageFont.load_default()
        font_small = ImageFont.load_default()

    draw.text((40, 56), "JARVIS",            fill=CYAN,     font=font_big)
    draw.text((40, 100), "JUST A RATHER VERY INTELLIGENT SYSTEM",
              fill=(140, 200, 230, 200),                   font=font_small)
    draw.text((40, h - 60), "DRAG  ⟶  APPLICATIONS",
              fill=(180, 220, 240, 220),                   font=font_small)

    return img


# ── conversion helpers ────────────────────────────────────────────────────────

ICONSET_SIZES = [16, 32, 64, 128, 256, 512, 1024]


def write_iconset (master: Image.Image, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    for s in ICONSET_SIZES:
        master.resize((s, s), Image.LANCZOS).save(out_dir / f"icon_{s}x{s}.png")
        if s <= 512:
            # @2x version for retina
            master.resize((s * 2, s * 2), Image.LANCZOS).save(out_dir / f"icon_{s}x{s}@2x.png")


def write_icns (iconset_dir: Path, out_path: Path) -> None:
    subprocess.run(
        ["iconutil", "-c", "icns", str(iconset_dir), "-o", str(out_path)],
        check=True,
    )


def write_ico (master: Image.Image, out_path: Path) -> None:
    sizes = [(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
    master.save(out_path, format="ICO", sizes=sizes)


def main () -> None:
    icon = make_icon(1024)
    icon.save(HERE / "icon.png")

    iconset_dir = HERE / "icon.iconset"
    write_iconset(icon, iconset_dir)
    write_icns(iconset_dir, HERE / "icon.icns")

    write_ico(icon, HERE / "icon.ico")

    bg = make_dmg_bg()
    bg.save(HERE / "dmg-background.png")
    # @2x retina version, in case dmg.backgroundColor isn't enough
    bg.resize((1080, 760), Image.LANCZOS).save(HERE / "dmg-background@2x.png")

    # Clean up the intermediate iconset
    shutil.rmtree(iconset_dir, ignore_errors=True)

    print("Wrote:")
    for name in ("icon.png", "icon.icns", "icon.ico", "dmg-background.png", "dmg-background@2x.png"):
        p = HERE / name
        if p.exists():
            print(f"  {p.relative_to(HERE.parent)}  ({p.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
