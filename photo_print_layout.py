#!/usr/bin/env python3
"""
Photo Print Layout Generator
Creates print-ready PDFs for 5x7 paper mimicking Instax/Polaroid formats.

The white border on every cut piece comes from the physical card's own frame
dimensions — not from a generic margin. Photos are placed exactly to spec.

Cut lines run only through the whitespace gap between photos, not across
the photos themselves.

Usage:
    python photo_print_layout.py --input ./photos --layout instax_mini
    python photo_print_layout.py --input ./photos --layout polaroid_go
    python photo_print_layout.py --input ./photos --layout instax_wide
    python photo_print_layout.py --input ./photos --layout instax_square
    python photo_print_layout.py --input ./photos --layout polaroid_itype
    python photo_print_layout.py --input ./photos --layout all
"""

import argparse
import os
import sys
from pathlib import Path
from PIL import Image
from reportlab.pdfgen import canvas
from reportlab.lib.units import mm, inch
import tempfile

# ─── FORMAT DEFINITIONS ───────────────────────────────────────────────────────
#
# All dimensions in mm, taken directly from the physical card spec.
# frame_lr = white border on left and right of image area
# frame_tb = white border on top and bottom of image area
# cols/rows = how many fit on 5x7 paper with these exact frame widths
# rotate    = rotate source image 90° before placing (for landscape cards)
#
# Layout rule:
#   outer margin = frame_lr (sides) / frame_tb (top+bottom)
#   gap between  = 2×frame  (one frame from each neighbouring card)
#   cut line     = midpoint of that gap — runs only through whitespace
#   → every cut piece has identical frame on all 4 sides, matching the spec

FORMATS = {
    "instax_mini": {
        "label":    "INSTAX mini",
        "img_w":    46.0,   # mm — image area
        "img_h":    62.0,
        "frame_lr": 4.0,    # mm — left/right white border
        "frame_tb": 12.0,   # mm — top/bottom white border
        "cols":     2,
        "rows":     2,
        "rotate":   False,
    },
    "polaroid_go": {
        "label":    "Polaroid Go",
        "img_w":    46.0,
        "img_h":    47.0,
        "frame_lr": 3.95,
        "frame_tb": 9.8,
        "cols":     2,
        "rows":     2,
        "rotate":   False,
    },
    "instax_wide": {
        "label":    "INSTAX WIDE",
        "img_w":    99.0,
        "img_h":    62.0,
        "frame_lr": 4.5,
        "frame_tb": 12.0,
        "cols":     1,
        "rows":     2,
        "rotate":   False,
    },
    "instax_square": {
        "label":    "INSTAX SQUARE",
        "img_w":    62.0,
        "img_h":    62.0,
        "frame_lr": 5.0,
        "frame_tb": 12.0,
        "cols":     1,
        "rows":     2,
        "rotate":   False,
    },
    "polaroid_itype": {
        "label":    "Polaroid i-Type / 600 / SX-70",
        # Physical card is 88×107mm portrait. Rotated 90° to fit 2 per sheet.
        # After rotation: 107mm wide × 88mm tall.
        # Image area 77×79mm → rotated: 79mm wide × 77mm tall.
        # Original sides (5.5mm) become top/bottom after rotation.
        # Original top/bottom (14mm) become left/right after rotation.
        "img_w":    79.0,
        "img_h":    77.0,
        "frame_lr": 14.0,
        "frame_tb": 5.5,
        "cols":     1,
        "rows":     2,
        "rotate":   True,
    },
}

# ─── CONSTANTS ────────────────────────────────────────────────────────────────

PAGE_W     = 5 * inch
PAGE_H     = 7 * inch
OUTPUT_DPI = 300
IMAGE_EXTS = {'.jpg', '.jpeg', '.png', '.tif', '.tiff', '.webp'}
CUT_COLOR  = (0.55, 0.55, 0.55)

# ─── HELPERS ──────────────────────────────────────────────────────────────────

def get_images(folder: Path) -> list[Path]:
    imgs = [p for p in sorted(folder.iterdir()) if p.suffix.lower() in IMAGE_EXTS]
    if not imgs:
        print(f"⚠️  No images found in {folder}")
        sys.exit(1)
    return imgs


def fit_image(img: Image.Image, target_w: int, target_h: int) -> Image.Image:
    """Cover-crop: resize to fill target exactly, centred."""
    src_w, src_h = img.size
    if src_w / src_h > target_w / target_h:
        new_h, new_w = target_h, int(src_w / src_h * target_h)
    else:
        new_w, new_h = target_w, int(target_w / (src_w / src_h))
    img = img.resize((new_w, new_h), Image.LANCZOS)
    left = (new_w - target_w) // 2
    top  = (new_h - target_h) // 2
    return img.crop((left, top, left + target_w, top + target_h))


def embed_image(c: canvas.Canvas, img_path: Path,
                x: float, y: float, w: float, h: float,
                rotate: bool = False):
    px_w = int(w / inch * OUTPUT_DPI)
    px_h = int(h / inch * OUTPUT_DPI)
    with Image.open(img_path) as img:
        img = img.convert("RGB")
        if rotate:
            img = img.rotate(90, expand=True)
        fitted = fit_image(img, px_w, px_h)
    with tempfile.NamedTemporaryFile(suffix='.jpg', delete=False) as tmp:
        fitted.save(tmp.name, 'JPEG', quality=95, dpi=(OUTPUT_DPI, OUTPUT_DPI))
        tmp_path = tmp.name
    try:
        c.drawImage(tmp_path, x, y, width=w, height=h, preserveAspectRatio=False)
    finally:
        os.unlink(tmp_path)


def draw_cut_segment(c: canvas.Canvas,
                     x1: float, y1: float,
                     x2: float, y2: float):
    """
    Draw one segment of a dotted cut line.
    Must only be called with coordinates that lie entirely within the
    white border gap — never over a photo.
    """
    c.saveState()
    c.setStrokeColorRGB(*CUT_COLOR)
    c.setLineWidth(0.8)
    c.setLineCap(1)         # round cap → dots
    c.setDash([0.1, 5], 0)  # near-zero dash length + gap = round dots
    c.line(x1, y1, x2, y2)
    c.restoreState()


# ─── LAYOUT ENGINE ────────────────────────────────────────────────────────────

def build_layout(images: list[Path], fmt: dict, output_path: Path):
    """
    Place images on 5×7 pages according to the format spec.

    Geometry (example: 2 cols, 2 rows, frame_lr=F, frame_tb=T):

      ←F→←── img_w ──→←F→←F→←── img_w ──→←F→
      ↑  ┌───────────┐   ┌───────────┐
      T  │  photo    │   │  photo    │
      ↓  └───────────┘   └───────────┘
      ↑  (cut line spans only the gap ← 2F wide → and the outer margins)
      T  ┌───────────┐   ┌───────────┐
      ↓  │  photo    │   │  photo    │
         └───────────┘   └───────────┘

    Vertical cut line x  = col_x[c] + img_w + frame_lr  (midpoint of 2F gap)
    Horizontal cut line y = row_y[r] + img_h + frame_tb  (midpoint of 2T gap)

    Cut line segments run:
      • Vertical lines   → through top/bottom margins + between-row gaps only
      • Horizontal lines → through left/right margins + between-column gaps only
    """
    cols   = fmt["cols"]
    rows   = fmt["rows"]
    img_w  = fmt["img_w"]  * mm
    img_h  = fmt["img_h"]  * mm
    f_lr   = fmt["frame_lr"] * mm
    f_tb   = fmt["frame_tb"] * mm
    rotate = fmt["rotate"]

    # Total block size
    block_w = 2 * f_lr + cols * img_w + (cols - 1) * 2 * f_lr
    block_h = 2 * f_tb + rows * img_h + (rows - 1) * 2 * f_tb

    # Centre block on page
    ox = (PAGE_W - block_w) / 2   # x offset
    oy = (PAGE_H - block_h) / 2   # y offset

    # Bottom-left corner of each image cell
    col_x = [ox + f_lr + c * (img_w + 2 * f_lr) for c in range(cols)]
    row_y = [oy + f_tb + r * (img_h + 2 * f_tb) for r in range(rows)]

    # Cut line positions: midpoint of each inter-card gap
    # v_cuts[c] = x midpoint between column c and column c+1
    v_cuts = [col_x[c] + img_w + f_lr        for c in range(cols - 1)]
    # h_cuts[r] = y midpoint between row r and row r+1
    h_cuts = [row_y[r] + img_h + f_tb        for r in range(rows - 1)]

    per_page = cols * rows
    pages    = (len(images) + per_page - 1) // per_page

    c = canvas.Canvas(str(output_path), pagesize=(PAGE_W, PAGE_H))
    c.setTitle(f"{fmt['label']} Print Layout")

    for page_idx in range(pages):
        chunk = images[page_idx * per_page: (page_idx + 1) * per_page]

        # ── Photos (reading order: left→right, top→bottom) ──────────────────
        for i, img_path in enumerate(chunk):
            col = i % cols
            row = rows - 1 - (i // cols)   # row 0 = bottom in PDF coords
            embed_image(c, img_path,
                        col_x[col], row_y[row], img_w, img_h, rotate)

        # Outer block edges (for reference below)
        left_edge   = ox
        right_edge  = ox + block_w
        bottom_edge = oy
        top_edge    = oy + block_h

        # ── Internal vertical cut lines ──────────────────────────────────────
        # Each runs top-to-bottom but SKIPS across photos — only appears in
        # the horizontal white bands: bottom margin, between-row gaps, top margin.
        for cx in v_cuts:
            draw_cut_segment(c, cx, 0,                   cx, row_y[0])
            for r in range(rows - 1):
                draw_cut_segment(c, cx, row_y[r] + img_h, cx, row_y[r + 1])
            draw_cut_segment(c, cx, row_y[-1] + img_h,  cx, PAGE_H)

        # ── Internal horizontal cut lines ────────────────────────────────────
        # Each runs left-to-right but SKIPS across photos — only appears in
        # the vertical white bands: left margin, between-column gaps, right margin.
        for hy in h_cuts:
            draw_cut_segment(c, 0,                    hy, col_x[0],          hy)
            for col in range(cols - 1):
                draw_cut_segment(c, col_x[col] + img_w, hy, col_x[col + 1], hy)
            draw_cut_segment(c, col_x[-1] + img_w,   hy, PAGE_W,             hy)

        # ── External cut lines ───────────────────────────────────────────────
        # The 4 outer edges of the whole block. These tell you where to cut
        # the surrounding waste paper off. Each line runs only in the outer
        # margin — from the page edge to the block edge — never over photos.
        #
        #   left_edge / right_edge : vertical lines in the top & bottom margins
        #   bottom_edge / top_edge : horizontal lines in the left & right margins

        # Left outer edge — vertical, runs in top and bottom outer margins
        draw_cut_segment(c, left_edge,  0,          left_edge,  bottom_edge)
        draw_cut_segment(c, left_edge,  top_edge,   left_edge,  PAGE_H)

        # Right outer edge — vertical, runs in top and bottom outer margins
        draw_cut_segment(c, right_edge, 0,          right_edge, bottom_edge)
        draw_cut_segment(c, right_edge, top_edge,   right_edge, PAGE_H)

        # Bottom outer edge — horizontal, runs in left and right outer margins
        draw_cut_segment(c, 0,          bottom_edge, left_edge,  bottom_edge)
        draw_cut_segment(c, right_edge, bottom_edge, PAGE_W,     bottom_edge)

        # Top outer edge — horizontal, runs in left and right outer margins
        draw_cut_segment(c, 0,          top_edge,    left_edge,  top_edge)
        draw_cut_segment(c, right_edge, top_edge,    PAGE_W,     top_edge)

        print(f"  page {page_idx + 1}/{pages} — {len(chunk)} photo(s)")
        c.showPage()

    c.save()
    print(f"✅ Saved: {output_path}")


# ─── MAIN ─────────────────────────────────────────────────────────────────────

def main():
    choices = list(FORMATS.keys()) + ["all"]
    parser = argparse.ArgumentParser(
        description="Print-ready 5×7 PDFs matching Instax / Polaroid frame specs."
    )
    parser.add_argument("--input",  "-i", required=True,
                        help="Folder of exported photos (JPG/PNG/TIF)")
    parser.add_argument("--layout", "-l", choices=choices, default="all",
                        help="Format to render (default: all)")
    parser.add_argument("--output", "-o", default=None,
                        help="Output folder (default: same as input)")
    args = parser.parse_args()

    input_folder = Path(args.input).expanduser().resolve()
    if not input_folder.is_dir():
        print(f"❌ Input folder not found: {input_folder}")
        sys.exit(1)
    output_folder = (Path(args.output).expanduser().resolve()
                     if args.output else input_folder)
    output_folder.mkdir(parents=True, exist_ok=True)

    images = get_images(input_folder)
    print(f"📸 Found {len(images)} images in {input_folder}\n")

    targets = list(FORMATS.keys()) if args.layout == "all" else [args.layout]

    for key in targets:
        fmt  = FORMATS[key]
        flr, ftb = fmt["frame_lr"], fmt["frame_tb"]
        cols, rows = fmt["cols"], fmt["rows"]
        iw, ih = fmt["img_w"], fmt["img_h"]

        block_w = 2*flr + cols*iw + (cols-1)*2*flr
        block_h = 2*ftb + rows*ih + (rows-1)*2*ftb

        print(f"🖨️  {fmt['label']}  ({cols}×{rows} = {cols*rows} per sheet)")
        print(f"   image area : {iw}×{ih} mm")
        print(f"   frame      : {flr} mm lr  /  {ftb} mm tb")
        print(f"   block      : {block_w:.1f}×{block_h:.1f} mm  (paper 127×177.8 mm)")

        out = output_folder / f"photos_{key}.pdf"
        build_layout(images, fmt, out)
        print()

    print("✅ Done! Print at 100% / actual size — no fit-to-page.")
    print("   Canon PRINT: 5×7 paper, Borderless OFF, Quality: High.")


if __name__ == "__main__":
    main()