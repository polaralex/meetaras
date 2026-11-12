"""Layout constants and ffmpeg filter builders.

All positions are written in 1080p units (the 1920×1080 canvas); a 4K export uses the same geometry
scaled by 2, computed here only (`Geometry`). Preview renders scale the finished frame.
"""

from __future__ import annotations

from dataclasses import dataclass

CANVAS = (1920, 1080)
FPS = 30
BACKGROUND = "0x1b1b1f"  # canvas behind the slides layout, bars around screen recordings
BACKGROUND_RGB = tuple(bytes.fromhex(BACKGROUND[2:]))  # the same, for slide PNGs' letterbox: (27, 27, 31)
# Frame-level BT.709 tags; ffmpeg 7 takes output color tags from the frames, not only from -color_* options.
BT709 = "setparams=color_primaries=bt709:color_trc=bt709:colorspace=bt709:range=tv"


@dataclass(frozen=True)
class Box:
    x: int
    y: int
    w: int
    h: int

    def scaled(self, k: int) -> Box:
        return Box(self.x * k, self.y * k, self.w * k, self.h * k)


# Default style: content almost full frame, the whole presenter video (16:9, no crop) over its
# bottom-right corner.
CORNER_CONTENT = Box(48, 27, 1824, 1026)
CORNER_PIP = Box(1368, 759, 480, 270)

# `side` style: content and presenter side by side, nothing covered.
SIDE_CONTENT = Box(24, 180, 1280, 720)
SIDE_PIP = Box(1320, 180, 576, 324)

# `speaker` style: the reverse of the default — the presenter full frame, the slide/screen small in the
# bottom-right corner.
SPEAKER_CONTENT = Box(1288, 707, 592, 333)
FULL = Box(0, 0, *CANVAS)

# The smaller picture on top ("inset") gets rounded corners and a soft drop shadow.
INSET_RADIUS = 14
SHADOW_SIGMA = 12
SHADOW_OFFSET = 6  # px down
SHADOW_OPACITY = 0.55
SHADOW_PAD = 3 * SHADOW_SIGMA  # room around the inset for the blurred shadow

# style → (content box, presenter box, which of the two is the inset: "pip", "content" or None)
STYLES = {
    "": (CORNER_CONTENT, CORNER_PIP, "pip"),
    "side": (SIDE_CONTENT, SIDE_PIP, None),
    "speaker": (SPEAKER_CONTENT, FULL, "content"),
}


@dataclass(frozen=True)
class Geometry:
    """Every size on one export canvas: the 1080p values above times `scale` (2: 4K)."""

    scale: int = 1

    @property
    def canvas(self) -> tuple[int, int]:
        return CANVAS[0] * self.scale, CANVAS[1] * self.scale

    def box(self, b: Box) -> Box:
        return b.scaled(self.scale)

    @property
    def styles(self) -> dict[str, tuple[Box, Box, str | None]]:
        return {name: (c.scaled(self.scale), p.scaled(self.scale), inset) for name, (c, p, inset) in STYLES.items()}

    @property
    def inset_radius(self) -> int:
        return INSET_RADIUS * self.scale

    @property
    def shadow_sigma(self) -> int:
        return SHADOW_SIGMA * self.scale

    @property
    def shadow_offset(self) -> int:
        return SHADOW_OFFSET * self.scale

    @property
    def shadow_pad(self) -> int:
        return SHADOW_PAD * self.scale

    @property
    def decor_id(self) -> tuple:
        """What the inset decor looks like, for the keys of the clips that use it."""
        return (self.inset_radius, self.shadow_sigma, self.shadow_offset, SHADOW_OPACITY)


HD = Geometry(1)  # 1920×1080: the editor, the draft, previews and 1080p exports
UHD = Geometry(2)  # 3840×2160: 4K exports
RESOLUTIONS = {"1080p": HD, "4k": UHD}  # the timeline's `resolution` → its export geometry


def _inset(
    base: str, src: str, box: Box, decor: tuple[int, int] | None, out: str, shortest: int, g: Geometry = HD
) -> str:
    """Overlay [src] (already box-sized) on [base] at box; with decor=(mask input, shadow input) it gets
    rounded corners and a drop shadow."""
    if decor is None:
        return f"[{base}][{src}]overlay={box.x}:{box.y}:shortest={shortest}[{out}]"
    mask, shadow = decor
    pad = g.shadow_pad
    return (
        f"[{mask}:v]format=gray[{out}_m];[{src}]format=yuva420p[{out}_a];[{out}_a][{out}_m]alphamerge[{out}_r];"
        f"[{base}][{shadow}:v]overlay={box.x - pad}:{box.y - pad + g.shadow_offset}:shortest=0[{out}_s];"
        f"[{out}_s][{out}_r]overlay={box.x}:{box.y}:shortest={shortest}[{out}]"
    )


def inset_filter(
    style: str = "", decor: tuple[int, int] | None = None, content: str = "1:v", out: str = "v", g: Geometry = HD
) -> str:
    """Inputs: [0:v] presenter, [content] at canvas size (slide PNG or screen video). Output: [out]."""
    w, h = g.canvas
    s, p, _ = g.styles[style]
    return (
        f"color=c={BACKGROUND}:s={w}x{h}:r={FPS}[bg];"
        f"[{content}]scale={s.w}:{s.h}:flags=lanczos,setsar=1[sl];"
        f"[0:v]scale={p.w}:{p.h}:flags=lanczos,setsar=1[pip];"
        f"[bg][sl]overlay={s.x}:{s.y}:shortest=0[t];"
        + _inset("t", "pip", p, decor, "c", 1, g)
        + f";[c]format=yuv420p,{BT709}[{out}]"
    )


def speaker_filter(decor: tuple[int, int] | None = None, content: str = "1:v", out: str = "v", g: Geometry = HD) -> str:
    """Inputs: [0:v] presenter, [content] at canvas size. Presenter full frame, content small. Output: [out]."""
    w, h = g.canvas
    c = g.styles["speaker"][0]
    return (
        f"[0:v]scale={w}:{h},setsar=1[bg];"
        f"[{content}]scale={c.w}:{c.h}:flags=lanczos,setsar=1[sm];"
        + _inset("bg", "sm", c, decor, "c", 0, g)
        + f";[c]format=yuv420p,{BT709}[{out}]"
    )


def full_filter(src: str = "0:v", out: str = "v", g: Geometry = HD) -> str:
    """[src] already at canvas size (presenter, slide PNG or screen) shown full frame. Output: [out]."""
    w, h = g.canvas
    return f"[{src}]scale={w}:{h},setsar=1,format=yuv420p,{BT709}[{out}]"


def screen_filter(src: str, crop: tuple[int, int, int, int] | None, out: str) -> str:
    """A screen recording that holds its last frame if it ends early; with crop (w, h, x, y), only
    that part of it (its 16:9 core, when it is the smaller picture: no bars around it)."""
    cropped = f",crop={crop[0]}:{crop[1]}:{crop[2]}:{crop[3]}" if crop else ""
    return f"[{src}]tpad=stop=-1:stop_mode=clone{cropped}[{out}]"


@dataclass(frozen=True)
class Fade:
    """When a timed overlay shows (seconds from the clip's start): fades in at `start`, stays `hold`."""

    start: float
    duration: float
    hold: float


def labels_filter(labels: list[tuple[int, Box]], timed: tuple[int, Box, Fade] | None, out: str) -> str:
    """Label PNGs (input index, box) over [l0], then one that fades in and out. Output: [out]."""
    graph, k = "", 0
    for k, (index, box) in enumerate(labels, 1):
        graph += f"[l{k - 1}][{index}:v]overlay={box.x}:{box.y}:shortest=0[l{k}];"
    if timed is not None:
        index, box, fade = timed
        fade_out = fade.start + fade.duration + fade.hold
        graph += (
            f"[{index}:v]format=rgba,fade=t=in:st={fade.start}:d={fade.duration}:alpha=1,"
            f"fade=t=out:st={fade_out}:d={fade.duration}:alpha=1[lt];"
            f"[l{k}][lt]overlay={box.x}:{box.y}:shortest=0:enable='lt(t,{fade_out + fade.duration})'[l{k + 1}];"
        )
        k += 1
    return graph + f"[l{k}]format=yuv420p,{BT709}[{out}]"


def clip_filter(
    kind: str,
    style: str,
    size: tuple[int, int] | None = None,
    decor: tuple[int, int] | None = None,
    crop: tuple[int, int, int, int] | None = None,
    labels: list[tuple[int, Box]] = (),
    timed: tuple[int, Box, Fade] | None = None,
    g: Geometry = HD,
) -> str:
    """A clip's whole filter graph on g's canvas. Inputs: [0:v] presenter (the content when it is shown
    alone), [1:v] content, then decor (mask, shadow) and label inputs at the given indices. Output: [v] at
    `size` (default: the canvas)."""
    size = size or g.canvas
    labelled, scaled = bool(labels) or timed is not None, size != g.canvas
    out = "l0" if labelled else "full" if scaled else "v"
    content = "0:v" if kind == "presenter" or style == "full" else "1:v"
    graph = ""
    if kind == "screen":
        graph, content = screen_filter(content, None if style == "full" else crop, "scr") + ";", "scr"
    if kind == "presenter" or style == "full":
        graph += full_filter(content, out, g)
    elif style == "speaker":
        graph += speaker_filter(decor, content, out, g)
    else:  # corner inset or side by side
        graph += inset_filter(style, decor, content, out, g)
    if labelled:
        graph += ";" + labels_filter(list(labels), timed, "full" if scaled else "v")
    if scaled:
        w, h = size
        graph += f";[full]scale={w}:{h}:flags=bicubic,{BT709}[v]"
    return graph
