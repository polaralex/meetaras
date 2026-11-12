"""Find the offset and clock drift of an external audio recording relative to the presenter video.

Model: ext_time = offset + presenter_time × (1 + drift_ppm / 1e6)
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np
from scipy import signal

from . import cache, ffmpeg
from .timeline import format_time

SAMPLE_RATE = 8000
WINDOW = 30.0  # seconds of presenter audio matched against the whole external track
POSITIONS = (0.15, 0.5, 0.85)  # where in the talk the windows are taken
MIN_CONFIDENCE = 8.0  # peak height in standard deviations of the correlation


@dataclass
class Match:
    presenter_time: float  # start of the window in the presenter clock
    lag: float  # ext_time - presenter_time at that point
    confidence: float


@dataclass
class Alignment:
    offset: float
    drift_ppm: float
    matches: list[Match]

    def ext_time(self, presenter_time: float) -> float:
        return self.offset + presenter_time * (1 + self.drift_ppm / 1e6)


class AlignmentError(RuntimeError):
    pass


def load_mono(path: Path, sr: int = SAMPLE_RATE) -> np.ndarray:
    track = ffmpeg.audio_track(ffmpeg.probe_json(path))
    if track is None:
        raise AlignmentError(f"{path.name} has no sound that can be read")
    raw = ffmpeg.run_raw(["-i", str(path), "-map", f"0:a:{track[0]}", "-ac", "1", "-ar", str(sr), "-f", "f32le", "-"])
    return np.frombuffer(raw, dtype=np.float32)


def _prepare(x: np.ndarray) -> np.ndarray:
    # First difference acts as a simple high-pass (removes rumble/DC that differs between mics),
    # then normalize so the recordings' levels don't matter.
    y = np.diff(x, prepend=x[:1]).astype(np.float32)
    std = y.std()
    return y / std if std > 0 else y


def find_lag(window: np.ndarray, ext: np.ndarray, sr: int = SAMPLE_RATE) -> tuple[float, float]:
    """Locate `window` inside `ext`. Returns (position in seconds, confidence)."""
    if len(ext) < len(window):
        pad = np.zeros(len(window) - len(ext) + 1, dtype=np.float32)
        ext = np.concatenate([ext, pad])
    corr = signal.oaconvolve(ext, window[::-1], mode="valid")
    peak = int(np.argmax(corr))
    # Confidence: how far the peak stands out, ignoring ±0.5 s around it.
    guard = sr // 2
    rest = np.concatenate([corr[: max(0, peak - guard)], corr[peak + guard :]])
    spread = rest.std() if len(rest) > 1 else 1.0
    confidence = float((corr[peak] - rest.mean()) / spread) if spread > 0 else 0.0
    # Parabolic interpolation for sub-sample precision.
    frac = 0.0
    if 0 < peak < len(corr) - 1:
        a, b, c = corr[peak - 1], corr[peak], corr[peak + 1]
        denom = a - 2 * b + c
        if denom != 0:
            frac = 0.5 * (a - c) / denom
    return float((peak + frac) / sr), confidence


def align(presenter_audio: np.ndarray, ext_audio: np.ndarray, sr: int = SAMPLE_RATE) -> Alignment:
    ref = _prepare(presenter_audio)
    ext = _prepare(ext_audio)
    duration = len(ref) / sr
    win = min(WINDOW, duration * 0.3)
    n = int(win * sr)

    matches: list[Match] = []
    for pos in POSITIONS:
        t0 = max(0.0, min(duration - win, duration * pos - win / 2))
        i0 = int(t0 * sr)
        window = ref[i0 : i0 + n]
        if window.std() == 0:
            continue
        found, confidence = find_lag(window, ext, sr)
        # With drift, the window fits best around its center, so the measured lag belongs there.
        matches.append(Match(presenter_time=i0 / sr + win / 2, lag=float(found - i0 / sr), confidence=confidence))

    good = [m for m in matches if m.confidence >= MIN_CONFIDENCE]
    if not good:
        best = max((m.confidence for m in matches), default=0)
        raise AlignmentError(
            f"could not find the presenter audio in the external recording (best confidence {best:.1f}, "
            f"need {MIN_CONFIDENCE}). Check that both recordings cover the same talk, or set audio_offset manually."
        )
    if len(good) == 1:
        return Alignment(offset=good[0].lag, drift_ppm=0.0, matches=matches)

    t = np.array([m.presenter_time for m in good])
    lag = np.array([m.lag for m in good])
    slope, intercept = np.polyfit(t, lag, 1)
    # Reject inconsistent matches (e.g. one window landed on a repeated sound).
    residual = np.abs(lag - (intercept + slope * t))
    if len(good) >= 3 and residual.max() > 0.05:
        keep = residual < 0.05
        if keep.sum() >= 2:
            slope, intercept = np.polyfit(t[keep], lag[keep], 1)
    return Alignment(offset=float(intercept), drift_ppm=float(slope * 1e6), matches=matches)


def align_files(presenter: Path, external: Path, work_dir: Path) -> Alignment:
    """Align with caching in .work/align-<key>.json."""
    out = work_dir / f"align-{cache.key('align', cache.file_fingerprint(presenter), cache.file_fingerprint(external))}.json"
    if out.exists():
        data = json.loads(out.read_text())
        return Alignment(data["offset"], data["drift_ppm"], [Match(**m) for m in data["matches"]])
    result = align(load_mono(presenter), load_mono(external))
    work_dir.mkdir(parents=True, exist_ok=True)
    with cache.atomic_output(out) as tmp:
        tmp.write_text(json.dumps(asdict(result), indent=2))
    return result


def describe(a: Alignment) -> str:
    lines = [f"  offset: {a.offset:+.3f} s   drift: {a.drift_ppm:+.1f} ppm"]
    for m in a.matches:
        flag = "" if m.confidence >= MIN_CONFIDENCE else "  (ignored: low confidence)"
        lines.append(f"    at {format_time(round(m.presenter_time, 1)):>9}: lag {m.lag:+.4f} s, confidence {m.confidence:.1f}{flag}")
    return "\n".join(lines)
