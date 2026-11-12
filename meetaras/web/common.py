"""What the app's routers share: the talk library, the prepared talks, background jobs (on app.state)."""

from __future__ import annotations

import json
import subprocess
import threading
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated
from urllib.parse import quote

from fastapi import Depends, HTTPException, Request

from .. import deliver, ffmpeg, make, timeline
from ..normalize import talk_videos
from ..slides import render_slides

STATIC = Path(__file__).parent / "static"
OPENED = ".opened.json"  # in the library folder: talks opened from elsewhere, {id: folder}


@dataclass
class Project:
    path: Path  # timeline.txt
    tl: timeline.Timeline
    mtime: int
    master: Path
    duration: float
    slide_pngs: list[Path]
    screen: Path | None = None
    screen_duration: float = 0.0
    bumpers: dict[str, tuple[Path, float]] = field(default_factory=dict)  # intro/outro: (converted file, seconds)
    camera: tuple[int, int] | None = None  # the camera video's size (after rotation), for the Export size hint
    screen_size: tuple[int, int] | None = None  # the screen recording's, the same


def prepare(path: Path) -> Project:
    """Load the timeline and make sure the media the browser needs exists (cached in .work/)."""
    tl = timeline.load(path)
    work = path.parent / ".work"
    videos = talk_videos(tl, work)
    master = videos["presenter"]
    pngs = render_slides(tl.slides, work)
    project = Project(path, tl, path.stat().st_mtime_ns, master, ffmpeg.probe_duration(master), pngs)
    if "screen" in videos:
        project.screen = videos["screen"]
        project.screen_duration = ffmpeg.probe_duration(project.screen)
    for name in ("intro", "outro"):
        if name in videos:
            project.bumpers[name] = (videos[name], ffmpeg.probe_duration(videos[name]))
    camera = ffmpeg.probe_video(tl.presenter)
    project.camera = (camera.width, camera.height)
    if tl.screen is not None:
        screen = ffmpeg.probe_video(tl.screen)
        project.screen_size = (screen.width, screen.height)
    return project


def not_ready(tl_file: Path) -> str | None:
    """Why the editor can't open this talk yet, or None: it needs the draft and the converted videos."""
    try:
        tl = timeline.load(tl_file)
    except timeline.TimelineError as exc:
        return str(exc)
    status = make.read_status(tl_file)
    if status and status["state"] == "running" and any(
        status["steps"][s]["state"] in ("waiting", "running") for s in ("convert", "slides", "draft")
    ):
        return "Make video is preparing the draft. The editor opens when it is ready."
    for key in timeline.PATH_KEYS:
        p = getattr(tl, key)
        if p is not None and not p.is_file():
            return f"{key} file not found: {p}"
    if not tl.events:
        return "Not drafted yet: Make video converts the videos and drafts the edit."
    work = tl_file.parent / ".work"
    if not all(p.exists() for p in talk_videos(tl, work, convert=False).values()):
        return "The videos aren't converted yet: Make video converts them."
    return None


def final_video(tl_file: Path) -> Path | None:
    """The finished video in output/, if there is one."""
    try:
        video = deliver.files(tl_file).video
    except timeline.TimelineError:
        return None
    return video if video.is_file() else None


def talk_url(tid: str) -> str:
    return f"/talk/{quote(tid)}/"


class Library:
    """The talks: folders with a timeline.txt in the library folder, plus any opened from elsewhere
    (remembered in the library folder, so their pages still work after a restart)."""

    def __init__(self, root: Path):
        self.root = timeline.absolute(root)
        self.lock = threading.Lock()
        try:
            self.opened = {tid: Path(p) for tid, p in json.loads((self.root / OPENED).read_text()).items()}
        except (OSError, ValueError, AttributeError):
            self.opened = {}

    def _save(self) -> None:
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            (self.root / OPENED).write_text(json.dumps({tid: str(p) for tid, p in self.opened.items()}, indent=1))
        except OSError:
            pass  # remembered until the app stops, then

    def talks(self) -> dict[str, Path]:
        """id → folder. Opened talks that are gone (moved, deleted, drive not mounted) are left out; one
        whose id a new library folder took gets another id."""
        found = {}
        if self.root.is_dir():
            for d in sorted(self.root.iterdir()):
                if not d.name.startswith(".") and (d / make.TIMELINE).is_file():
                    found[d.name] = d
        with self.lock:
            renamed = False
            for tid, d in list(self.opened.items()):
                if d in found.values() or not (d / make.TIMELINE).is_file():
                    continue
                if tid in found:
                    del self.opened[tid]
                    tid = self._new_id(d, found)
                    self.opened[tid] = d
                    renamed = True
                found[tid] = d
            if renamed:
                self._save()
        return found

    def _new_id(self, folder: Path, taken: dict[str, Path]) -> str:
        tid, n = folder.name, 2
        while tid in taken or tid in self.opened:
            tid, n = f"{folder.name}-{n}", n + 1
        return tid

    def add(self, folder: Path) -> str:
        """The talk's id (its folder name, made unique), adding it if it is outside the library."""
        folder = timeline.absolute(folder)
        talks = self.talks()
        for tid, d in talks.items():
            if d == folder:
                return tid
        with self.lock:
            tid = self._new_id(folder, talks)
            self.opened[tid] = folder
            self._save()
        return tid

    def folder(self, tid: str) -> Path:
        d = self.talks().get(tid)
        if d is None:
            raise HTTPException(404, f"no talk {tid!r}")
        return d


@dataclass
class Export:  # Export preview (the final export is a `meetaras make` run)
    proc: subprocess.Popen | None = None
    lines: list[str] = field(default_factory=list)
    output: Path | None = None

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None


@dataclass
class Job:  # work in a thread of the app: writing a folder for the Mac, YouTube metadata
    thread: threading.Thread | None = None
    error: str | None = None

    @property
    def running(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    def start(self, fn: Callable[[], object]) -> None:
        def run() -> None:
            try:
                fn()
            except Exception as exc:  # shown on the page
                self.error = str(exc)

        self.error = None
        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()


class AppState:
    def __init__(self, library: Path):
        self.lib = Library(library)
        self.projects: dict[str, Project] = {}  # prepared when a talk page loads (or a request needs one)
        self.preparing: dict[str, threading.Lock] = defaultdict(threading.Lock)
        self.saving: dict[str, threading.Lock] = defaultdict(threading.Lock)
        self.exports: dict[str, Export] = defaultdict(Export)
        self.mac_jobs: dict[str, Job] = defaultdict(Job)
        self.youtube_jobs: dict[str, Job] = defaultdict(Job)

    def _prepare(self, tid: str) -> Project:
        tl_file = self.lib.folder(tid) / make.TIMELINE
        reason = not_ready(tl_file)
        if reason:
            raise HTTPException(409, reason)
        try:
            p = self.projects[tid] = prepare(tl_file)
        except (timeline.TimelineError, ffmpeg.FFmpegError) as exc:
            raise HTTPException(422, str(exc)) from None
        return p

    def load(self, tid: str) -> Project:
        """Prepare the talk afresh (the editor page does on load)."""
        with self.preparing[tid]:
            return self._prepare(tid)

    def project(self, tid: str) -> Project:
        """The prepared talk; prepared now if the app has restarted since its page loaded."""
        with self.preparing[tid]:
            p = self.projects.get(tid)
            return p if p is not None else self._prepare(tid)


def app_state(request: Request) -> AppState:
    return request.app.state.meetaras


def talk_folder(tid: str, s: Annotated[AppState, Depends(app_state)]) -> Path:
    return s.lib.folder(tid)


def talk_project(tid: str, s: Annotated[AppState, Depends(app_state)]) -> Project:
    return s.project(tid)


State = Annotated[AppState, Depends(app_state)]
Folder = Annotated[Path, Depends(talk_folder)]
Loaded = Annotated[Project, Depends(talk_project)]
