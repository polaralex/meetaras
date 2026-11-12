"""Timeline → segments → clips → concat → audio → output.mp4."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path

from . import align as align_mod
from . import cache, ffmpeg, layouts, progress, slides
from .labels import (
    LT_HOLD,
    LT_IN,
    LT_TIMING,
    Overlay,
    for_segment,
    inset_decor,
    lower_third_segments,
    render_overlays,
)
from .layouts import FPS, HD, RESOLUTIONS, Geometry
from .normalize import (
    AUDIO_BITRATE,
    AUDIO_RATE,
    COLOR_TAGS,
    Kind,
    fit_size,
    master_canvas,
    normalize,
    normalized_path,
)
from .timeline import DEFAULT_LOUDNESS, Segment, Timeline, TimelineError, format_time

TRUE_PEAK = -1.0  # dBTP ceiling; the loudness target itself is per talk (Timeline.loudness)


@dataclass(frozen=True)
class Quality:
    name: str
    size: tuple[int, int]
    preset: str
    crf: int
    loudness_corrections: int  # times the final gain is measured on the real chain and corrected

    def __str__(self) -> str:
        # What cache keys see (clips are keyed on it): the form from before the field was renamed.
        return (
            f"Quality(name={self.name!r}, size={self.size!r}, preset={self.preset!r}, crf={self.crf!r}, "
            f"two_pass_loudnorm={self.loudness_corrections > 1!r})"
        )


FINAL = Quality("final", (1920, 1080), "medium", 18, 3)
FINAL_4K = Quality("final-4k", (3840, 2160), "medium", 18, 3)
PREVIEW = Quality("preview", (1280, 720), "ultrafast", 28, 1)

# Identical for every clip so the concat demuxer can join them without re-encoding.
TIMESCALE = 15360


@dataclass
class FrameSegment:
    first: int  # first frame (inclusive) in the presenter clock at FPS
    last: int  # exclusive
    segment: Segment

    @property
    def frames(self) -> int:
        return self.last - self.first


def to_frames(segments: list[Segment]) -> list[FrameSegment]:
    """Snap every boundary to a frame once, so clip lengths add up exactly to the audio length."""
    out = []
    for s in segments:
        first, last = round(s.start * FPS), round(s.end * FPS)
        if last > first:
            out.append(FrameSegment(first, last, s))
    return out


def screen_or_presenter(segments: list[Segment], lo: float, hi: float) -> list[Segment]:
    """Where a screen layout falls outside the screen recording [lo, hi), show the presenter instead."""
    out: list[Segment] = []
    for s in segments:
        if s.kind != "screen":
            out.append(s)
            continue
        for a, b, layout in ((s.start, min(s.end, lo), "presenter"), (max(s.start, lo), min(s.end, hi), s.layout),
                             (max(s.start, hi), s.end, "presenter")):  # fmt: skip
            if b > a:
                out.append(Segment(a, b, layout))  # type: ignore[arg-type]
    return out


def kept_ranges(frame_segments: list[FrameSegment]) -> list[tuple[int, int]]:
    """Contiguous frame ranges that end up in the output (adjacent segments merged)."""
    ranges: list[list[int]] = []
    for fs in frame_segments:
        if ranges and ranges[-1][1] == fs.first:
            ranges[-1][1] = fs.last
        else:
            ranges.append([fs.first, fs.last])
    return [(a, b) for a, b in ranges]


def kept_segments(segments: list[Segment]) -> list[FrameSegment]:
    """The segments that end up in the output, in frames (cuts left out)."""
    return [fs for fs in to_frames(segments) if fs.segment.kind != "cut"]


@dataclass
class Edit:
    """The final edit as a map from talk time to output time: chapters and captions use the same one."""

    segments: list[FrameSegment]  # what is kept, no cuts
    intro: float = 0.0  # seconds before the talk
    outro: float = 0.0

    @cached_property
    def ranges(self) -> list[tuple[int, int]]:
        return kept_ranges(self.segments)

    @property
    def total(self) -> float:
        """Length of the whole video."""
        return self.intro + sum(b - a for a, b in self.ranges) / FPS + self.outro

    def output_time(self, t: float) -> float | None:
        """Where talk time t is in the video; None where it is cut."""
        base = self.intro
        for a, b in self.ranges:
            if a / FPS <= t < b / FPS:
                return base + t - a / FPS
            base += (b - a) / FPS
        return None


def the_edit(tl: Timeline, work: Path) -> Edit:
    """The edit as exported, without converting anything: lengths come from the converted videos, or,
    where a video isn't converted yet, from its source (close, not frame-exact)."""
    master = normalized_path(tl.presenter, work, "presenter")
    segments = kept_segments(tl.segments(ffmpeg.probe_duration(master if master.exists() else tl.presenter)))
    intro, outro = (bumper_length(getattr(tl, k), k, work) if getattr(tl, k) else 0.0 for k in ("intro", "outro"))
    return Edit(segments, intro, outro)


def step(n: int, total: int, text: str) -> None:
    print(f"[{n}/{total}] {text}", flush=True)


def video_codec_args(q: Quality) -> list[str]:
    return [
        "-c:v", "libx264", "-preset", q.preset, "-crf", str(q.crf),
        "-profile:v", "high", "-pix_fmt", "yuv420p", "-r", str(FPS),
        "-g", str(FPS * 2), "-video_track_timescale", str(TIMESCALE),
        *COLOR_TAGS,
    ]  # fmt: skip


@dataclass
class Sources:
    master: Path  # normalized presenter video (canvas size)
    slide_pngs: list[Path]
    screen: Path | None = None  # normalized screen recording (canvas size)
    screen_offset: float = 0.0  # presenter time at which the screen recording starts
    screen_duration: float = 0.0
    screen_crop: tuple[int, int, int, int] | None = None  # w, h, x, y: the recording's 16:9 core, for insets
    work: Path | None = None  # for the inset decor images
    g: Geometry = HD  # the canvas everything above is made for


def _seek(t: float) -> str:
    # Half a frame early: accurate seeking then starts exactly at the intended frame.
    return f"{max(0.0, t - 0.5 / FPS):.6f}"


def render_clip(
    fs: FrameSegment, src: Sources, q: Quality, out: Path, overlays: list[Overlay] = (), lower_third: Overlay | None = None
) -> None:
    s, t = fs.segment, fs.first / FPS
    presenter = ["-ss", _seek(t), "-i", str(src.master)]
    if s.kind == "slide":
        assert s.slide is not None
        content = ["-loop", "1", "-framerate", str(FPS), "-i", str(src.slide_pngs[s.slide - 1])]
    elif s.kind == "screen":
        assert src.screen is not None
        st = t - src.screen_offset
        if st < -0.5 / FPS or st >= src.screen_duration:
            raise ffmpeg.FFmpegError(
                f"screen at {format_time(t)} is outside the screen recording "
                f"({format_time(src.screen_offset)}–{format_time(src.screen_offset + src.screen_duration)} "
                f"with screen_offset {src.screen_offset:g})"
            )
        content = ["-ss", _seek(st), "-i", str(src.screen)]
    else:
        content = []
    # Inputs: presenter, content (the content alone when it is shown alone), decor, labels.
    args = presenter if s.kind == "presenter" else content if s.style == "full" else presenter + content
    decor = None
    if s.kind != "presenter" and s.style != "full" and layouts.STYLES[s.style][2] and src.work is not None:
        content_box, pip_box, inset = src.g.styles[s.style]
        box = pip_box if inset == "pip" else content_box
        mask, shadow = inset_decor(box.w, box.h, src.work, src.g)
        args += ["-loop", "1", "-i", str(mask), "-loop", "1", "-i", str(shadow)]
        decor = (2, 3)
    n = args.count("-i")
    for ov in [*overlays, *([lower_third] if lower_third else [])]:
        args += ["-loop", "1", "-framerate", str(FPS), "-i", str(ov.path)]
    graph = layouts.clip_filter(
        s.kind, s.style, q.size, decor, src.screen_crop,
        labels=[(n + i, ov.box) for i, ov in enumerate(overlays)],
        timed=(n + len(overlays), lower_third.box, LT_TIMING) if lower_third else None, g=src.g,
    )  # fmt: skip
    with cache.atomic_output(out) as tmp:
        ffmpeg.run(
            [*args, "-filter_complex", graph, "-map", "[v]", "-an", "-frames:v", str(fs.frames),
             *video_codec_args(q), str(tmp)],
            label="clip", duration=fs.frames / FPS, quiet=True,
        )  # fmt: skip


def screen_crop(recording: Path, canvas: tuple[int, int] = layouts.CANVAS) -> tuple[int, int, int, int] | None:
    """Where the recording's centre 16:9 area sits in its normalized (canvas-sized, barred) version."""
    fw, fh = fit_size(ffmpeg.probe_video(recording), canvas)
    cw, ch = canvas
    x0, y0 = (cw - fw) // 2, (ch - fh) // 2
    if abs(fw / fh - 16 / 9) < 0.01:
        return None if (fw, fh) == canvas else (fw, fh, x0, y0)
    if fw / fh < 16 / 9:  # taller than 16:9 (e.g. a 16:10 Mac screen): trim top and bottom
        h = round(fw * 9 / 16 / 2) * 2
        return fw, h, x0, y0 + (fh - h) // 2
    w = round(fh * 16 / 9 / 2) * 2
    return w, fh, x0 + (fw - w) // 2, y0


def clip_key(fs: FrameSegment, src: Sources, q: Quality, ovs: list[Overlay] = (), lt: Overlay | None = None) -> str:
    """The clip's cache key: what it is made from. Only the geometry a clip actually uses goes into it,
    so adding a style elsewhere keeps the cache."""
    s, g = fs.segment, src.g
    png = src.slide_pngs[s.slide - 1] if s.slide else None
    uses_presenter = s.style != "full"
    uses_screen = s.kind == "screen"
    k = cache.key(
        "clip", fs.first, fs.last, s.layout, q,
        (g.canvas, g.styles.get(s.style), layouts.BACKGROUND, g.decor_id),
        src.screen_crop if uses_screen else None,
        src.master.name if uses_presenter else None,  # names are already content-keyed
        png.parent.name + png.name if png else None,
        (src.screen.name, src.screen_offset) if uses_screen and src.screen else None,
    )  # fmt: skip
    if ovs or lt:  # labels change the picture: part of the clip's identity
        k = cache.key(k, [(o.path.name, o.box) for o in ovs], (lt.path.name, lt.box, LT_IN, LT_HOLD) if lt else None)
    return k


def render_clips(
    frame_segments: list[FrameSegment],
    src: Sources,
    q: Quality,
    work: Path,
    overlays: dict[str, Overlay] | None = None,
    lower_third_at: set[float] = frozenset(),
) -> list[Path]:
    overlays = overlays or {}
    clip_dir = work / "clips"
    clip_dir.mkdir(parents=True, exist_ok=True)
    clips = []
    total = len(frame_segments)
    for i, fs in enumerate(frame_segments, 1):
        s = fs.segment
        ovs = for_segment(s, overlays)
        lt = overlays.get("lower_third") if s.start in lower_third_at else None
        out = clip_dir / f"{clip_key(fs, src, q, ovs, lt)}.mp4"
        status = "cached" if out.exists() else "rendering"
        print(f"  clip {i}/{total}: {format_time(s.start)}–{format_time(s.end)} {s.label} ({status})", flush=True)
        progress.report("clips", (i - 1) / total)
        if not out.exists():
            render_clip(fs, src, q, out, ovs, lt)
        clips.append(out)
    return clips


def concat_list(clips: list[Path], listing: Path) -> list[str]:
    """ffmpeg input args that read the clips as one video (concat demuxer, no re-encode)."""
    quoted = (str(c.resolve()).replace("'", "'\\''") for c in clips)  # a ' ends the quotes, \' is one, ' reopens
    listing.write_text("".join(f"file '{q}'\n" for q in quoted))
    return ["-f", "concat", "-safe", "0", "-i", str(listing)]


def audio_source(tl: Timeline, master: Path, work: Path) -> tuple[list[str], str, int]:
    """Return (ffmpeg input args, filter chain, audio track) producing the chosen audio in the presenter clock.

    The chain reads the input's audio track of that number ([N:a:track]); its output timeline matches
    the presenter video: t=0 is the start of the video.
    """
    if tl.audio is None:
        info = ffmpeg.probe_video(master)
        if not info.has_audio:
            return ["-f", "lavfi", "-i", f"anullsrc=r={AUDIO_RATE}:cl=mono"], f"aresample={AUDIO_RATE}", 0
        return ["-i", str(master)], f"aresample={AUDIO_RATE}", 0  # (converted: its one track)

    track = ffmpeg.audio_track(ffmpeg.probe_json(tl.audio))  # the same one align.load_mono reads
    if track is None:
        raise ffmpeg.FFmpegError(
            f"{tl.audio.name} has no sound track that can be read. "
            "Save the sound as a .wav or .m4a file and use that as `audio`."
        )
    if tl.audio_offset is not None:
        a = align_mod.Alignment(tl.audio_offset, tl.audio_drift_ppm, [])
        print(f"  using manual audio_offset {a.offset:+.3f} s, drift {a.drift_ppm:+.1f} ppm")
    else:
        a = align_mod.align_files(master, tl.audio, work)
        print(align_mod.describe(a))
    rate = 1 + a.drift_ppm / 1e6
    ext_rate = int(track[1]["sample_rate"])
    # After asetrate the external stream runs `rate` times faster: stream time s = ext_time / rate.
    # Presenter time t sits at s = t + shift.
    shift = a.offset / rate
    chain = f"asetrate={ext_rate * rate:.6f},aresample={AUDIO_RATE}"
    if shift >= 0:
        chain += f",atrim=start={shift:.6f},asetpts=PTS-STARTPTS"
    else:
        chain += f",adelay={-shift * 1000:.3f}:all=1"
    return ["-i", str(tl.audio)], chain, track[0]


def measure_loudness(inputs: list[str], graph: str, duration: float | None = None) -> float:
    """Integrated loudness (LUFS) of a filter graph that reads input 0 and ends in [a]; -inf for silence."""
    stderr = ffmpeg.run(
        [*inputs, "-filter_complex", f"{graph};[a]loudnorm=print_format=json[m]", "-map", "[m]", "-f", "null", "-"],
        label="loudness", duration=duration,
    )
    match = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", stderr)
    if not match:
        raise ffmpeg.FFmpegError("could not read loudnorm measurement")
    return float(json.loads(match.group(0))["input_i"])


def measured(work: Path | None, parts: tuple, measure: Callable[[], dict]) -> tuple[dict, bool]:
    """(measurements, whether they were cached). Measuring decodes the whole talk's audio a few times,
    so they are kept in .work by what they were made from: an export after a visual edit skips them."""
    if work is None:
        return measure(), False
    path = work / f"loudness-{cache.key('loudness', *parts)}.json"
    if path.exists():
        return json.loads(path.read_text()), True
    result = measure()
    work.mkdir(parents=True, exist_ok=True)
    with cache.atomic_output(path) as tmp:
        tmp.write_text(json.dumps(result))
    return result, False


def input_ids(inputs: list[str]) -> list[str]:
    """ffmpeg input args with each file as its fingerprint (for cache keys)."""
    return [
        cache.file_fingerprint(Path(a)) if prev == "-i" and Path(a).is_file() else a
        for prev, a in zip(["", *inputs], inputs)
    ]


FADE = 0.01  # seconds of fade at each cut, so joins don't click


def audio_graph(inp: str, base: str, ranges: list[tuple[int, int]], tail: str = "", out: str = "a") -> str:
    """Filter graph: [inp] → presenter-clock audio (`base`) → the kept frame ranges, joined → `tail` → [a].

    Each piece is exactly as long as its frames, so audio and video stay in step across cuts.
    """
    tail = f",{tail}" if tail else ""

    def piece(first: int, last: int) -> str:
        n = round((last - first) / FPS * AUDIO_RATE)
        p = (
            f"atrim=start={first / FPS:.6f},asetpts=PTS-STARTPTS,"
            f"apad=whole_len={n},atrim=end_sample={n}"
        )
        if len(ranges) > 1:
            p += f",afade=t=in:d={FADE},afade=t=out:st={n / AUDIO_RATE - FADE:.6f}:d={FADE}"
        return p

    if len(ranges) == 1:
        return f"[{inp}]{base},{piece(*ranges[0])}{tail}[{out}]"
    split = "".join(f"[s{i}]" for i in range(len(ranges)))
    pieces = ";".join(f"[s{i}]{piece(*r)}[p{i}]" for i, r in enumerate(ranges))
    joined = "".join(f"[p{i}]" for i in range(len(ranges)))
    return f"[{inp}]{base},asplit={len(ranges)}{split};{pieces};{joined}concat=n={len(ranges)}:v=0:a=1{tail}[{out}]"


SPEECH_LEVEL = -20.0  # LUFS the compressor is tuned for
COMPRESSOR = "acompressor=threshold=-26dB:ratio=2.5:attack=10:release=200:knee=6"
LIMIT = 10 ** ((TRUE_PEAK - 0.3) / 20)  # small margin for AAC encoding
# Limiting at 4× oversampling catches inter-sample peaks, so this approximates a true-peak limiter.
LIMITER = (
    f"aresample={AUDIO_RATE * 4},alimiter=limit={LIMIT:.4f}:attack=5:release=50:level=disabled:latency=1,"
    f"aresample={AUDIO_RATE}"
)


def build_audio_graph(
    base: str, ranges: list[tuple[int, int]], q: Quality, inputs: list[str], track: int = 0, index: int = 1,
    out: str = "a", target: float = DEFAULT_LOUDNESS, compress: bool = True, work: Path | None = None,
) -> str:
    """The kept ranges of the chosen audio, brought to the loudness target; reads [index:a:track], ends in [out].

    Chain: level to SPEECH_LEVEL → gentle compression → gain → limiter. A plain gain to −16 LUFS
    would push typical speech peaks well over −1 dBTP (loudnorm's linear mode then silently gives
    up), so compression narrows the peak-to-loudness ratio first. The final gain is measured on the
    real chain and corrected until it lands, so the limiter's effect is accounted for. The
    measurements are cached in `work`, when given.
    """
    def pre(raw: float) -> str:
        # Without compression (sound already cleaned up): the same level and limiter, so only the evening out goes.
        return f"volume={SPEECH_LEVEL - raw:.2f}dB" + (f",{COMPRESSOR}" if compress else "")

    def chain(raw: float, gain: float) -> str:
        return f"{pre(raw)},volume={gain:.2f}dB,{LIMITER}"

    def measure(tail: str) -> float:  # measurements read the audio as input 0
        seconds = sum(b - a for a, b in ranges) / FPS
        return measure_loudness(inputs, audio_graph(f"0:a:{track}", base, ranges, tail), seconds)

    def measure_all() -> dict:
        raw = measure("")
        if raw == float("-inf"):
            return {"raw": raw, "gain": None}
        gain = target - measure(pre(raw))
        # The limiter takes off a little loudness: correct until within 0.2 LU.
        for _ in range(q.loudness_corrections):
            error = target - measure(chain(raw, gain))
            if abs(error) < 0.2:
                break
            gain += error
        return {"raw": raw, "gain": gain}

    parts = (input_ids(inputs), track, base, ranges, compress, target, q.loudness_corrections)
    m, cached = measured(work, (*parts, SPEECH_LEVEL, COMPRESSOR, LIMITER), measure_all)
    inp = f"{index}:a:{track}"
    if m["gain"] is None:
        return audio_graph(inp, base, ranges, out=out)
    print(f"  loudness {m['raw']:.1f} LUFS → {target:g} LUFS" + (" (measured before)" if cached else ""))
    return audio_graph(inp, base, ranges, chain(m["raw"], m["gain"]), out)


# Every audio part is brought to this format before parts are joined.
AUDIO_FORMAT = f"aformat=sample_fmts=fltp:sample_rates={AUDIO_RATE}:channel_layouts=stereo"


@dataclass
class Bumper:
    """An intro or outro: converted once, rendered as a clip like the talk's, audio levelled on its own."""

    clip: Path
    master: Path  # the 1080p file: its sound
    frames: int
    gain: float | None  # dB to reach the loudness target; None when it has no (audible) sound

    def audio_input(self) -> list[str]:
        if self.gain is None:
            return ["-f", "lavfi", "-i", f"anullsrc=r={AUDIO_RATE}:cl=stereo"]
        return ["-i", str(self.master)]

    def audio_graph(self, idx: int, out: str) -> str:
        n = round(self.frames / FPS * AUDIO_RATE)
        level = f"volume={self.gain:.2f}dB,{LIMITER}," if self.gain is not None else ""
        return f"[{idx}:a]aresample={AUDIO_RATE},{level}apad=whole_len={n},atrim=end_sample={n},{AUDIO_FORMAT}[{out}]"


def frame_count(probe: dict) -> int:
    """Frames of a converted video, from its probe (its file duration can include audio padding past the last frame)."""
    stream = next(st for st in probe["streams"] if st["codec_type"] == "video")
    return max(1, int(stream.get("nb_frames") or round(float(stream["duration"]) * FPS)))


def bumper_length(path: Path, kind: Kind, work: Path) -> float:
    """Seconds an intro or outro takes in the video, from a probe (prepare_bumper renders it)."""
    master = normalized_path(path, work, kind)
    if master.exists():
        return frame_count(ffmpeg.probe_json(master)) / FPS
    return max(1, round(ffmpeg.probe_duration(path) * FPS)) / FPS  # not converted yet


def bumper_clip(picture: Path, kind: Kind, work: Path, q: Quality, g: Geometry = HD) -> Path:
    """Where an intro's or outro's clip is cached (picture: its converted file at g's size)."""
    return work / "clips" / f"{kind}-{cache.key(kind, picture.name, q, g.canvas)}.mp4"


def masters(src: Path, kind: Kind, work: Path, g: Geometry = HD) -> tuple[Path, Path]:
    """(the 1080p file: sound and timing; the picture for g's canvas), converted. The picture is the 1080p
    file too at 1080p, and for sources no bigger than that (the clip graphs scale it up). The export
    master first, so that one pass makes both where neither exists."""
    canvas = master_canvas(src, g.canvas)
    picture = normalize(src, work, kind, canvas) if canvas != layouts.CANVAS else None
    hd = normalize(src, work, kind)
    return hd, picture or hd


def prepare_bumper(
    path: Path, kind: Kind, work: Path, q: Quality, target: float = DEFAULT_LOUDNESS, g: Geometry = HD
) -> Bumper:
    master, picture = masters(path, kind, work, g)
    probe = ffmpeg.probe_json(master)
    frames = frame_count(probe)
    clip = bumper_clip(picture, kind, work, q, g)
    if not clip.exists():
        clip.parent.mkdir(parents=True, exist_ok=True)
        graph = layouts.clip_filter("presenter", "", q.size, g=g)
        with cache.atomic_output(clip) as tmp:
            ffmpeg.run(
                ["-i", str(picture), "-filter_complex", graph, "-map", "[v]", "-an",
                 "-frames:v", str(frames), *video_codec_args(q), str(tmp)],
                label=kind, quiet=True,
            )  # fmt: skip
    gain = None
    if ffmpeg.audio_track(probe) is not None:
        graph = f"[0:a]aresample={AUDIO_RATE}[a]"
        m, _ = measured(work, (master.name, graph), lambda: {"loudness": measure_loudness(["-i", str(master)], graph)})
        if m["loudness"] != float("-inf"):
            gain = target - m["loudness"]
    print(f"  {kind}: {path.name}, {format_time(frames / FPS)}" + ("" if gain is not None else ", no sound"))
    return Bumper(clip, master, frames, gain)


def with_bumpers(talk: str, inputs: list[str], intro: Bumper | None, outro: Bumper | None) -> str:
    """The talk's audio graph (ending in [talk_raw]) between the intro's and outro's sound, ending in [a].
    Appends their inputs to `inputs`: the talk audio is input 1 (after the video), then one per bumper."""
    graph = talk + f";[talk_raw]{AUDIO_FORMAT}[talk]"
    named = [(b, name) for b, name in ((intro, "intro"), (outro, "outro")) if b]
    for i, (b, name) in enumerate(named):
        inputs += b.audio_input()
        graph += ";" + b.audio_graph(2 + i, name)
    order = (["intro"] if intro else []) + ["talk"] + (["outro"] if outro else [])
    return graph + ";" + "".join(f"[{n}]" for n in order) + f"concat=n={len(order)}:v=0:a=1[a]"


def render(timeline_path: Path, tl: Timeline, output: Path, preview: bool = False) -> Path:
    """The video of the timeline at its `resolution`; previews are 720p, made from the 1080p pipeline."""
    g = HD if preview else RESOLUTIONS[tl.resolution]
    q = PREVIEW if preview else FINAL if g == HD else FINAL_4K
    work = timeline_path.parent / ".work"
    work.mkdir(exist_ok=True)
    for label, p in (
        ("presenter video", tl.presenter), ("slides", tl.slides), ("audio", tl.audio),
        ("screen recording", tl.screen), ("intro", tl.intro), ("outro", tl.outro),
    ):  # fmt: skip
        if p is not None and not p.is_file():
            raise FileNotFoundError(f"{label} file not found: {p}")
    total = 5

    step(1, total, f"Normalizing presenter video ({tl.presenter.name})" + (", for a 4K export" if g != HD else ""))
    master, picture = masters(tl.presenter, "presenter", work, g)  # (master: the sound and the timing)
    duration = ffmpeg.probe_duration(master)

    step(2, total, f"Rendering slides ({tl.slides.name})")
    slide_pngs = slides.render_slides(tl.slides, work, g.canvas)
    tl.validate_slides(len(slide_pngs))
    print(f"  {len(slide_pngs)} slides" + (" at 4K" if g != HD else ""))
    src = Sources(picture, slide_pngs, work=work, g=g)
    if tl.screen is not None and any(e.kind == "screen" for e in tl.events):
        print(f"  normalizing screen recording ({tl.screen.name})")
        screen, src.screen = masters(tl.screen, "screen", work, g)
        src.screen_offset = tl.screen_offset
        src.screen_duration = ffmpeg.probe_duration(screen)
        src.screen_crop = screen_crop(tl.screen, master_canvas(tl.screen, g.canvas))  # in its picture's pixels

    segments = tl.segments(duration)
    if src.screen is not None:
        segments = screen_or_presenter(segments, src.screen_offset, src.screen_offset + src.screen_duration)
    frame_segments = kept_segments(segments)
    if not frame_segments:
        raise TimelineError("everything is cut; nothing left to render")
    ranges = kept_ranges(frame_segments)
    frames_out = sum(b - a for a, b in ranges)
    cut = len(ranges) - 1
    step(3, total, f"Rendering {len(frame_segments)} segments ({q.name} quality, {format_time(frames_out / FPS)}"
         + (f", {cut} cut{'s' if cut > 1 else ''}" if cut else "") + ")")
    overlays = render_overlays(tl.info, work, g)
    lt_at = lower_third_segments([fs.segment for fs in frame_segments], tl.lower_third_every)
    if overlays:
        print(f"  labels: {', '.join(overlays)}" + (f"; lower third at {len(lt_at)} presenter shot(s)" if lt_at else ""))
    clips = render_clips(frame_segments, src, q, work, overlays, lt_at)
    intro = prepare_bumper(tl.intro, "intro", work, q, tl.loudness, g) if tl.intro else None
    outro = prepare_bumper(tl.outro, "outro", work, q, tl.loudness, g) if tl.outro else None
    all_clips = ([intro.clip] if intro else []) + clips + ([outro.clip] if outro else [])
    joined = concat_list(all_clips, work / f"clips-{q.name}.txt")
    frames_out += sum(b.frames for b in (intro, outro) if b)

    step(4, total, "Preparing audio" + (f" (aligning {tl.audio.name})" if tl.audio else " (camera audio)"))
    inputs, base, track = audio_source(tl, master, work)
    bumpers = intro is not None or outro is not None
    graph = build_audio_graph(base, ranges, q, inputs, track, out="talk_raw" if bumpers else "a", target=tl.loudness,
                              compress=tl.compression, work=work)  # fmt: skip
    if bumpers:
        graph = with_bumpers(graph, inputs, intro, outro)

    step(5, total, f"Exporting {output.name}")
    output.parent.mkdir(parents=True, exist_ok=True)
    with cache.atomic_output(output) as tmp:
        ffmpeg.run(
            [
                *joined, *inputs,
                "-filter_complex", graph,
                "-map", "0:v", "-map", "[a]",
                "-c:v", "copy", "-c:a", "aac", "-b:a", AUDIO_BITRATE, "-ar", str(AUDIO_RATE),
                "-movflags", "+faststart", str(tmp),
            ],
            label="export", duration=frames_out / FPS,
        )  # fmt: skip
    from .captions import (  # (captions builds on this module)
        transcript_path,
        write_captions,
    )

    transcript = transcript_path(tl, master, work)
    if transcript is not None:
        edit = Edit(frame_segments, intro.frames / FPS if intro else 0.0, outro.frames / FPS if outro else 0.0)
        n = write_captions(transcript, edit, output.with_suffix(".srt"))
        print(f"  captions: {n} cues → {output.with_suffix('.srt').name}")
    return output
