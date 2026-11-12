"""The editor page of a talk: its state (geometry from layouts.py, so preview == export), saving
timeline.txt, and the media the preview plays."""

from __future__ import annotations

import os

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import BaseModel

from .. import labels, layouts, make, timeline
from .common import STATIC, Folder, Loaded, Project, State, final_video, talk_url

router = APIRouter(prefix="/talk/{tid}")


def fractions(x: float, y: float, w: float, h: float) -> dict:
    """A rectangle in canvas pixels as fractions of the canvas, as the browser places things."""
    cw, ch = layouts.CANVAS
    return {"x": x / cw, "y": y / ch, "w": w / cw, "h": h / ch}


def box_json(b: layouts.Box) -> dict:
    return fractions(b.x, b.y, b.w, b.h)


def overlays_json(p: Project) -> dict:
    return {
        name: {"url": f"media/label/{ov.path.name}", "box": box_json(ov.box)}
        for name, ov in labels.render_overlays(p.tl.info, p.path.parent / ".work").items()
    }


def crop_json(p: Project) -> dict | None:
    """The screen recording's 16:9 core as fractions of the canvas (insets show only that)."""
    from ..render import screen_crop

    if p.screen is None or p.tl.screen is None:
        return None
    crop = screen_crop(p.tl.screen)
    if crop is None:
        return None
    cw, ch, cx, cy = crop
    return fractions(cx, cy, cw, ch)


def timeline_json(p: Project) -> dict:
    tl = p.tl
    settings = {"lower_third_every": tl.lower_third_every, "compression": tl.compression, "resolution": tl.resolution}
    return {
        "info": {**{k: getattr(tl, k) for k in timeline.INFO_KEYS}, **settings},
        "overlays": overlays_json(p),
        "events": [
            {"time": e.time, "kind": e.kind, "slide": e.slide, "style": e.style, "note": e.note} for e in tl.events
        ],
        "screen_offset": tl.screen_offset,
        "mtime": str(p.mtime),  # nanoseconds: too large for a JS number, so passed as text
    }


class EventIn(BaseModel):
    time: float
    kind: str
    slide: int | None = None
    style: str = ""
    note: str = ""


class InfoIn(BaseModel):
    event: str = ""
    date: str = ""
    speaker: str = ""
    role: str = ""
    talk: str = ""
    lower_third_every: float = 0.0
    compression: bool = True
    resolution: str = "1080p"


class TimelineIn(BaseModel):
    events: list[EventIn]
    screen_offset: float = 0.0
    info: InfoIn | None = None
    base_mtime: str  # the file version the browser edited; a mismatch means someone else changed it


@router.get("")
def talk_redirect(tid: str) -> RedirectResponse:
    return RedirectResponse(talk_url(tid))


@router.get("/")
def talk_page(folder: Folder) -> FileResponse:
    return FileResponse(STATIC / "editor.html")


@router.get("/api/state")
def state(tid: str, s: State) -> dict:
    p = s.load(tid)
    w = layouts.CANVAS[0]
    return {
        "name": p.path.parent.name,
        "timeline_file": str(p.path),
        "duration": p.duration,
        "slides": len(p.slide_pngs),
        "has_screen": p.screen is not None,
        "screen_duration": p.screen_duration,
        "external_audio": p.tl.audio is not None,
        "camera": p.camera,
        "screen_size": p.screen_size,
        "bumpers": {name: {"file": getattr(p.tl, name).name, "duration": d} for name, (_, d) in p.bumpers.items()},
        "layout": {
            "styles": {
                name: {"content": box_json(c), "pip": box_json(pip), "inset": inset}
                for name, (c, pip, inset) in layouts.STYLES.items()
            },
            "inset": {
                "radius": layouts.INSET_RADIUS / w,
                "shadow": [layouts.SHADOW_OFFSET / w, layouts.SHADOW_SIGMA * 2 / w, layouts.SHADOW_OPACITY],
            },
            "screen_crop": crop_json(p),
            "lower_third": {"in": labels.LT_IN, "fade": labels.LT_FADE, "hold": labels.LT_HOLD},
        },
        "timeline": timeline_json(p),
    }


@router.put("/api/timeline")
def save(tid: str, p: Loaded, body: TimelineIn, s: State) -> dict:
    with s.saving[tid]:
        disk = p.path.stat().st_mtime_ns
        if disk != p.mtime or body.base_mtime != str(p.mtime):
            raise HTTPException(412, "timeline.txt was changed outside the editor; reload to continue")
        events = []
        for e in sorted(body.events, key=lambda e: e.time):
            if e.kind not in ("start", "end", "presenter", "slide", "screen", "cut") or e.style not in timeline.STYLES:
                raise HTTPException(422, f"unknown change {e.kind!r} {e.style!r}")
            events.append(timeline.Event(round(e.time, 3), e.kind, e.slide, e.style, note=e.note))  # type: ignore[arg-type]
        info = body.info.model_dump() if body.info else {}
        info = {
            k: v if isinstance(v, bool) else " ".join(v.split()) if isinstance(v, str) else max(0.0, float(v))
            for k, v in info.items()
        }
        candidate = timeline.Timeline(**{**p.tl.__dict__, **info, "events": events, "screen_offset": body.screen_offset})
        text = candidate.to_text()
        try:  # validate exactly what the CLI will read
            parsed = timeline.parse(text, p.path.parent)
            parsed.validate_slides(len(p.slide_pngs))
            parsed.segments(p.duration)
        except timeline.TimelineError as exc:
            raise HTTPException(422, str(exc)) from None
        backup = p.path.with_suffix(p.path.suffix + ".orig")
        if not backup.exists():  # the hand-written original, comments and all
            backup.write_text(p.path.read_text())
        tmp = p.path.with_suffix(".tmp")
        tmp.write_text(text)
        os.replace(tmp, p.path)
        p.tl, p.mtime = parsed, p.path.stat().st_mtime_ns
        return {"mtime": str(p.mtime), "overlays": overlays_json(p)}


@router.post("/api/reload")
def reload(tid: str, p: Loaded, s: State) -> dict:
    with s.saving[tid]:
        try:
            p.tl = timeline.load(p.path)
        except timeline.TimelineError as exc:
            raise HTTPException(422, str(exc)) from None
        p.mtime = p.path.stat().st_mtime_ns
        return timeline_json(p)


# ---------- media ----------


@router.get("/media/presenter.mp4")
def presenter(p: Loaded) -> FileResponse:
    return FileResponse(p.master, media_type="video/mp4")


@router.get("/media/screen.mp4")
def screen(p: Loaded) -> FileResponse:
    if p.screen is None:
        raise HTTPException(404)
    return FileResponse(p.screen, media_type="video/mp4")


@router.get("/media/label/{name}")
def label(folder: Folder, name: str) -> FileResponse:
    f = folder / ".work" / "labels" / name
    if "/" in name or not name.endswith(".png") or not f.is_file():
        raise HTTPException(404)
    return FileResponse(f, media_type="image/png")


@router.get("/media/slide/{n}.png")
def slide(p: Loaded, n: int) -> FileResponse:
    if not 1 <= n <= len(p.slide_pngs):
        raise HTTPException(404)
    return FileResponse(p.slide_pngs[n - 1], media_type="image/png")


@router.get("/media/output/{name}")
def output(folder: Folder, name: str) -> FileResponse:
    if name not in ("final.mp4", "output-preview.mp4"):
        raise HTTPException(404)
    f = final_video(folder / make.TIMELINE) if name == "final.mp4" else folder / name
    if f is None or not f.is_file():
        raise HTTPException(404)
    return FileResponse(f, media_type="video/mp4")


@router.get("/media/{name}.mp4")
def bumper(p: Loaded, name: str) -> FileResponse:
    if name not in p.bumpers:
        raise HTTPException(404)
    return FileResponse(p.bumpers[name][0], media_type="video/mp4")
