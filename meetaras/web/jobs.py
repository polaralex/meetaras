"""A talk's work outside the editor: Make video (a detached `meetaras make`), Export preview, captions
(Transcribe on a Mac), the YouTube text, and showing the finished files."""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import threading
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .. import captions, deliver, ffmpeg, macbundle, make, metadata, timeline
from .common import Export, Folder, Loaded, State, final_video, not_ready

router = APIRouter(prefix="/talk/{tid}")

PROGRESS_LINE = re.compile(r"^\s+[\w ]+: \d+%$")  # "  normalize: 45%": noise in a log shown to people
FILE_MANAGERS = ("nautilus", "dolphin", "thunar", "nemo", "caja", "pcmanfm")  # the system default may be another app


def open_folder_command(folder: Path) -> list[str] | None:
    """How to show a folder in this computer's file manager (Finder on a Mac), or None."""
    if sys.platform == "darwin":
        return ["open", str(folder)]
    found = next((shutil.which(f) for f in FILE_MANAGERS if shutil.which(f)), None) or shutil.which("xdg-open")
    return [found, str(folder)] if found else None


class MakeIn(BaseModel):
    review: bool = False


class MacIn(BaseModel):
    folder: str


class YouTubeIn(BaseModel):
    title: str
    description: str
    tags: list[str]
    chapters: str  # "m:ss Name" per line, as in the description


class RegenerateIn(BaseModel):
    ai: bool = True


# ---------- Make video ----------


@router.get("/api/make")
def make_status(folder: Folder) -> dict:
    log = folder / ".work" / make.LOG
    lines = log.read_text(errors="replace").splitlines() if log.exists() else []
    reason = not_ready(folder / make.TIMELINE)
    return {
        "name": folder.name,
        "status": make.read_status(folder),
        "ready": reason is None,
        "reason": reason,
        "log": [line for line in lines if not PROGRESS_LINE.match(line)][-40:],
        "output": final_video(folder / make.TIMELINE) is not None,
        "whisper": captions.available(),
    }


@router.post("/api/make")
def start_make(tid: str, folder: Folder, body: MakeIn, s: State) -> dict:
    if s.exports[tid].running:
        raise HTTPException(409, "a preview export is running; Make video can start when it has finished")
    try:
        return {"pid": make.start_detached(folder, body.review)}
    except make.MakeError as exc:
        raise HTTPException(409, str(exc)) from None


# ---------- Export preview ----------


@router.post("/api/export")
def export(tid: str, p: Loaded, s: State) -> dict:
    if s.exports[tid].running:
        raise HTTPException(409, "an export is already running")
    if make.running(p.path.parent / ".work"):
        raise HTTPException(409, "Make video is running; preview when it has finished")
    out = p.path.parent / "output-preview.mp4"
    cmd = [sys.executable, "-m", "meetaras.cli", "render", str(p.path), "-o", str(out), "--preview"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    ex = s.exports[tid] = Export(proc, [], out)

    def collect() -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            ex.lines.append(line.rstrip())
        proc.wait()

    threading.Thread(target=collect, daemon=True).start()
    return {"started": True}


@router.get("/api/export")
def export_status(tid: str, s: State) -> dict:
    ex = s.exports[tid]
    if ex.proc is None:
        return {"state": "idle"}
    code = ex.proc.poll()
    state = "running" if code is None else ("done" if code == 0 else "failed")
    return {"state": state, "lines": ex.lines[-30:], "output": ex.output.name if ex.output else None}


# ---------- captions ----------


@router.get("/api/captions")
def captions_status(tid: str, folder: Folder, s: State) -> dict:
    tl_file = folder / make.TIMELINE
    p = s.projects.get(tid)
    job = s.mac_jobs[tid]
    video = final_video(tl_file)
    return {
        "transcript": p is not None and captions.transcript_path(p.tl, p.master, folder / ".work") is not None,
        "srt": video is not None and video.with_suffix(".srt").is_file(),
        "whisper": captions.available(),
        "mac": macbundle.results(tl_file),
        "writing": job.running,
        "error": job.error,
        "default_folder": str(macbundle.default_folder()),
        "instructions": macbundle.INSTRUCTIONS,
    }


@router.post("/api/captions/mac")
def captions_mac(tid: str, folder: Folder, body: MacIn, s: State) -> dict:
    job = s.mac_jobs[tid]
    if job.running:
        raise HTTPException(409, "the folder for the Mac is being written")
    target = Path(body.folder.strip()).expanduser()
    if not body.folder.strip() or not target.parent.is_dir():
        raise HTTPException(422, f"{target.parent} is not a folder")
    job.start(lambda: macbundle.write_bundle(folder / make.TIMELINE, target))
    return {"started": True}


@router.post("/api/captions/import")
def captions_import(p: Loaded) -> dict:
    try:
        transcript = macbundle.import_results(p.path)
        _, n = captions.deliver_srt(p.path, p.tl, transcript)
    except (macbundle.MacBundleError, captions.CaptionError, timeline.TimelineError) as exc:
        raise HTTPException(409, str(exc)) from None
    return {"captions": n}


# ---------- YouTube ----------


@router.get("/api/youtube")
def youtube(tid: str, folder: Folder, s: State) -> dict:
    tl_file = folder / make.TIMELINE
    job = s.youtube_jobs[tid]
    data = metadata.load(tl_file)
    stale = False
    if data and not job.running and not_ready(tl_file) is None:  # (never converts anything here)
        try:  # the edit, transcript or slides changed since: chapter times may be off
            stale = metadata.inputs(tl_file)[4] != data.get("made_from")
        except (timeline.TimelineError, ffmpeg.FFmpegError, OSError):
            pass
    return {
        "data": data, "stale": stale, "running": job.running, "error": job.error,
        "claude": metadata.claude_available(),
        "description_with_chapters": metadata.description_with_chapters(data) if data else "",
        "markdown": metadata.to_markdown(data) if data else "",
    }  # fmt: skip


@router.put("/api/youtube")
def save_youtube(folder: Folder, body: YouTubeIn) -> dict:
    tl_file = folder / make.TIMELINE
    data = metadata.load(tl_file)
    if data is None:
        raise HTTPException(409, "nothing generated yet")
    chapters = []
    for n, line in enumerate(body.chapters.splitlines(), 1):
        if not line.strip():
            continue
        time_text, _, title = line.strip().partition(" ")
        try:
            chapters.append({"time": timeline.parse_time(time_text), "title": title.strip()})
        except ValueError:
            raise HTTPException(422, f"chapter line {n}: start it with a time, e.g. 1:23 Title") from None
    title = " ".join(body.title.split())
    titles = [title] + [t for t in data["titles"] if t != title]
    tags = [t.strip() for t in body.tags if t.strip()]
    data.update(title=title, titles=titles, description=body.description.strip(), tags=tags, chapters=chapters, edited=True)
    metadata.save(tl_file, data)
    return {"description_with_chapters": metadata.description_with_chapters(data), "markdown": metadata.to_markdown(data)}


@router.post("/api/youtube/regenerate")
def regenerate_youtube(tid: str, folder: Folder, body: RegenerateIn, s: State) -> dict:
    job = s.youtube_jobs[tid]
    if job.running:
        raise HTTPException(409, "already writing")
    job.start(lambda: metadata.generate(folder / make.TIMELINE, ai=body.ai))
    return {"started": True}


# ---------- the finished files ----------


@router.post("/api/open-folder")
def open_folder(folder: Folder) -> dict:
    if (folder / deliver.OUTPUT_DIR).is_dir():
        folder = folder / deliver.OUTPUT_DIR  # the finished files
    cmd = open_folder_command(folder)
    if cmd is None:
        raise HTTPException(501, f"no file manager found; the talk is in {folder}")
    subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    return {"folder": str(folder)}
