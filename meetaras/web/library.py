"""The home page (the talks, New talk with a file picker served by Meetaras, opening a talk from
elsewhere) and Setup."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from .. import ffmpeg, make, timeline, tools
from .common import STATIC, State, final_video, talk_url

router = APIRouter()

FILE_KINDS = {
    "video": {".mov", ".mp4", ".m4v", ".mkv", ".avi", ".webm", ".mts"},
    "pdf": {".pdf"},
    "audio": {".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg", ".mov", ".mp4"},
}


def talk_json(tid: str, folder: Path) -> dict:
    tl_file = folder / make.TIMELINE
    video = final_video(tl_file)
    out: dict = {"id": tid, "folder": str(folder), "url": talk_url(tid), "output": video is not None}
    try:
        tl = timeline.load(tl_file)
        out.update({k: getattr(tl, k) for k in timeline.INFO_KEYS})
    except timeline.TimelineError as exc:
        out["error"] = str(exc)
    out["status"] = make.read_status(folder)
    times = [p.stat().st_mtime for p in (tl_file, folder / ".work" / "status.json", video) if p and p.exists()]
    out["updated"] = max(times, default=0)  # (the folder can go while this looks)
    return out


def places(library: Path) -> list[dict]:
    """Shortcuts for the file picker: home, the library, shared and removable drives."""
    home = Path.home()
    out = [{"label": "Home", "path": str(home)}, {"label": "Talks", "path": str(library)}]
    for name in ("Desktop", "Downloads", "Movies", "Videos"):
        if (home / name).is_dir():
            out.append({"label": name, "path": str(home / name)})
    for parent in (Path("/mnt/hgfs"), Path("/Volumes"), Path("/media") / home.name):  # VMware, macOS, Linux
        try:
            out += [{"label": d.name, "path": str(d)} for d in sorted(parent.iterdir()) if d.is_dir()]
        except OSError:
            pass
    return out


class OpenIn(BaseModel):
    path: str


class NewTalkIn(BaseModel):
    name: str  # folder in the library
    video: str
    slides: str
    screen: str = ""
    audio: str = ""
    intro: str = ""
    outro: str = ""
    event: str = ""
    date: str = ""
    speaker: str = ""
    role: str = ""
    talk: str = ""
    language: str = ""
    resolution: str = "auto"  # export size: auto (match the recordings), 1080p or 4k
    make: bool = True  # start Make video right away
    review: bool = False


@router.get("/")
def home() -> FileResponse:
    return FileResponse(STATIC / "home.html")


@router.get("/api/app")
def about(s: State) -> dict:
    return {"app": "meetaras", "library": str(s.lib.root)}


@router.post("/api/open")
def open_talk(body: OpenIn, s: State) -> dict:
    tl_file = make.timeline_file(Path(body.path))
    if not tl_file.is_file():
        raise HTTPException(404, f"{tl_file} not found")
    return {"url": talk_url(s.lib.add(tl_file.parent))}


@router.get("/api/talks")
def talks(s: State) -> dict:
    out = [talk_json(tid, d) for tid, d in s.lib.talks().items()]
    return {"library": str(s.lib.root), "talks": sorted(out, key=lambda t: -t["updated"])}


@router.post("/api/talks")
def new_talk(body: NewTalkIn, s: State) -> dict:
    name = body.name.strip()
    if not name or name.startswith(".") or "/" in name or "\\" in name:
        raise HTTPException(422, "Give the talk a folder name (no slashes)")

    def path(text: str) -> Path | None:
        return Path(text.strip()).expanduser() if text.strip() else None

    if not body.video.strip() or not body.slides.strip():
        raise HTTPException(422, "The camera video and the slides PDF are needed")
    folder = s.lib.root / name
    try:
        make.new_talk(
            folder, path(body.video), path(body.slides), path(body.screen), path(body.audio), path(body.intro),
            path(body.outro), body.resolution, event=body.event, date=body.date, speaker=body.speaker,
            role=body.role, talk=body.talk, language=body.language,
        )  # fmt: skip
    except (make.MakeError, ffmpeg.FFmpegError) as exc:
        raise HTTPException(422, str(exc)) from None
    tid = s.lib.add(folder)
    if body.make:
        make.start_detached(folder, body.review)
    return {"id": tid, "url": talk_url(tid)}


@router.get("/setup")
def setup_page() -> FileResponse:
    return FileResponse(STATIC / "setup.html")


@router.get("/api/setup")
def setup(s: State) -> dict:
    return {
        "checks": tools.checks_json(s.lib.root), "job": tools.setup_status(), "log": tools.setup_log(),
        "data": str(tools.data_dir()), "gpu": tools.gpu(),
    }  # fmt: skip


@router.post("/api/setup/captions")
def set_up_captions() -> dict:
    try:
        tools.start_setup()
    except tools.SetupError as exc:
        raise HTTPException(409, str(exc)) from None
    return {"started": True}


@router.get("/api/files")
def files(s: State, path: str = "", kind: str = "video") -> dict:
    d = timeline.absolute(Path(path).expanduser() if path.strip() else Path.home())
    if d.is_file():
        d = d.parent
    if not d.is_dir():
        raise HTTPException(404, f"{d} is not a folder")
    exts = FILE_KINDS.get(kind)
    dirs, found = [], []
    try:
        entries = sorted(d.iterdir(), key=lambda e: e.name.lower())
    except OSError as exc:
        raise HTTPException(403, f"can't open {d}: {exc.strerror}") from None
    for e in entries:
        if e.name.startswith("."):
            continue
        try:
            if e.is_dir():
                dirs.append(e.name)
            elif exts is None or e.suffix.lower() in exts:
                found.append({"name": e.name, "size": e.stat().st_size})
        except OSError:
            continue
    parent = str(d.parent) if d.parent != d else None
    return {"path": str(d), "parent": parent, "dirs": dirs, "files": found, "places": places(s.lib.root)}
