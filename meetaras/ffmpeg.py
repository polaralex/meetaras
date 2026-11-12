"""Thin wrapper around ffmpeg/ffprobe: run with progress, clear errors, probe."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import threading
from collections import deque
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from . import progress


class FFmpegError(RuntimeError):
    pass


def _binary(name: str) -> str:
    path = shutil.which(name)
    if not path:
        from .tools import install_hint

        raise FFmpegError(f"{name} not found on PATH. {install_hint('ffmpeg')}")
    return path


def run(
    args: list[str],
    *,
    label: str = "ffmpeg",
    duration: float | None = None,
    quiet: bool = False,
) -> str:
    """Run ffmpeg with the given arguments (without the leading 'ffmpeg').

    Shows a percentage progress line when `duration` (seconds of output) is given.
    Returns stderr (needed e.g. for loudnorm's measurement JSON). Raises FFmpegError
    with the tail of stderr on failure.
    """
    cmd = [_binary("ffmpeg"), "-hide_banner", "-nostdin", "-y", "-nostats", "-progress", "pipe:1", *args]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)

    stderr_lines: list[str] = []

    def read_stderr() -> None:
        assert proc.stderr is not None
        for line in proc.stderr:
            stderr_lines.append(line)

    reader = threading.Thread(target=read_stderr, daemon=True)
    reader.start()

    tty = sys.stderr.isatty()
    show = not quiet
    last_step = -1
    assert proc.stdout is not None
    for line in proc.stdout:
        key, _, value = line.strip().partition("=")
        if show and duration and key == "out_time_us" and value.isdigit():
            pct = min(100.0, int(value) / 1e6 / duration * 100)
            progress.report(label, pct / 100)
            if tty:
                sys.stderr.write(f"\r  {label}: {pct:5.1f}%")
                sys.stderr.flush()
            elif int(pct // 5) > last_step:  # not a terminal (e.g. run by the editor): a line every 5 %
                last_step = int(pct // 5)
                print(f"  {label}: {last_step * 5}%", flush=True)
    proc.wait()
    reader.join()
    proc.stdout.close()
    proc.stderr.close()
    if show and duration and tty:
        sys.stderr.write(f"\r  {label}: done   \n" if proc.returncode == 0 else "\n")

    stderr = "".join(stderr_lines)
    if proc.returncode != 0:
        tail = "".join(deque(stderr_lines, maxlen=15))
        raise FFmpegError(f"{label} failed (exit {proc.returncode}).\nCommand: {' '.join(cmd)}\n{tail}")
    return stderr


def stream(args: list[str], size: int, label: str = "ffmpeg") -> Iterator[bytes]:
    """Run ffmpeg and yield its output in pieces of `size` bytes (raw video frames: never the whole
    video in memory). Raises FFmpegError with the tail of stderr if it fails; a caller that stops
    early stops ffmpeg, which is no error."""
    cmd = [_binary("ffmpeg"), "-hide_banner", "-nostdin", "-loglevel", "error", *args]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    stderr_lines: deque[bytes] = deque(maxlen=15)
    assert proc.stdout is not None and proc.stderr is not None
    reader = threading.Thread(target=stderr_lines.extend, args=(proc.stderr,), daemon=True)  # a full pipe would block ffmpeg
    reader.start()
    finished = False
    try:
        while len(buf := proc.stdout.read(size)) == size:
            yield buf
        finished = True
    finally:
        proc.stdout.close()
        if not finished:
            proc.kill()
        proc.wait()
        reader.join()
        proc.stderr.close()
    if proc.returncode != 0:
        tail = b"".join(stderr_lines).decode(errors="replace")
        raise FFmpegError(f"{label} failed (exit {proc.returncode}).\nCommand: {' '.join(cmd)}\n{tail}")


def run_raw(args: list[str]) -> bytes:
    """Run ffmpeg and return its stdout bytes (for piping raw audio)."""
    cmd = [_binary("ffmpeg"), "-hide_banner", "-nostdin", "-loglevel", "error", *args]
    proc = subprocess.run(cmd, capture_output=True)
    if proc.returncode != 0:
        raise FFmpegError(f"ffmpeg failed: {proc.stderr.decode(errors='replace')[-2000:]}")
    return proc.stdout


@dataclass
class VideoInfo:
    width: int  # after rotation
    height: int
    rotation: int
    duration: float
    fps: float
    color_transfer: str
    color_primaries: str
    color_space: str
    has_audio: bool
    codec: str = ""
    pix_fmt: str = ""
    constant_fps: bool = False  # declared and average frame rate agree exactly
    audio_codec: str = ""
    audio_rate: int = 0
    audio_index: int = 0  # which of the file's audio tracks to use (for -map 0:a:N)

    @property
    def is_hdr(self) -> bool:
        return self.color_transfer in ("arib-std-b67", "smpte2084")


def probe_json(path: Path) -> dict:
    cmd = [_binary("ffprobe"), "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise FFmpegError(f"Cannot read {path}: {proc.stderr.strip()}")
    return json.loads(proc.stdout)


def audio_track(data: dict) -> tuple[int, dict] | None:
    """The audio track to use: (its number among the audio tracks, its stream info), or None.

    The first track ffmpeg can decode, preferring the one marked default: iPhone recordings with
    spatial audio put an `apac` track (no decoder) before the stereo one.
    """
    tracks = [s for s in data["streams"] if s.get("codec_type") == "audio"]
    usable = [(i, s) for i, s in enumerate(tracks) if s.get("codec_name") not in (None, "", "unknown", "none")]
    usable.sort(key=lambda t: not t[1].get("disposition", {}).get("default"))  # stable: defaults first
    return usable[0] if usable else None


def _rotation(stream: dict) -> int:
    for sd in stream.get("side_data_list", []):
        if "rotation" in sd:
            return int(round(float(sd["rotation"])))
    tag = stream.get("tags", {}).get("rotate")
    return int(tag) if tag else 0


def probe_video(path: Path) -> VideoInfo:
    data = probe_json(path)
    video = next((s for s in data["streams"] if s.get("codec_type") == "video"), None)
    if video is None:
        raise FFmpegError(f"{path} has no video stream")
    audio_index, audio = audio_track(data) or (0, None)
    rotation = _rotation(video)
    w, h = int(video["width"]), int(video["height"])
    if abs(rotation) % 180 == 90:
        w, h = h, w
    num, _, den = video.get("avg_frame_rate", "0/1").partition("/")
    fps = float(num) / float(den) if float(den or 0) else 0.0
    duration = float(data["format"].get("duration") or video.get("duration") or 0)
    return VideoInfo(
        width=w,
        height=h,
        rotation=rotation,
        duration=duration,
        fps=fps,
        color_transfer=video.get("color_transfer", ""),
        color_primaries=video.get("color_primaries", ""),
        color_space=video.get("color_space", ""),
        has_audio=audio is not None,
        codec=video.get("codec_name", ""),
        pix_fmt=video.get("pix_fmt", ""),
        constant_fps=video.get("avg_frame_rate") == video.get("r_frame_rate"),
        audio_codec=audio.get("codec_name", "") if audio else "",
        audio_rate=int(audio.get("sample_rate", 0)) if audio else 0,
        audio_index=audio_index,
    )


def probe_duration(path: Path) -> float:
    return float(probe_json(path)["format"]["duration"])
