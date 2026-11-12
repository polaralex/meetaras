"""`meetaras new`, `make` and `status`: a talk folder from the inputs, then every step unattended.

`make` is the job runner for the CLI and the app alike: one run per talk (a lock in .work/),
progress in .work/status.json, and every step skipped when its result is already there, so a run
interrupted anywhere continues where it stopped. The app starts it as a detached process.
"""

from __future__ import annotations

import os
import shutil
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from . import cache, captions, ffmpeg, jobs, progress, timeline
from .layouts import CANVAS
from .normalize import export_masters, talk_videos
from .slides import page_count, render_slides

STEPS = ("convert", "slides", "draft", "captions", "metadata", "export")
REVIEW_STOPS_BEFORE = "captions"  # --review: stop once the draft is ready
TIMELINE = "timeline.txt"
LOG = "make.log"  # in .work/, written by detached runs


class MakeError(RuntimeError):
    pass


class Skip(Exception):
    """A step that doesn't apply to this talk; the message says why."""


def timeline_file(target: Path) -> Path:
    """A talk is given as its folder or as its timeline file."""
    target = Path(target)
    return target / TIMELINE if target.is_dir() else target


# ---------- new ----------


def auto_resolution(camera: ffmpeg.VideoInfo, screen: ffmpeg.VideoInfo | None = None) -> str:
    """The export size that matches the recordings: 4K for a camera video of at least 3840×2160, or for a
    screen recording bigger than 1080p (code and demos are where the detail is; a 1080p export throws it
    away), else 1080p."""
    if camera.width >= 3840 and camera.height >= 2160:
        return "4k"
    if screen is not None and (screen.width > CANVAS[0] or screen.height > CANVAS[1]):
        return "4k"
    return "1080p"


def new_talk(
    folder: Path, video: Path, slides: Path, screen: Path | None = None, audio: Path | None = None,
    intro: Path | None = None, outro: Path | None = None, resolution: str = "auto", **info: str | None,
) -> Path:  # fmt: skip
    """Create a talk folder with a timeline.txt holding the inputs and video info, not drafted yet.

    Videos and audio stay where they are (they are gigabytes); the PDF is copied in. The export size
    (`resolution`: auto, 1080p or 4k) is written as chosen; auto matches the recordings (`auto_resolution`).
    """
    if resolution not in ("auto", *timeline.RESOLUTIONS):
        raise MakeError(f"export size must be auto, {' or '.join(timeline.RESOLUTIONS)}, not {resolution!r}")
    folder = timeline.absolute(folder)
    out = folder / TIMELINE
    if out.exists():
        raise MakeError(f"{out} exists already; `meetaras make {folder}` continues that talk")
    inputs = {"presenter": video, "slides": slides, "screen": screen, "audio": audio, "intro": intro, "outro": outro}
    inputs = {k: timeline.absolute(p) for k, p in inputs.items() if p is not None}
    for key, p in inputs.items():
        if not p.is_file():
            raise MakeError(f"{key} file not found: {p}")
    if inputs["slides"].suffix.lower() != ".pdf":
        raise MakeError(f"{inputs['slides'].name}: slides must be a PDF (Keynote: File → Export To → PDF)")
    try:
        page_count(inputs["slides"])
    except Exception as exc:
        raise MakeError(f"cannot read {inputs['slides'].name} as a PDF: {exc}") from None
    probed: dict[str, ffmpeg.VideoInfo] = {}
    for key in ("presenter", "screen", "intro", "outro"):  # unreadable files fail now, not an hour into a run
        if key in inputs:
            v = ffmpeg.probe_video(inputs[key])
            if key == "presenter" and v.height > v.width:
                raise MakeError(f"{inputs[key].name} is portrait ({v.width}×{v.height}); the talk must be landscape")
            probed[key] = v
    if resolution == "auto":
        resolution = auto_resolution(probed["presenter"], probed.get("screen"))
    if "audio" in inputs:
        ffmpeg.probe_json(inputs["audio"])

    folder.mkdir(parents=True, exist_ok=True)
    pdf = folder / inputs["slides"].name
    if pdf != inputs["slides"]:
        shutil.copy2(inputs["slides"], pdf)  # keeps the mtime: copying again keeps the slide cache
    inputs["slides"] = pdf
    fields = {k: " ".join(v.split()) for k, v in info.items() if v and v.strip()}
    tl = timeline.Timeline(base_dir=folder, **inputs, **fields, resolution=resolution)  # type: ignore[arg-type]
    out.write_text(tl.to_text())
    return out


# ---------- status and lock ----------


class Status:
    """.work/status.json: what the current or last run did, read by `meetaras status` and the app."""

    def __init__(self, work: Path, review: bool, pid: int | None = None):
        self.path = work / "status.json"
        self.current: str | None = None
        self._written = 0.0
        self.data: dict = {
            "state": "running", "pid": pid, "review": review, "started": time.time(), "finished": None,
            "error": None, "steps": {name: {"state": "waiting", "detail": ""} for name in STEPS},
        }  # fmt: skip
        self.write()

    def write(self) -> None:
        self._written = time.monotonic()
        jobs.write_json(self.path, self.data)

    def start(self, name: str) -> None:
        self.current = name
        self.data["steps"][name] = {"state": "running", "detail": "", "started": time.time()}
        self.write()

    def end(self, state: str, detail: str = "") -> None:
        step = self.data["steps"][self.current]
        step.pop("progress", None)
        step.update(state=state, detail=detail, finished=time.time())
        self.current = None
        self.write()

    def progress(self, label: str, fraction: float) -> None:
        if self.current is None:
            return
        step = self.data["steps"][self.current]
        step["progress"], step["detail"] = round(fraction, 3), f"{label} {fraction:.0%}"
        if time.monotonic() - self._written >= 1.0:
            self.write()

    def finish(self, state: str, error: str | None = None) -> None:
        if self.current is not None:
            self.end(state, error or "")
        self.data.update(state=state, error=error, finished=time.time())
        self.write()


def _lock_path(work: Path) -> Path:
    return work / "make.lock"


@contextmanager
def lock(work: Path):
    """Hold the talk's run lock (released by the OS even if the process is killed)."""
    try:
        with jobs.lock(_lock_path(work)):
            yield
    except jobs.Busy:
        raise MakeError(f"meetaras make is already running for {work.parent} (see `meetaras status`)") from None


def running(work: Path) -> bool:
    return jobs.held(_lock_path(work))


def read_status(target: Path) -> dict | None:
    """The last run's status; `interrupted` if it says running but nothing holds the lock."""
    work = timeline_file(target).parent / ".work"
    data = jobs.read_json(work / "status.json")
    if data is None:
        return None
    if jobs.gone(data, _lock_path(work)):
        data["state"] = "interrupted"
        for step in data["steps"].values():
            if step["state"] == "running":
                step["state"] = "interrupted"
    return data


def describe(data: dict | None, folder: Path) -> str:
    """`meetaras status` text."""
    if data is None:
        return f"No run yet. Start one with `meetaras make {folder}`."
    lines = []
    for name, step in data["steps"].items():
        took = step.get("finished", 0) - step.get("started", 0) if step["state"] == "done" else 0
        lines.append(f"  {name:<9} {step['state']:<11} {step['detail']}{f' ({took / 60:.0f} min)' if took >= 60 else ''}".rstrip())
    state = data["state"]
    if state == "running":
        head = f"Running since {time.strftime('%H:%M', time.localtime(data['started']))}" + (f" (pid {data['pid']})" if data["pid"] else "")
    elif state == "done":
        head = f"Done: the video and its files are in {folder / 'output'}"
    elif state == "review":
        head = f"Draft ready: review it with `meetaras edit {folder}`, then `meetaras make {folder}` finishes."
    elif state == "failed":
        head = f"Failed: {data.get('error')}"
    else:
        head = f"Interrupted; `meetaras make {folder}` continues where it stopped."
    return "\n".join([head, *lines])


def start_detached(target: Path, review: bool = False) -> int:
    """Start `meetaras make` as its own process that outlives the caller; returns its pid."""
    tl_file = timeline_file(target)
    if not tl_file.is_file():
        raise MakeError(f"{tl_file} not found; create the talk with `meetaras new`")
    work = tl_file.parent / ".work"
    work.mkdir(exist_ok=True)
    status = read_status(tl_file)
    if status and status["state"] == "running":  # also a run that is still starting up
        raise MakeError(f"meetaras make is already running for {tl_file.parent} (see `meetaras status`)")
    # A fresh "running" status before the process exists, so whoever polls next doesn't see the last run.
    Status(work, review)
    cmd = [sys.executable, "-m", "meetaras.cli", "make", str(tl_file.parent)] + (["--review"] if review else [])
    return jobs.start(cmd, work / LOG)


# ---------- the steps ----------


@dataclass
class Run:
    tl_file: Path
    work: Path
    master: Path | None = None
    screen: Path | None = None
    review: bool = False  # stopping after the draft

    def load(self) -> timeline.Timeline:
        tl = timeline.load(self.tl_file)
        for key in timeline.PATH_KEYS:
            p = getattr(tl, key)
            if p is not None and not p.is_file():
                raise MakeError(f"{key} file not found: {p}")
        return tl


def _convert(r: Run) -> str:
    tl = r.load()
    # A 4K talk's masters in the same pass as its 1080p files; with --review only those, so the editor
    # opens sooner (the export makes the masters then).
    videos = talk_videos(tl, r.work, export=not r.review)
    r.master, r.screen = videos["presenter"], videos.get("screen")
    masters, four_k = export_masters(tl, r.work).values(), ""
    if masters:  # (a 4K talk with videos bigger than 1080p)
        four_k = ", with 4K masters for the export" if all(p.exists() for p in masters) else "; the export makes the 4K masters"
    return f"{timeline.format_time(ffmpeg.probe_duration(r.master))} of video{four_k}"


def _slides(r: Run) -> str:
    tl = r.load()
    pngs = render_slides(tl.slides, r.work)
    tl.validate_slides(len(pngs))
    return f"{len(pngs)} slides"


def draft_timeline(tl_file: Path, tl: timeline.Timeline, master: Path, screen: Path | None) -> str:
    """Draft the events (and screen_offset) from the converted videos into the timeline file, keeping
    its header; returns what was found. Also `meetaras draft`; progress goes to progress.report."""
    from . import draft

    work = tl_file.parent / ".work"
    durations = {"camera": ffmpeg.probe_duration(master)}
    if screen is not None:
        durations["screen"] = ffmpeg.probe_duration(screen)

    def on_progress(label: str, t: float) -> None:
        progress.report(label, t / durations[label])

    pngs = render_slides(tl.slides, work)
    changes, synced = draft.draft_talk(master, screen, tl.slides, pngs, durations["camera"], work, on_progress)
    # Never leave it empty: an empty timeline is what "not drafted yet" means.
    tl.events = draft.to_events(changes) or [timeline.Event(0.0, "presenter", note="no slides recognised")]
    if synced is not None:
        tl.screen_offset = synced.offset
    tmp = tl_file.with_suffix(".tmp")
    tmp.write_text(tl.to_text())
    os.replace(tmp, tl_file)
    unsure = sum(1 for e in tl.events if "CHECK" in e.note)
    detail = f"{len(changes)} changes" + (f", {unsure} marked CHECK" if unsure else "")
    if synced is not None:
        detail += f"; screen recording {synced.offset:+.2f} s ({synced.note})"
    return detail


def _draft(r: Run) -> str:
    tl = r.load()
    if tl.events:
        raise Skip(f"the timeline has {len(tl.events)} changes already")
    assert r.master is not None
    return draft_timeline(r.tl_file, tl, r.master, r.screen)


def _captions(r: Run) -> str:
    from . import macbundle

    assert r.master is not None
    if captions.available():
        captions.transcribe_talk(r.load(), r.master, r.work)
        return "transcript ready"
    mac = macbundle.results(r.tl_file)
    if mac and mac["done"] == mac["pieces"]:
        macbundle.import_results(r.tl_file)
        return "transcript from the Mac"
    if mac:
        raise Skip(f"waiting for the Mac ({mac['done']} of {mac['pieces']} pieces)")
    raise Skip("whisper.cpp not set up (meetaras doctor)")


def _metadata(r: Run) -> str:
    from . import metadata

    return metadata.generate(r.tl_file, force=False)[1]


def _export(r: Run) -> str:
    from . import deliver, metadata
    from .render import render

    youtube = metadata.load(r.tl_file)  # also yours when edited: the output folder always has the current text
    if youtube:
        deliver.write_texts(r.tl_file, metadata.to_text(youtube), metadata.to_markdown(youtube))
    tl = r.load()
    assert r.master is not None
    out = deliver.files(r.tl_file, tl).video
    transcript = captions.transcript_path(tl, r.master, r.work)
    inputs = [cache.file_fingerprint(getattr(tl, k)) for k in timeline.PATH_KEYS if getattr(tl, k) is not None]
    key = cache.key("export", r.tl_file.read_text(), inputs)
    done = r.work / "export.key"  # what the last export by make was made from
    up_to_date = done.exists() and done.read_text() == key
    if up_to_date and out.exists():
        if transcript is None:
            return f"{out.name} is up to date"
        captions.deliver_srt(r.tl_file, tl, transcript)  # captions may have arrived since: no need to re-export
        return f"{out.name} is up to date; captions written"
    render(r.tl_file, tl, out)
    deliver.made(r.tl_file, "video", out)
    if transcript:
        deliver.made(r.tl_file, "srt", out.with_suffix(".srt"))
    done.write_text(key)
    return out.name + (" and its captions" if transcript else "")


STEP_FUNCTIONS = {"convert": _convert, "slides": _slides, "draft": _draft, "captions": _captions, "metadata": _metadata, "export": _export}


def make(target: Path, review: bool = False) -> Path:
    """Run every step that isn't done yet (stopping after the draft with `review`); returns the timeline."""
    tl_file = timeline_file(target)
    if not tl_file.is_file():
        raise MakeError(f"{tl_file} not found; create the talk with `meetaras new`")
    work = tl_file.parent / ".work"
    work.mkdir(exist_ok=True)
    with lock(work):
        status = Status(work, review, os.getpid())
        run = Run(tl_file, work, review=review)
        progress.hook = status.progress
        try:
            for name in STEPS:
                if review and name == REVIEW_STOPS_BEFORE:
                    break
                print(f"== {name}", flush=True)
                status.start(name)
                try:
                    detail, state = STEP_FUNCTIONS[name](run), "done"
                except Skip as skip:
                    detail, state = str(skip), "skipped"
                status.end(state, detail)
                print(f"   {state}: {detail}", flush=True)
            status.finish("review" if review else "done")
        except KeyboardInterrupt:
            status.finish("interrupted", "stopped")
            raise
        except BaseException as exc:
            status.finish("failed", str(exc).splitlines()[0] if str(exc) else type(exc).__name__)
            raise
        finally:
            progress.hook = None
    return tl_file
