"""Captions: transcribe once with whisper.cpp (word timestamps, in presenter time), then write an SRT
fitted to each export (trimming, cuts and the intro shift every word to output time).
"""

from __future__ import annotations

import json
import math
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from . import cache, deliver, ffmpeg, progress, tools
from .layouts import FPS
from .normalize import earlier_path
from .render import Edit, audio_source, the_edit
from .slides import page_texts
from .timeline import Timeline, format_time

DTW_PRESET = "large.v3.turbo"  # must match the model, for word timestamps

MAX_LINE = 42  # characters per caption line (common broadcast/YouTube guidance)
MAX_LINES = 2
MAX_CUE = 6.0  # seconds
MIN_CUE = 1.0
PAUSE = 0.8  # a silence this long starts a new caption


def available() -> bool:
    """whisper.cpp and its model are installed, so captions can be made on this machine."""
    return None not in tools.find_whisper()


class CaptionError(RuntimeError):
    pass


@dataclass
class Word:
    start: float  # seconds, presenter time
    end: float
    text: str


# ---------- transcription ----------


def vocabulary(pdf: Path, extra: list[str] = (), limit: int = 60) -> list[str]:
    """Distinctive terms from the slides (product names, acronyms) to help whisper spell them."""
    common = {"the", "and", "for", "with", "you", "your", "what", "about", "from", "data", "model", "models"}
    seen: dict[str, str] = {}
    text = " ".join(page_texts(pdf))
    for term in extra:  # given explicitly (speaker, event): always included
        seen.setdefault(term.lower(), term)
    explicit = len(seen)
    ranked: dict[str, tuple[int, int, str]] = {}  # key → (rank, first position, term)
    for pos, term in enumerate(re.findall(r"[A-Za-z][A-Za-z0-9.+#-]*[A-Za-z0-9+#]", text)):
        key = term.lower()
        if key in common or key in seen or key in ranked or len(term) < 2:
            continue
        if any(c.isupper() for c in term[1:]) or any(c.isdigit() for c in term):
            ranked[key] = (0, pos, term)  # MLOps, PyTorch, vLLM, S3: what speech recognition gets wrong
        elif term[0].isupper():
            ranked[key] = (1, pos, term)  # capitalised names, but also ordinary title-case words
    best = sorted(ranked.values())[: max(0, limit - explicit)]
    return list(seen.values()) + [t for _, _, t in sorted(best, key=lambda r: r[1])]


def build_prompt(talk: str, terms: list[str]) -> str:
    head = f"{talk}. " if talk else ""
    return f"{head}Terms: {', '.join(terms)}."


CHUNK = 600.0  # seconds per whisper run; each piece is saved, so an interruption only loses one piece


# Every whisper-cli run (here and in the Mac script): full JSON with DTW word times, non-speech tokens suppressed.
# -nfa: flash attention (on by default) silently disables DTW, leaving only coarse token times
# (measured: words placed in pauses, over a second early). Costs ~25% speed on CPU.
# No --vad: in this whisper.cpp version, VAD shifts timestamps (measured: seconds of drift within a
# minute), and captions must line up with the speech. The 10-minute pieces limit drift-free decoding errors.
WHISPER_FLAGS = ("-ojf", "--dtw", DTW_PRESET, "-sns", "-nfa")


def _whisper(wav: Path, out_base: Path, prompt: str, language: str, live, offset: float, on_time) -> None:
    """Run whisper-cli on one file; stream its segment lines (shifted by offset) to `live`."""
    cli, model = tools.find_whisper()
    cmd = [str(cli), "-m", str(model), "-f", str(wav), "-l", language, "-t", str(os.cpu_count() or 4),
           "-of", str(out_base), "--prompt", prompt, *WHISPER_FLAGS]  # fmt: skip
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    tail: list[str] = []  # for the error message
    assert proc.stdout is not None
    for line in proc.stdout:
        tail = (tail + [line])[-20:]
        m = re.match(r"\[(\d+):(\d+):(\d+)\.(\d+) --> [^\]]*\]\s*(.*)", line)  # segment lines
        if m:
            t = offset + int(m[1]) * 3600 + int(m[2]) * 60 + int(m[3])
            live.write(f"[{format_time(t)}] {m[5]}\n")
            live.flush()
            on_time(t)
    if proc.wait() != 0:
        raise CaptionError("whisper.cpp failed:\n" + "".join(tail))


def _shift(data: dict, offset: float) -> list[dict]:
    """whisper JSON segments with all times moved by offset seconds."""
    ms, cs = round(offset * 1000), round(offset * 100)
    for seg in data.get("transcription", []):
        seg["offsets"] = {k: v + ms for k, v in seg.get("offsets", {}).items()}
        for tok in seg.get("tokens", []):
            tok["offsets"] = {k: v + ms for k, v in tok.get("offsets", {}).items()}
            if tok.get("t_dtw", -1) is not None and tok.get("t_dtw", -1) >= 0:
                tok["t_dtw"] += cs
    return data.get("transcription", [])


class Transcription:
    """A talk's transcription in CHUNK-second pieces, each saved as soon as it is recognised: it resumes
    after an interruption, and pieces can be recognised elsewhere (Transcribe on a Mac) and imported."""

    def __init__(self, audio_inputs: list[str], audio_filter: str, duration: float, language: str, work: Path, key_parts: tuple):
        self.audio_inputs, self.audio_filter, self.duration = audio_inputs, audio_filter, duration
        self.language, self.work, self.key_parts = language, work, key_parts
        # The prompt (slide terms, talk title) only nudges spelling; changing it doesn't redo an hour of work.
        self.name = f"transcript-{cache.key('transcript', key_parts, tools.MODEL_NAME, language)}"
        self.out = work / f"{self.name}.json"
        self.starts = [i * CHUNK for i in range(math.ceil(duration / CHUNK))]
        self.parts = [work / f"{self.name}-part{i:03d}.json" for i in range(len(self.starts))]

    def audio(self) -> Path:
        """The talk audio as 16 kHz mono, as whisper wants it (made once)."""
        wav = self.work / f"{self.name}.wav"
        if not wav.exists():
            print("  extracting audio (16 kHz mono)")
            self.work.mkdir(parents=True, exist_ok=True)
            with cache.atomic_output(wav) as tmp:
                ffmpeg.run([*self.audio_inputs, "-filter_complex", f"{self.audio_filter},aresample=16000[a]", "-map", "[a]",
                            "-ac", "1", "-t", f"{self.duration:.3f}", str(tmp)], label="audio", duration=self.duration)  # fmt: skip
        return wav

    def cut(self, i: int, dest: Path) -> None:
        ffmpeg.run(["-ss", f"{self.starts[i]:.3f}", "-t", f"{CHUNK:.3f}", "-i", str(self.audio()), "-c", "copy", str(dest)],
                   label="piece", quiet=True)  # fmt: skip

    def finish(self) -> Path:
        """Join the recognised pieces into the transcript, in presenter time."""
        segments: list[dict] = []
        for start, part in zip(self.starts, self.parts):
            segments += _shift(json.loads(part.read_text()), start)
        self.out.write_text(json.dumps({"transcription": segments}))
        for part in self.parts:
            part.unlink()
        (self.work / f"{self.name}.wav").unlink(missing_ok=True)
        self.out.with_suffix(".key").write_text(repr(self.key_parts))  # lets exports find the transcript for this audio
        return self.out


def transcribe(audio_inputs: list[str], audio_filter: str, duration: float, prompt: str, language: str, work: Path, key_parts: tuple) -> Path:
    """Run whisper.cpp on the talk audio (presenter time); returns the cached JSON transcript.

    Pieces recognised earlier (or imported from a Mac) are kept; the live text grows piece by piece.
    """
    t = Transcription(audio_inputs, audio_filter, duration, language, work, key_parts)
    if t.out.exists():
        print(f"  using cached transcript {t.out.name}")
        return t.out
    if not all(part.exists() for part in t.parts):
        if not available():
            raise CaptionError("whisper.cpp or its model not found: `meetaras doctor --fix` sets them up (or the Setup page)")
        live = t.out.with_suffix(".live.txt")  # the text as it is recognised, for watching progress
        print(f"  transcribing {format_time(duration)} with whisper.cpp ({tools.MODEL_NAME}) in {CHUNK / 60:.0f}-minute pieces")
        print(f"  live text: {live}")
        last_pct = [-5]

        def on_time(t_: float) -> None:
            progress.report("transcribe", t_ / duration)
            pct = int(t_ / duration * 100)
            if pct >= last_pct[0] + 5:
                last_pct[0] = pct - pct % 5
                print(f"  transcribe: {last_pct[0]}%", flush=True)

        with live.open("a", encoding="utf-8") as live_file:
            for i, (start, part) in enumerate(zip(t.starts, t.parts)):
                if part.exists():
                    print(f"  piece {i + 1}: done earlier")
                    continue
                piece = part.with_suffix(".wav")
                t.cut(i, piece)
                base = part.with_suffix(".tmp")
                _whisper(piece, base, prompt, language, live_file, start, on_time)
                Path(f"{base}.json").replace(part)  # whisper writes <base>.json; keep it only when complete
                piece.unlink(missing_ok=True)
    return t.finish()


# ---------- a talk's transcript ----------


def transcript_key(tl: Timeline, master: Path) -> tuple:
    """What a transcript was made from (the talk's audio); recorded next to it, so exports find it."""
    return (master.name, cache.file_fingerprint(tl.audio) if tl.audio else None, tl.audio_offset, tl.audio_drift_ppm)


def transcription(tl: Timeline, master: Path, work: Path) -> tuple[Transcription, str]:
    """The talk's transcription (its audio, pieces and cache names) and the prompt for whisper."""
    inputs, base, track = audio_source(tl, master, work)
    terms = vocabulary(tl.slides, [x for x in (tl.speaker, tl.event) if x])
    t = Transcription(inputs, f"[0:a:{track}]{base}", ffmpeg.probe_duration(master), tl.language, work, transcript_key(tl, master))
    return t, build_prompt(tl.talk, terms)


def transcribe_talk(tl: Timeline, master: Path, work: Path) -> Path:
    """The talk's transcript (whisper.cpp; slow, once): `meetaras captions` and make's captions step."""
    t, prompt = transcription(tl, master, work)
    return transcribe(t.audio_inputs, t.audio_filter, t.duration, prompt, t.language, work, t.key_parts)


def transcript_path(tl: Timeline, master: Path, work: Path) -> Path | None:
    """The cached transcript for this talk's audio, if one has been made."""
    earlier = earlier_path(tl.presenter, work, "presenter") if tl.presenter.is_file() else None
    keys = {repr(transcript_key(tl, m)) for m in (master, earlier) if m is not None}
    found = sorted(work.glob("transcript-*.json"), key=lambda p: p.stat().st_mtime)
    for path in reversed(found):  # newest first; the key is recorded next to the transcript
        meta = path.with_suffix(".key")
        if meta.exists() and meta.read_text() in keys:
            return path
    return None


def words_from_json(path: Path) -> list[Word]:
    """Words with times from whisper.cpp's full JSON (tokens joined at leading spaces).

    Repairs two whisper failures seen on real talks: a run of words all given the same instant
    (typically at the end of a 30 s window; spread over the gap that follows), and repetition loops
    in quiet stretches (a segment repeating one from the last few seconds is dropped).
    """
    data = json.loads(path.read_text())
    words: list[Word] = []
    recent: list[tuple[float, str]] = []  # (start, normalised text) of the segments kept
    for seg in data.get("transcription", []):
        seg_words = _segment_words(seg)
        if not seg_words:
            continue
        key = re.sub(r"\W+", " ", seg.get("text", "").lower()).strip()
        t = seg_words[0].start
        if key and any(k == key and abs(t - s) < REPEAT_WINDOW for s, k in recent):
            continue
        recent = [(s, k) for s, k in recent if abs(t - s) < REPEAT_WINDOW] + [(t, key)]
        words += seg_words
    for i in range(1, len(words)):  # keep times monotonic
        if words[i].start < words[i - 1].start:
            _move(words[i], words[i - 1].start)
    _spread_collapsed(words)
    for i, w in enumerate(words):  # a word lasts its own length, at most until the next one starts
        nxt = words[i + 1].start if i + 1 < len(words) else float("inf")
        w.end = min(max(w.end, w.start + MIN_WORD), w.start + MAX_WORD, nxt)
        w.end = max(w.end, w.start + 0.01)
    return [w for w in words if w.text]


REPEAT_WINDOW = 15.0  # s: whisper's repetition loops bring a segment back within seconds
MIN_WORD, MAX_WORD = 0.15, 1.0  # s: how long one word is shown as lasting
SPREAD_PACE = 0.6  # s per word at most, when spreading words whisper put on one instant


def _segment_words(seg: dict) -> list[Word]:
    words: list[Word] = []
    for tok in seg.get("tokens", []):
        text = tok.get("text", "")
        if not text or text.startswith("[_") or text.startswith("<|"):
            continue  # special tokens
        start = tok["offsets"]["from"] / 1000
        length = max(0.0, tok["offsets"]["to"] / 1000 - start)
        dtw = tok.get("t_dtw", -1)
        if dtw is not None and dtw >= 0:  # more precise word timing (in 10 ms units)
            start = dtw / 100
        if text.startswith(" ") or not words:
            words.append(Word(start, start + length, text.strip()))
        else:
            words[-1].end = max(words[-1].end, start + length)
            words[-1].text += text
    return words


def _move(w: Word, start: float) -> None:
    w.end, w.start = start + (w.end - w.start), start


def _spread_collapsed(words: list[Word]) -> None:
    """Runs of words on (nearly) the same instant: spread them over the gap until the next word."""
    i = 0
    while i < len(words):
        j = i
        while j + 1 < len(words) and words[j + 1].start - words[i].start < 0.02:
            j += 1
        n = j - i + 1
        if n > 1:
            t0 = words[i].start
            gap = words[j + 1].start - t0 if j + 1 < len(words) else n * SPREAD_PACE
            step = min(gap / n, SPREAD_PACE)
            for k in range(1, n):
                _move(words[i + k], t0 + k * step)
        i = j + 1


# ---------- mapping to output time and building cues ----------


def to_output(words: list[Word], edit: Edit) -> list[tuple[Word, bool]]:
    """Map words from presenter time into output time. Words outside the kept ranges are dropped.
    Returns (word in output time, starts a new piece) pairs; a new piece follows a cut."""
    out: list[tuple[Word, bool]] = []
    base = edit.intro
    for a, b in edit.ranges:
        lo, hi = a / FPS, b / FPS
        first = True
        for w in words:
            mid = (w.start + w.end) / 2
            if lo <= mid < hi:
                s = base + max(w.start, lo) - lo
                e = base + min(w.end, hi) - lo
                out.append((Word(s, max(e, s + 0.05), w.text), first))
                first = False
        base += hi - lo
    return out


@dataclass
class Cue:
    start: float
    end: float
    lines: list[str]


def _split_lines(text: str) -> list[str]:
    if len(text) <= MAX_LINE:
        return [text]
    words = text.split()
    best, best_cost = [text], float("inf")
    for i in range(1, len(words)):  # the most balanced split where both lines fit
        a, b = " ".join(words[:i]), " ".join(words[i:])
        if len(a) <= MAX_LINE and len(b) <= MAX_LINE:
            cost = abs(len(a) - len(b))
            if cost < best_cost:
                best, best_cost = [a, b], cost
    return best


def build_cues(words: list[tuple[Word, bool]]) -> list[Cue]:
    cues: list[Cue] = []
    cur: list[Word] = []

    def flush():
        if cur:
            text = " ".join(w.text for w in cur)
            cues.append(Cue(cur[0].start, cur[-1].end, _split_lines(text)))
            cur.clear()

    for w, new_piece in words:
        if cur:
            text = " ".join(x.text for x in cur)
            lines = _split_lines(f"{text} {w.text}")
            too_long = len(lines) > MAX_LINES or any(len(line) > MAX_LINE for line in lines)
            if (
                new_piece
                or w.start - cur[-1].end > PAUSE
                or too_long
                or w.end - cur[0].start > MAX_CUE
                or (cur[-1].text[-1:] in ".?!" and len(text) > 20)
            ):
                flush()
        cur.append(w)
    flush()
    for i, c in enumerate(cues):  # readable minimum duration, without overlapping the next cue
        nxt = cues[i + 1].start if i + 1 < len(cues) else c.end + MIN_CUE
        c.end = min(max(c.end + 0.2, c.start + MIN_CUE), nxt - 0.05) if nxt > c.start + 0.1 else c.end
    for a, b in zip(cues, cues[1:]):  # never two captions on screen at once
        if b.start < a.end:
            a.end = max(b.start, a.start + 0.3)
            b.start = a.end
            b.end = max(b.end, b.start + 0.3)
    return cues


def srt_time(t: float) -> str:
    ms = max(0, round(t * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def write_srt(cues: list[Cue], path: Path) -> None:
    blocks = [f"{i}\n{srt_time(c.start)} --> {srt_time(c.end)}\n" + "\n".join(c.lines) for i, c in enumerate(cues, 1)]
    path.write_text("\n\n".join(blocks) + "\n", encoding="utf-8")


def write_captions(transcript: Path, edit: Edit, out: Path) -> int:
    """The .srt for an export; returns the number of cues."""
    cues = build_cues(to_output(words_from_json(transcript), edit))
    write_srt(cues, out)
    return len(cues)


def deliver_srt(tl_file: Path, tl: Timeline, transcript: Path) -> tuple[Path, int]:
    """The .srt for the current edit, in output/ next to the video (without exporting again)."""
    out = deliver.files(tl_file, tl).srt
    out.parent.mkdir(exist_ok=True)
    n = write_captions(transcript, the_edit(tl, tl_file.parent / ".work"), out)
    deliver.made(tl_file, "srt", out)
    return out, n
