"""The pictures laid over the video, drawn with PyMuPDF: event / speaker labels and the insets' decor.

Labels are transparent PNGs with a bundled font (Noto Sans, covers Greek); the editor shows the same
PNGs, so the preview matches the export. The decor is a rounded-corner mask and a soft shadow for the
smaller picture (made once per size, cached in .work/decor). Labels are drawn in 1080p units; for a 4K
export the same drawing is rendered at twice the resolution.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

import pymupdf

from . import cache, ffmpeg
from .layouts import (
    CANVAS,
    CORNER_PIP,
    HD,
    INSET_RADIUS,
    SHADOW_OPACITY,
    SIDE_CONTENT,
    SIDE_PIP,
    Box,
    Fade,
    Geometry,
)
from .timeline import Info, Segment

FONTS = Path(__file__).parent / "fonts"
REGULAR = FONTS / "NotoSans-Regular.ttf"
BOLD = FONTS / "NotoSans-SemiBold.ttf"
WHITE = (1, 1, 1)
MUTED = (0.78, 0.78, 0.82)
ACCENT = (0.31, 0.63, 1.0)

# Lower third timing within a presenter segment (seconds from the segment's start).
LT_IN, LT_FADE, LT_HOLD = 0.5, 0.4, 7.0
LT_TIMING = Fade(LT_IN, LT_FADE, LT_HOLD)


@dataclass(frozen=True)
class Overlay:
    name: str  # strip_corner, strip_side, panel, badge, lower_third
    path: Path
    box: Box  # where it goes on the canvas (1920×1080, or 3840×2160 for 4K)


_fonts: dict[Path, pymupdf.Font] = {}


def _font(path: Path) -> pymupdf.Font:
    if path not in _fonts:
        _fonts[path] = pymupdf.Font(fontfile=str(path))
    return _fonts[path]


def _fit(text: str, path: Path, size: float, width: float) -> str:
    """Shorten with an ellipsis so the text fits the width."""
    font = _font(path)
    if font.text_length(text, size) <= width:
        return text
    while text and font.text_length(text + "…", size) > width:
        text = text[:-1]
    return text.rstrip() + "…"


def _wrap(text: str, path: Path, size: float, width: float, max_lines: int) -> list[str]:
    font, lines, line = _font(path), [], ""
    for word in text.split():
        trial = f"{line} {word}".strip()
        if font.text_length(trial, size) <= width or not line:
            line = trial
        else:
            lines.append(line)
            line = word
    if line:
        lines.append(line)
    if len(lines) > max_lines:
        lines = lines[: max_lines - 1] + [_fit(" ".join(lines[max_lines - 1 :]), path, size, width)]
    return lines


class _Canvas:
    """A drawing surface of w×h pixels (times `scale` when saved): transparent, or opaque on a background colour."""

    def __init__(self, w: int, h: int, background: tuple[float, float, float] | None = None, scale: int = 1):
        self.doc = pymupdf.open()
        self.page = self.doc.new_page(width=w, height=h)
        self.scale = scale
        self.opaque = background is not None
        self.fonts = False
        if background is not None:
            self.box(self.page.rect, 1.0, color=background)

    def box(self, rect, opacity: float, radius: float = 0.0, color=(0.07, 0.07, 0.09)):
        r = pymupdf.Rect(rect)
        rad = radius / min(r.width, r.height) if radius else None
        self.page.draw_rect(r, color=None, fill=color, fill_opacity=opacity, radius=rad)

    def text(self, x: float, baseline: float, text: str, size: float, bold: bool = False, color=WHITE):
        if not self.fonts:
            self.page.insert_font(fontname="reg", fontfile=str(REGULAR))
            self.page.insert_font(fontname="bold", fontfile=str(BOLD))
            self.fonts = True
        self.page.insert_text((x, baseline), text, fontname="bold" if bold else "reg", fontsize=size, color=color)

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with cache.atomic_output(path) as tmp:
            self.page.get_pixmap(matrix=pymupdf.Matrix(self.scale, self.scale), alpha=not self.opaque).save(tmp)
        self.doc.close()


def inset_decor(w: int, h: int, work: Path, g: Geometry = HD) -> tuple[Path, Path]:
    """(mask, shadow) PNGs for a w×h inset on g's canvas: white rounded rectangle on black; blurred dark rectangle."""
    d = work / "decor"
    radius, sigma, pad = g.inset_radius, g.shadow_sigma, g.shadow_pad
    key = cache.key("decor", w, h, radius, sigma, SHADOW_OPACITY, pad)
    mask, shadow = d / f"mask-{key}.png", d / f"shadow-{key}.png"
    if not (mask.exists() and shadow.exists()):
        c = _Canvas(w, h, background=(0, 0, 0))
        c.box((0, 0, w, h), 1.0, radius=radius, color=(1, 1, 1))
        c.save(mask)
        with cache.atomic_output(shadow) as tmp:
            sharp = tmp.with_name(f"{tmp.stem}-sharp.png")
            c = _Canvas(w + 2 * pad, h + 2 * pad)
            c.box((pad, pad, pad + w, pad + h), SHADOW_OPACITY, radius=radius, color=(0, 0, 0))
            c.save(sharp)
            try:
                blur = f"format=rgba,gblur=sigma={sigma}"
                ffmpeg.run(["-i", str(sharp), "-vf", blur, "-frames:v", "1", str(tmp)], label="shadow", quiet=True)
            finally:
                sharp.unlink(missing_ok=True)
    return mask, shadow


def _strip(info: Info, pip: Box, path: Path, radius: int = 0, scale: int = 1) -> Box:
    w, h = pip.w, 34
    c = _Canvas(w, h, scale=scale)
    # With a rounded inset, round the strip's bottom corners to match (its top corners fall off the canvas).
    c.box((0, -radius, w, h), 0.68, radius=radius)
    c.text(12, 23, _fit(info.who_line(), REGULAR, 17, w - 24), 17)
    c.save(path)
    return Box(pip.x, pip.y + pip.h - h, w, h)


def _panel(info: Info, path: Path, scale: int = 1) -> Box:
    x, y = SIDE_PIP.x, SIDE_PIP.y + SIDE_PIP.h + 28
    w, max_h = SIDE_PIP.w, SIDE_CONTENT.y + SIDE_CONTENT.h - y
    lines: list[tuple[str, float, bool, tuple]] = []  # text, size, bold, colour
    lines += [(line, 30, True, WHITE) for line in _wrap(info.talk, BOLD, 30, w - 24, 3)]
    for text, size, color in ((info.speaker, 24, WHITE), (info.role, 20, MUTED), (info.event_line(), 20, MUTED)):
        if text:
            lines.append((_fit(text, REGULAR, size, w - 24), size, False, color))
    # Baselines: 40 px per title line, a gap after the title, then size + 14 per detail line.
    baselines, cursor = [], 34.0
    for i, (_, size, bold, _) in enumerate(lines):
        baselines.append(cursor)
        nxt = lines[i + 1] if i + 1 < len(lines) else None
        cursor += 40 if bold and nxt and nxt[2] else (50 if bold else size + 14)
    h = min(max_h, int(baselines[-1] + 14)) if lines else 1
    c = _Canvas(w, h, scale=scale)
    c.box((0, 0, 4, h), 1.0, color=ACCENT)
    for (text, size, bold, color), base in zip(lines, baselines):
        c.text(20, base, text, size, bold=bold, color=color)
    c.save(path)
    return Box(x, y, w, h)


def _badge(info: Info, path: Path, scale: int = 1) -> Box:
    text = info.event_line()
    size = 22
    w = int(_font(REGULAR).text_length(text, size)) + 32
    h = 42
    c = _Canvas(w, h, scale=scale)
    c.box((0, 0, w, h), 0.55, radius=10)
    c.text(16, 28, text, size)
    c.save(path)
    return Box(32, 28, w, h)


def _lower_third(info: Info, path: Path, scale: int = 1) -> Box:
    name = info.speaker or info.talk
    second = " · ".join(x for x in (info.role, info.talk if info.speaker else "") if x)
    third = info.event_line()
    w = 1100
    font_b, font_r = _font(BOLD), _font(REGULAR)
    w = math.ceil(
        min(
            w,
            58  # 28 px padding left and right, plus a little slack for rounding
            + max(
                font_b.text_length(name, 44),
                font_r.text_length(second, 26) if second else 0,
                font_r.text_length(third, 22) if third else 0,
            ),
        )
    )
    h = 70 + (40 if second else 0) + (34 if third else 0)
    c = _Canvas(w, h, scale=scale)
    c.box((0, 0, w, h), 0.78)
    c.box((0, 0, 8, h), 1.0, color=ACCENT)
    cursor = 56
    c.text(28, cursor, _fit(name, BOLD, 44, w - 56), 44, bold=True)
    if second:
        cursor += 40
        c.text(28, cursor, _fit(second, REGULAR, 26, w - 56), 26)
    if third:
        cursor += 34
        c.text(28, cursor, _fit(third, REGULAR, 22, w - 56), 22, color=MUTED)
    c.save(path)
    return Box(64, CANVAS[1] - 90 - h, w, h)


def render_overlays(info: Info, work: Path, g: Geometry = HD) -> dict[str, Overlay]:
    """Draw every label the info has content for, at g's resolution. Cached by content, so this is cheap
    to call often."""
    if info.empty:
        return {}
    out_dir = work / "labels"
    # (The 0 was the inset's border width; it stays so that the labels keep their cache names.)
    scaled = (g.scale,) if g.scale != 1 else ()
    key = cache.key("labels", info, CORNER_PIP, SIDE_PIP, 0, INSET_RADIUS, REGULAR.stat().st_size, *scaled)
    s = g.scale
    makers = []
    if info.who_line():
        makers.append(("strip_corner", lambda p: _strip(info, CORNER_PIP, p, INSET_RADIUS, s)))
        makers.append(("strip_side", lambda p: _strip(info, SIDE_PIP, p, scale=s)))
    if info.talk or info.speaker or info.event_line():
        makers.append(("panel", lambda p: _panel(info, p, s)))
    if info.event_line():
        makers.append(("badge", lambda p: _badge(info, p, s)))
    if info.speaker or info.talk:
        makers.append(("lower_third", lambda p: _lower_third(info, p, s)))
    overlays = {}
    for name, make in makers:
        path = out_dir / f"{name}-{key}.png"
        box = make(path)  # always redraw: boxes are computed while drawing, and it takes milliseconds
        overlays[name] = Overlay(name, path, g.box(box))
    return overlays


def speaker_shot(s: Segment) -> bool:
    """The presenter fills the frame: the shot gets the badge and can get the lower third."""
    return s.kind == "presenter" or s.style == "speaker"


def for_segment(s: Segment, overlays: dict[str, Overlay]) -> list[Overlay]:
    """The permanent labels a segment shows (the lower third is timed separately)."""
    if s.kind == "cut":
        names = []
    elif speaker_shot(s):
        names = ["badge"]
    else:
        names = {"": ["strip_corner"], "side": ["strip_side", "panel"]}.get(s.style, [])
    return [overlays[n] for n in names if n in overlays]


def lower_third_segments(segments: list[Segment], every_minutes: float) -> set[float]:
    """Start times of the presenter segments that get a lower third: the first one, then the next one
    at least `every_minutes` after the last showing (0: only the first)."""
    shown: set[float] = set()
    last = None
    for s in segments:
        if speaker_shot(s) and (last is None or (every_minutes > 0 and s.start - last >= every_minutes * 60)):
            shown.add(s.start)
            last = s.start
    return shown
