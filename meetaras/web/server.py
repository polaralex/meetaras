"""Local web app: the talk library (home, New talk with a file picker) and an editor page per talk.

The editor only reads and writes timeline.txt and serves media; rendering always goes through the
CLI, so the UI can never break the render path: Make video starts the same detached `meetaras make`
the CLI uses and polls its status file, Export preview runs `meetaras render --preview`.

Routers: library.py (home, Setup, file picker, new/open), jobs.py (Make video, Export preview,
captions, YouTube, open folder), editor.py (state, timeline, media); common.py has what they share.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from .common import STATIC, AppState, prepare, talk_url
from .editor import router as editor_router
from .jobs import router as jobs_router
from .library import router as library_router

__all__ = ["LIBRARY", "create_app", "prepare", "talk_url"]

LIBRARY = Path(os.environ.get("MEETARAS_LIBRARY", "~/talks")).expanduser()
LOCAL_HOSTS = ("127.0.0.1", "localhost")
DATA_PATHS = re.compile(r"^(/talk/[^/]+)?/(api|media)(/|$)")  # what other sites must not read, even by GET


def denied(request: Request) -> bool:
    """Whether a request comes from somewhere other than this app's own pages on this machine.

    DNS rebinding shows up in the Host header, cross-site POSTs in Origin (compared with the full
    host:port served, as another local app on another port is another site too). Browsers send no
    Origin with a GET, but they do send Sec-Fetch-Site: only links to pages may come from elsewhere.
    """
    host = request.headers.get("host", "")
    if urlsplit(f"//{host}").hostname not in LOCAL_HOSTS:
        return True
    origin = request.headers.get("origin")
    if origin and origin.lower() != f"{request.url.scheme}://{host}".lower():
        return True
    return request.headers.get("sec-fetch-site") in ("cross-site", "same-site") and bool(DATA_PATHS.match(request.url.path))


def create_app(library: Path = LIBRARY) -> FastAPI:
    app = FastAPI(title="meetaras", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.meetaras = AppState(library)
    app.state.library = app.state.meetaras.lib  # `meetaras edit` adds its talk before serving

    @app.middleware("http")
    async def local_only(request: Request, call_next):
        if denied(request):
            return JSONResponse({"detail": "meetaras only answers its own pages on this machine"}, status_code=403)
        return await call_next(request)

    for router in (library_router, jobs_router, editor_router):
        app.include_router(router)
    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app
