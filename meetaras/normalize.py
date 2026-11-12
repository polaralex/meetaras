"""Any camera video (HEVC/HDR/VFR, rotation, …), screen recording or intro/outro → 1080p30 SDR H.264.

The 1080p file is what the editor, the draft, captions and the sound use. A 4K talk's export also gets a
4K master of each video (picture only), made in the same decoding pass when neither exists yet.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from . import cache, ffmpeg
from .layouts import BACKGROUND, BT709, CANVAS, FPS, RESOLUTIONS
from .timeline import Timeline

Kind = Literal["presenter", "screen", "intro", "outro"]
KINDS: tuple[Kind, ...] = ("presenter", "screen", "intro", "outro")  # also the timeline's keys for them

AUDIO_RATE = 48000  # every converted file and the export
AUDIO_BITRATE = "192k"
COLOR_TAGS = ["-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709", "-color_range", "tv"]
# Video settings of every converted file, and how each file ends.
ENCODE = ["-fps_mode", "cfr", "-c:v", "libx264", "-preset", "veryfast", "-crf", "16", "-pix_fmt", "yuv420p",
          "-g", str(FPS * 2), *COLOR_TAGS]  # fmt: skip
FILE_END = ["-movflags", "+faststart", "-map_metadata", "-1"]

# zscale names for ffprobe's color properties
_ZSCALE_TRANSFER = {"arib-std-b67": "arib-std-b67", "smpte2084": "smpte2084"}
_ZSCALE_MATRIX = {"bt2020nc": "2020_ncl", "bt2020c": "2020_cl", "bt709": "709"}
_ZSCALE_PRIMARIES = {"bt2020": "2020", "bt709": "709"}
# SDR matrices other than BT.709 (webcams, older cameras): converted, not just tagged as BT.709.
_BT601 = ("bt470bg", "smpte170m")


def fit_size(info: ffmpeg.VideoInfo, canvas: tuple[int, int] = CANVAS) -> tuple[int, int]:
    """Largest even size with the source's aspect ratio that fits the canvas."""
    w, h = canvas
    scale = min(w / info.width, h / info.height)
    return int(info.width * scale) // 2 * 2, int(info.height * scale) // 2 * 2


def tonemap_filter(info: ffmpeg.VideoInfo, size: tuple[int, int]) -> str:
    """HDR (HLG/PQ, BT.2020) → SDR BT.709 via linear light + Hable tone-mapping.

    Resizes in the first zscale step, so the expensive float processing runs at the output size (1080p
    for a 4K source, unless a 4K master is made).
    """
    tin = _ZSCALE_TRANSFER[info.color_transfer]
    min_ = _ZSCALE_MATRIX.get(info.color_space, "2020_ncl")
    pin = _ZSCALE_PRIMARIES.get(info.color_primaries, "2020")
    w, h = size
    # npl=203: HLG/PQ reference white (ITU-R BT.2408), so diffuse white lands at SDR white. With npl=100
    # and desat=0, real iPhone footage came out oversaturated with blown, hue-shifted highlights
    # (a white projector screen turned cyan). Hable's default desaturation keeps highlights neutral.
    return (
        f"zscale=w={w}:h={h}:f=spline36:tin={tin}:min={min_}:pin={pin}:t=linear:npl=203,"
        "format=gbrpf32le,"
        "zscale=p=709,"
        "tonemap=tonemap=hable,"
        "zscale=t=709:m=709:r=tv,"
        "format=yuv420p"
    )


def is_bt601(info: ffmpeg.VideoInfo) -> bool:
    return not info.is_hdr and info.color_space in _BT601


def _resize(info: ffmpeg.VideoInfo, canvas: tuple[int, int]) -> str:
    """Source → its fit size on the canvas, in BT.709 SDR."""
    size = fit_size(info, canvas)
    if info.is_hdr:
        return tonemap_filter(info, size)
    if is_bt601(info):
        return f"scale={size[0]}:{size[1]}:flags=lanczos:in_color_matrix=bt601:out_color_matrix=bt709"
    return f"scale={size[0]}:{size[1]}:flags=lanczos"


def _place(canvas: tuple[int, int], pad_color: str) -> list[str]:
    """The resized picture centred on the canvas, tagged BT.709."""
    w, h = canvas
    return [f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color={pad_color}", "setsar=1", "format=yuv420p", BT709]


def video_filter(info: ffmpeg.VideoInfo, pad_color: str = "black", canvas: tuple[int, int] = CANVAS) -> str:
    # fps first, so 60 fps sources don't pay for frames that get dropped
    return ",".join([f"fps={FPS}", _resize(info, canvas), *_place(canvas, pad_color)])


def split_filter(info: ffmpeg.VideoInfo, pad_color: str, big: tuple[int, int]) -> str:
    """One decoding pass for two sizes: [big] at `big` and [hd] at the canvas. HDR is tone-mapped once,
    at the big size, and the 1080p picture made from that."""
    if info.is_hdr:
        hd = fit_size(info)
        head, to_big, to_hd = [f"fps={FPS}", _resize(info, big)], [], [f"scale={hd[0]}:{hd[1]}:flags=lanczos"]
    else:
        head, to_big, to_hd = [f"fps={FPS}"], [_resize(info, big)], [_resize(info, CANVAS)]
    return (
        f"[0:v]{','.join(head)},split=2[a][b];"
        f"[a]{','.join(to_big + _place(big, pad_color))}[big];"
        f"[b]{','.join(to_hd + _place(CANVAS, pad_color))}[hd]"
    )


def already_normalized(info: ffmpeg.VideoInfo, canvas: tuple[int, int] = CANVAS) -> bool:
    """True when the file is exactly what the pipeline needs at this canvas size, so its video can be
    copied as is (e.g. a recording pre-converted on the Mac, or a "Most Compatible" iPhone recording at 1080p30)."""
    return (
        info.codec == "h264"
        and info.pix_fmt == "yuv420p"
        and (info.width, info.height) == canvas
        and info.rotation == 0
        and info.constant_fps
        and round(info.fps, 3) == FPS
        and not info.is_hdr
        and info.color_transfer in ("", "bt709", "unknown")
        and not is_bt601(info)
    )


def pad_color(kind: Kind) -> str:
    """Screen recordings get bars in the background colour, so they can be placed like a slide image."""
    return BACKGROUND if kind == "screen" else "black"


def _copies(info: ffmpeg.VideoInfo, kind: Kind, canvas: tuple[int, int]) -> bool:
    """The video can be copied as it is (screen recordings are always converted)."""
    return kind != "screen" and already_normalized(info, canvas)


def size_name(canvas: tuple[int, int]) -> str:
    return "4K" if canvas == RESOLUTIONS["4k"].canvas else f"{canvas[1]}p"


@lru_cache(maxsize=64)
def _probe(fingerprint: str, src: Path) -> ffmpeg.VideoInfo | None:
    try:
        return ffmpeg.probe_video(src)
    except ffmpeg.FFmpegError:
        return None  # not a video ffmpeg can read: normalize says so


def _converts_matrix(fingerprint: str, src: Path) -> bool:
    info = _probe(fingerprint, src)
    return info is not None and is_bt601(info)


def master_canvas(src: Path, canvas: tuple[int, int]) -> tuple[int, int]:
    """The size of the file an export at `canvas` takes `src`'s picture from. A source no bigger than
    1080p (after rotation) gets no 4K master: the export scales its 1080p file up instead."""
    info = _probe(cache.file_fingerprint(src), src) if canvas != CANVAS else None
    if info is None or (info.width <= CANVAS[0] and info.height <= CANVAS[1]):
        return CANVAS
    return canvas


def _path(src: Path, work_dir: Path, kind: Kind, bt601: bool, canvas: tuple[int, int] = CANVAS) -> Path:
    extra: tuple[str, ...] = () if kind == "presenter" else (pad_color(kind),)
    extra += ("bt601",) if bt601 else ()
    return work_dir / f"{kind}-{cache.key('normalize', cache.file_fingerprint(src), canvas, FPS, *extra)}.mp4"


def normalized_path(src: Path, work_dir: Path, kind: Kind, canvas: tuple[int, int] = CANVAS) -> Path:
    """Where the normalized `kind` video of `src` at this canvas size is (or will be) cached."""
    return _path(src, work_dir, kind, _converts_matrix(cache.file_fingerprint(src), src), canvas)


def earlier_path(src: Path, work_dir: Path, kind: Kind) -> Path | None:
    """Where versions that only tagged a BT.601 video as BT.709 kept it (wrong colours, the same
    sound: what was made from its sound still fits); None for other videos."""
    return _path(src, work_dir, kind, False) if _converts_matrix(cache.file_fingerprint(src), src) else None


def normalize(src: Path, work_dir: Path, kind: Kind, canvas: tuple[int, int] = CANVAS) -> Path:
    """Return the path of the normalized `kind` video of `src` at this canvas size, building it if needed.

    Other sizes than the 1080p canvas (4K export masters) are picture only: the sound, and with it every
    measurement and transcript, stays with the 1080p file. When that one is missing too, one pass makes both.
    """
    out = normalized_path(src, work_dir, kind, canvas)
    if out.exists():
        print(f"  using cached {out.name}")
        return out

    info = ffmpeg.probe_video(src)
    if info.height > info.width and kind == "presenter":
        raise ffmpeg.FFmpegError(
            f"{src.name} is portrait ({info.width}×{info.height} after rotation). Record the talk in landscape."
        )
    print(
        f"  source: {info.width}×{info.height}, {info.fps:.2f} fps, {info.duration / 60:.1f} min"
        + (f", HDR ({info.color_transfer}) → tone-mapping to SDR" if info.is_hdr else ", SDR")
        + (f" ({info.color_space} → BT.709)" if is_bt601(info) else "")
    )

    work_dir.mkdir(parents=True, exist_ok=True)
    master, size = canvas != CANVAS, size_name(canvas)  # master: a 4K export master, no sound
    hd = normalized_path(src, work_dir, kind)
    both = master and not hd.exists() and not _copies(info, kind, canvas) and not _copies(info, kind, CANVAS)
    audio = []
    if info.has_audio and (both or not master):
        audio = ["-map", f"0:a:{info.audio_index}", "-c:a", "aac", "-b:a", AUDIO_BITRATE, "-ar", str(AUDIO_RATE)]
    if both:
        print(f"  converting to 1080p (editing) and {size} (export) in one pass")
        with cache.atomic_output(out) as tmp_big, cache.atomic_output(hd) as tmp_hd:
            ffmpeg.run(
                ["-i", str(src), "-filter_complex", split_filter(info, pad_color(kind), canvas),
                 "-map", "[hd]", *ENCODE, *audio, *FILE_END, str(tmp_hd),
                 "-map", "[big]", *ENCODE, *FILE_END, str(tmp_big)],
                label=f"normalize 1080p and {size}", duration=info.duration,
            )  # fmt: skip
        return out
    if _copies(info, kind, canvas):
        print(f"  already {size}30 H.264 SDR: copying instead of converting")
        args = [
            "-i", str(src), "-map", "0:v:0", "-c:v", "copy",
            # tag the colours explicitly, as a converted file would be
            "-bsf:v", "h264_metadata=colour_primaries=1:transfer_characteristics=1:matrix_coefficients=1",
        ]  # fmt: skip
        if audio and info.audio_codec == "aac" and info.audio_rate == AUDIO_RATE:
            audio = [*audio[:2], "-c:a", "copy"]
        label = "copy"
    else:
        args = ["-i", str(src), "-map", "0:v:0", "-vf", video_filter(info, pad_color(kind), canvas), *ENCODE]
        label = "normalize"
    with cache.atomic_output(out) as tmp:
        ffmpeg.run([*args, *audio, *FILE_END, str(tmp)], label=f"{label} {size}" if master else label, duration=info.duration)
    return out


def _sources(tl: Timeline) -> list[tuple[Kind, Path]]:
    """The talk's videos: the camera, and the optional ones that are there."""
    return [(k, src) for k in KINDS if (src := getattr(tl, k)) is not None and (k == "presenter" or src.is_file())]


def export_masters(tl: Timeline, work_dir: Path) -> dict[Kind, Path]:
    """Where a 4K talk's export masters are (or will be): one per video bigger than 1080p. {} at 1080p."""
    canvas = RESOLUTIONS[tl.resolution].canvas
    out: dict[Kind, Path] = {}
    for kind, src in _sources(tl):
        if (size := master_canvas(src, canvas)) != CANVAS:
            out[kind] = normalized_path(src, work_dir, kind, size)
    return out


def talk_videos(tl: Timeline, work_dir: Path, convert: bool = True, export: bool = False) -> dict[Kind, Path]:
    """The talk's videos at 1080p (editor, draft, sound), converted (or, with convert=False, where they will
    be): {kind: path}. Optional ones that aren't there are left out. With export=True, a 4K talk's export
    masters are made in the same pass where the 1080p file isn't there yet; the others are left to the
    export (render makes whatever is missing)."""
    masters = export_masters(tl, work_dir) if convert and export else {}
    out: dict[Kind, Path] = {}
    for kind, src in _sources(tl):
        if kind in masters and not normalized_path(src, work_dir, kind).exists():
            normalize(src, work_dir, kind, master_canvas(src, RESOLUTIONS[tl.resolution].canvas))
        out[kind] = normalize(src, work_dir, kind) if convert else normalized_path(src, work_dir, kind)
    return out
