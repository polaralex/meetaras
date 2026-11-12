"""PDF slides → one letterboxed 1920×1080 PNG per page (3840×2160 for 4K exports); the text on the pages
(all PDF reading is here)."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pymupdf

from . import cache
from .layouts import BACKGROUND_RGB, CANVAS


def page_count(pdf: Path) -> int:
    with pymupdf.open(pdf) as doc:
        return doc.page_count


def page_texts(pdf: Path) -> list[str]:
    """Each page's text."""
    with pymupdf.open(pdf) as doc:
        return [page.get_text() for page in doc]


def page_titles(pdf: Path) -> list[str]:
    """Each page's title: its biggest text in the upper part of the page ("" if none)."""
    titles = []
    with pymupdf.open(pdf) as doc:
        for page in doc:
            lines = []
            for block in page.get_text("dict")["blocks"]:
                for line in block.get("lines", []):
                    text = " ".join(s["text"].strip() for s in line["spans"] if s["text"].strip())
                    if text and line["bbox"][1] < page.rect.height * 0.6:
                        lines.append((max(s["size"] for s in line["spans"]), line["bbox"][1], text))
            if not lines:
                titles.append("")
                continue
            size = max(s for s, _, _ in lines)
            # A title can wrap onto two lines of the same size; more is body text (a page in one size).
            top = [t for s, _, t in sorted(lines, key=lambda x: x[1]) if s >= size - 0.5]
            title = " ".join(top if len(top) <= 2 else top[:1])
            title = " ".join(title.split())[:80]
            titles.append(title if any(c.isalnum() for c in title) else "")  # emoji or symbols only: no title
    return titles


def page_stats(pdf: Path) -> list[tuple[int, float]]:
    """Each page's (words, share of the page covered by pictures); a full-page picture is a background."""
    stats = []
    with pymupdf.open(pdf) as doc:
        for page in doc:
            area = page.rect.width * page.rect.height
            boxes = [abs(pymupdf.Rect(info["bbox"]) & page.rect) for info in page.get_image_info()]
            covered = sum(b for b in boxes if b < 0.85 * area)
            stats.append((len(page.get_text("words")), covered / area if area > 0 else 0.0))
    return stats


def render_page(page: pymupdf.Page, out: Path, size: tuple[int, int] = CANVAS) -> None:
    w, h = size
    rect = page.rect
    zoom = min(w / rect.width, h / rect.height)
    pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False, colorspace=pymupdf.csRGB)

    # Filled with numpy: Pixmap.set_rect takes seconds per page.
    canvas = pymupdf.Pixmap(pymupdf.csRGB, w, h, np.full((h, w, 3), BACKGROUND_RGB, np.uint8).tobytes(), False)
    pix.set_origin((w - pix.width) // 2, (h - pix.height) // 2)
    canvas.copy(pix, pix.irect)
    canvas.save(out)


def slides_dir(pdf: Path, work_dir: Path, size: tuple[int, int] = CANVAS) -> Path:
    """Where the PNGs of the PDF's pages at `size` are cached."""
    return work_dir / "slides" / cache.key("slides", cache.file_fingerprint(pdf), size, BACKGROUND_RGB)


def render_slides(pdf: Path, work_dir: Path, size: tuple[int, int] = CANVAS) -> list[Path]:
    """Render every page at `size`; reuses the PNGs if the PDF is unchanged. Returns paths by page (index 0 = slide 1)."""
    out_dir = slides_dir(pdf, work_dir, size)
    with pymupdf.open(pdf) as doc:
        paths = [out_dir / f"slide-{i + 1:03d}.png" for i in range(doc.page_count)]
        if all(p.exists() for p in paths):
            return paths
        out_dir.mkdir(parents=True, exist_ok=True)
        for page, path in zip(doc, paths):
            with cache.atomic_output(path) as tmp:
                render_page(page, tmp, size)
    return paths
