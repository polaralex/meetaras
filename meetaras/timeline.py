"""Timeline file parser and segment model.

The timeline is the single source of truth: a header of `key value` settings and
timestamped commands (`0:45 slide 1`). Both the CLI and the UI use this module.

Layouts (what a command shows from its time on):
  presenter      the presenter video, full screen
  slide N        slide N large, the whole presenter video small in the bottom-right corner
  slide N side   slide N and the presenter video side by side
  slide N full   slide N only
  slide N speaker  the presenter full frame, slide N small in the corner
  screen         the screen recording, with the same three styles: screen / screen side / screen full
  cut            leave this part out of the video, until the next command
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

Layout = Literal[
    "presenter", "slide", "slide_side", "slide_full", "slide_speaker",
    "screen", "screen_side", "screen_full", "screen_speaker", "cut",
]  # fmt: skip
STYLES = ("", "side", "full", "speaker")  # how a slide or the screen recording is combined with the presenter

HEADER_KEYS = (
    "presenter", "audio", "slides", "screen", "screen_offset", "audio_offset", "audio_drift_ppm", "intro", "outro",
    "event", "date", "speaker", "role", "talk", "lower_third_every", "language", "loudness", "compression",
    "resolution",
)  # fmt: skip
INFO_KEYS = ("event", "date", "speaker", "role", "talk")  # free text shown in the video's labels
DEFAULT_LOUDNESS = -16.0  # LUFS; the usual target for speech (YouTube turns louder videos down to −14)
RESOLUTIONS = ("1080p", "4k")  # export sizes; the editor, the draft and previews are always 1080p
REQUIRED_KEYS = ("presenter", "slides")
PATH_KEYS = ("presenter", "audio", "slides", "screen", "intro", "outro")


class TimelineError(ValueError):
    def __init__(self, message: str, line: int | None = None):
        self.line = line
        super().__init__(f"line {line}: {message}" if line else message)


def layout_name(kind: str, style: str = "") -> Layout:
    """The layout a command shows: `slide` + `side` → "slide_side"; presenter and cut have no style."""
    return f"{kind}_{style}" if style and kind in ("slide", "screen") else kind  # type: ignore[return-value]


def command_text(kind: str, slide: int | None = None, style: str = "") -> str:
    """A command as written in the timeline: `slide 3 side`, `screen full`, `presenter`."""
    if kind == "slide":
        return f"slide {slide}" + (f" {style}" if style else "")
    if kind == "screen":
        return "screen" + (f" {style}" if style else "")
    return kind


@dataclass(frozen=True)
class Info:
    """The labels' text (cache keys use its repr: keep the name and fields)."""

    event: str = ""
    date: str = ""
    speaker: str = ""
    role: str = ""
    talk: str = ""

    @property
    def empty(self) -> bool:
        return not any((self.event, self.date, self.speaker, self.role, self.talk))

    def event_line(self) -> str:
        return " · ".join(x for x in (self.event, self.date) if x)

    def who_line(self) -> str:
        return " · ".join(x for x in (self.speaker, self.talk) if x)


@dataclass
class Event:
    time: float
    kind: Literal["start", "end", "presenter", "slide", "screen", "cut"]
    slide: int | None = None
    style: str = ""  # one of STYLES
    line: int | None = None
    note: str = ""  # the line's trailing comment, kept when the UI rewrites the file

    @property
    def layout(self) -> tuple[Layout, int | None]:
        kind = self.kind if self.kind in ("slide", "screen", "cut") else "presenter"
        return layout_name(kind, self.style), self.slide if kind == "slide" else None

    @property
    def command(self) -> str:
        return command_text(self.kind, self.slide, self.style)


@dataclass
class Segment:
    start: float
    end: float
    layout: Layout
    slide: int | None = None  # 1-based, only for the slide layouts

    @property
    def duration(self) -> float:
        return self.end - self.start

    @property
    def kind(self) -> str:
        """presenter, slide, screen or cut."""
        return self.layout.partition("_")[0]

    @property
    def style(self) -> str:
        """One of STYLES ("" for presenter and cut)."""
        return self.layout.partition("_")[2]

    @property
    def label(self) -> str:
        return command_text(self.kind, self.slide, self.style)


@dataclass
class Timeline:
    base_dir: Path
    presenter: Path
    slides: Path
    audio: Path | None = None
    screen: Path | None = None
    screen_offset: float = 0.0  # presenter time at which the screen recording starts
    intro: Path | None = None  # video played before the talk
    outro: Path | None = None  # video played after the talk
    event: str = ""
    date: str = ""
    speaker: str = ""
    role: str = ""
    talk: str = ""
    lower_third_every: float = 0.0  # minutes between lower thirds on presenter shots; 0: only the first
    loudness: float = DEFAULT_LOUDNESS
    compression: bool = True  # even out loud and quiet speech; off for sound already cleaned up (iPhone Studio audio mix)
    language: str = "auto"  # spoken language for captions (whisper code, e.g. en, el), or auto-detect
    resolution: str = "1080p"  # export size, one of RESOLUTIONS
    audio_offset: float | None = None
    audio_drift_ppm: float = 0.0
    events: list[Event] = field(default_factory=list)
    written: dict[str, str] = field(default_factory=dict)  # header paths as written, kept when rewriting the file

    @property
    def info(self) -> Info:
        return Info(*(getattr(self, k) for k in INFO_KEYS))

    @property
    def start(self) -> float | None:
        return next((e.time for e in self.events if e.kind == "start"), None)

    @property
    def end(self) -> float | None:
        return next((e.time for e in self.events if e.kind == "end"), None)

    def segments(self, video_duration: float) -> list[Segment]:
        """Turn the commands into contiguous segments covering [start, end]."""
        start = self.start if self.start is not None else 0.0
        end = self.end if self.end is not None else video_duration
        if start >= video_duration:
            raise TimelineError(f"start {format_time(start)} is beyond the video length {format_time(video_duration)}")
        if end > video_duration + 0.5:
            raise TimelineError(f"end {format_time(end)} is beyond the video length {format_time(video_duration)}")
        end = min(end, video_duration)
        if end <= start:
            raise TimelineError("end must be after start")

        layout: Layout = "presenter"
        slide: int | None = None
        changes: list[tuple[float, Layout, int | None]] = []
        for e in self.events:
            if e.kind in ("start", "end"):
                continue
            new = e.layout
            if e.time <= start:
                layout, slide = new  # last command before start defines the opening layout
            elif e.time < end:
                changes.append((e.time, *new))

        segments: list[Segment] = []
        t = start
        for time, new_layout, new_slide in changes:
            if time > t and (new_layout, new_slide) != (layout, slide):
                segments.append(Segment(t, time, layout, slide))
                t = time
            layout, slide = new_layout, new_slide
        segments.append(Segment(t, end, layout, slide))
        return segments

    def validate_slides(self, page_count: int) -> None:
        for e in self.events:
            if e.kind == "slide" and e.slide is not None and e.slide > page_count:
                raise TimelineError(f"slide {e.slide} does not exist; the PDF has {page_count} pages", e.line)

    def to_text(self) -> str:
        """Serialize in canonical form (used by the UI)."""

        def rel(key: str) -> str:
            p, as_written = getattr(self, key), self.written.get(key)
            if as_written is not None and path_from_header(as_written, self.base_dir) == p:
                return as_written
            return header_path(p, self.base_dir)

        lines = [f"presenter  {rel('presenter')}"]
        if self.audio:
            lines.append(f"audio      {rel('audio')}")
        lines.append(f"slides     {rel('slides')}")
        if self.screen:
            lines.append(f"screen     {rel('screen')}")
            lines.append(f"screen_offset {self.screen_offset:.3f}")
        if self.intro:
            lines.append(f"intro      {rel('intro')}")
        if self.outro:
            lines.append(f"outro      {rel('outro')}")
        for key in INFO_KEYS:
            if getattr(self, key):
                lines.append(f"{key:<10} {getattr(self, key)}")
        if self.lower_third_every:
            lines.append(f"lower_third_every {self.lower_third_every:g}")
        if self.loudness != DEFAULT_LOUDNESS:
            lines.append(f"loudness   {self.loudness:g}")
        if not self.compression:
            lines.append("compression off")
        if self.resolution != "1080p":
            lines.append(f"resolution {self.resolution}")
        if self.language != "auto":
            lines.append(f"language   {self.language}")
        if self.audio_offset is not None:
            lines.append(f"audio_offset {self.audio_offset:.3f}")
            if self.audio_drift_ppm:
                lines.append(f"audio_drift_ppm {self.audio_drift_ppm:.1f}")
        lines.append("")
        for e in self.events:
            line = f"{format_time(e.time):<8} {e.command}"
            lines.append(f"{line:<24} # {e.note}" if e.note else line)
        return "\n".join(lines) + "\n"


def absolute(p: Path | str) -> Path:
    """Absolute and normalised, without following symlinks (a linked video stays inside the talk folder)."""
    return Path(os.path.normpath(Path(p).expanduser().absolute()))


def path_from_header(text: str, base_dir: Path) -> Path:
    """A header path: relative to the timeline's folder unless absolute."""
    return absolute(Path(base_dir) / Path(text).expanduser())


def header_path(p: Path, base_dir: Path) -> str:
    """How a file is written in the header: relative inside the talk folder (portable), else absolute."""
    p, base = absolute(p), absolute(base_dir)
    return str(p.relative_to(base)) if p.is_relative_to(base) else str(p)


_TIME_RE = re.compile(r"^(?:(\d+):)?(?:(\d+):)?(\d+(?:\.\d+)?)$")


def parse_time(text: str) -> float:
    """Parse `s`, `m:ss`, `h:mm:ss`, each with optional fractional seconds."""
    m = _TIME_RE.match(text.strip())
    if not m:
        raise ValueError(f"invalid time {text!r} (expected m:ss, h:mm:ss or seconds)")
    a, b, sec = m.groups()
    seconds = float(sec)
    if a is not None and b is not None:
        hours, minutes = int(a), int(b)
    elif a is not None:
        hours, minutes = 0, int(a)
    else:
        hours, minutes = 0, 0
    if (a is not None) and seconds >= 60:
        raise ValueError(f"invalid time {text!r}: seconds must be below 60")
    if b is not None and minutes >= 60:
        raise ValueError(f"invalid time {text!r}: minutes must be below 60")
    return hours * 3600 + minutes * 60 + seconds


def format_time(t: float) -> str:
    """Format seconds as m:ss or h:mm:ss, keeping up to 3 decimals when needed."""
    ms = round(t * 1000)
    h, rem = divmod(ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, frac = divmod(rem, 1000)
    sec = f"{s:02d}" + (f".{frac:03d}".rstrip("0") if frac else "")
    return f"{h}:{m:02d}:{sec}" if h else f"{m}:{sec}"


def _split_comment(line: str) -> tuple[str, str]:
    # A line starting with `#` is a comment; elsewhere a comment is ` # …` (hash then space), so
    # text like "Meetup #101" stays text.
    if line.lstrip().startswith("#"):
        return "", line.lstrip()[1:].strip()
    m = re.search(r"\s#(?=\s|$)", line)
    if not m:
        return line.strip(), ""
    return line[: m.start()].strip(), line[m.end() :].strip()


def parse(text: str, base_dir: Path) -> Timeline:
    header: dict[str, tuple[str, int]] = {}
    events: list[Event] = []

    for lineno, raw in enumerate(text.splitlines(), start=1):
        line, comment = _split_comment(raw)
        if not line:
            continue
        first, _, rest = line.partition(" ")
        rest = rest.strip()

        if first[:1].isdigit():  # a time: the line is a command
            try:
                t = parse_time(first)
            except ValueError as exc:
                raise TimelineError(str(exc), lineno) from None
            event = _parse_command(t, rest, lineno)
            event.note = comment
            events.append(event)
            continue

        if first not in HEADER_KEYS:
            raise TimelineError(f"unknown setting {first!r} (expected one of: {', '.join(HEADER_KEYS)})", lineno)
        if not rest:
            raise TimelineError(f"{first} needs a value", lineno)
        if first in header:
            raise TimelineError(f"{first} is set twice (first on line {header[first][1]})", lineno)
        header[first] = (rest, lineno)

    for key in REQUIRED_KEYS:
        if key not in header:
            raise TimelineError(f"missing required setting {key!r}")

    _check_order(events)

    def path(key: str) -> Path:
        return path_from_header(header[key][0], base_dir)

    def switch(key: str) -> bool:
        value, lineno = header[key]
        if value.lower() not in ("on", "off"):
            raise TimelineError(f"{key} must be on or off, got {value!r}", lineno)
        return value.lower() == "on"

    def choice(key: str, values: tuple[str, ...]) -> str:
        value, lineno = header[key]
        if value.lower() not in values:
            raise TimelineError(f"{key} must be {' or '.join(values)}, got {value!r}", lineno)
        return value.lower()

    def number(key: str, lo: float | None = None, hi: float | None = None) -> float:
        value, lineno = header[key]
        try:
            x = float(value)
        except ValueError:
            raise TimelineError(f"{key} must be a number, got {value!r}", lineno) from None
        if (lo is not None and x < lo) or (hi is not None and x > hi):
            raise TimelineError(f"{key} must be between {lo} and {hi}", lineno)
        return x

    if "screen" not in header:
        for e in events:
            if e.kind == "screen":
                raise TimelineError("`screen` needs a `screen <recording>` setting in the header", e.line)

    return Timeline(
        base_dir=absolute(base_dir),
        presenter=path("presenter"),
        slides=path("slides"),
        audio=path("audio") if "audio" in header else None,
        screen=path("screen") if "screen" in header else None,
        screen_offset=number("screen_offset") if "screen_offset" in header else 0.0,
        intro=path("intro") if "intro" in header else None,
        outro=path("outro") if "outro" in header else None,
        **{key: header[key][0] for key in INFO_KEYS if key in header},
        lower_third_every=number("lower_third_every", 0) if "lower_third_every" in header else 0.0,
        language=header["language"][0] if "language" in header else "auto",
        loudness=number("loudness", -30, -9) if "loudness" in header else DEFAULT_LOUDNESS,
        compression=switch("compression") if "compression" in header else True,
        resolution=choice("resolution", RESOLUTIONS) if "resolution" in header else "1080p",
        audio_offset=number("audio_offset") if "audio_offset" in header else None,
        audio_drift_ppm=number("audio_drift_ppm") if "audio_drift_ppm" in header else 0.0,
        events=events,
        written={key: header[key][0] for key in PATH_KEYS if key in header},
    )


def _parse_command(t: float, rest: str, lineno: int) -> Event:
    parts = rest.split()
    if not parts:
        raise TimelineError("missing command after time (start, end, presenter or slide N)", lineno)
    cmd = parts[0]
    if cmd in ("start", "end", "presenter", "cut"):
        if len(parts) > 1:
            raise TimelineError(f"{cmd} takes no arguments", lineno)
        return Event(t, cmd, line=lineno)  # type: ignore[arg-type]
    if cmd == "slide":
        if len(parts) not in (2, 3) or not parts[1].isdigit() or int(parts[1]) < 1:
            raise TimelineError("slide needs a page number of 1 or more, e.g. `slide 3`, `slide 3 side` or `slide 3 full`", lineno)
        if len(parts) == 3 and parts[2] not in STYLES:
            raise TimelineError(f"unexpected {parts[2]!r} after slide {parts[1]} (expected `side`, `full` or `speaker`)", lineno)
        return Event(t, "slide", int(parts[1]), style=parts[2] if len(parts) == 3 else "", line=lineno)
    if cmd == "screen":
        if len(parts) > 2 or (len(parts) == 2 and parts[1] not in STYLES):
            raise TimelineError("expected `screen`, `screen side`, `screen full` or `screen speaker`", lineno)
        return Event(t, "screen", style=parts[1] if len(parts) == 2 else "", line=lineno)
    raise TimelineError(
        f"unknown command {cmd!r} (expected start, end, presenter, slide N [side|full|speaker], screen [side|full|speaker] or cut)", lineno
    )


def _check_order(events: list[Event]) -> None:
    seen: dict[str, int] = {}
    prev: Event | None = None
    for e in events:
        if e.kind in ("start", "end"):
            if e.kind in seen:
                raise TimelineError(f"{e.kind} appears twice (first on line {seen[e.kind]})", e.line)
            seen[e.kind] = e.line or 0
        if prev is not None and e.time < prev.time:
            raise TimelineError(
                f"time {format_time(e.time)} is before the previous command at {format_time(prev.time)}", e.line
            )
        prev = e


def load(path: Path) -> Timeline:
    path = Path(path)
    if not path.is_file():
        raise TimelineError(f"timeline file not found: {path}")
    return parse(path.read_text(encoding="utf-8"), path.parent)
