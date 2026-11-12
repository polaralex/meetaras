"""Helpers for the app tests (test_web, test_ui): a prepared talk to copy, the app on a free port."""

from __future__ import annotations

import contextlib
import shutil
import socket
import threading
import time
from pathlib import Path

import uvicorn
from fastapi.testclient import TestClient

from meetaras.web.common import prepare
from meetaras.web.server import create_app

BASE = "http://127.0.0.1:8770"  # what TestClient pretends to be (it opens no port)
_prepared: dict[Path, Path] = {}


def app_client(library: Path) -> TestClient:
    return TestClient(create_app(library), base_url=BASE)


def prepared_talk(tmp_path_factory, fixtures: Path) -> Path:
    """A talk folder (talk.mp4 and deck.pdf, linked) with the converted video and slides in .work/, as a
    make run leaves it. Made once per session: converting takes seconds."""
    if fixtures not in _prepared:
        d = tmp_path_factory.mktemp("prepared") / "talk"
        d.mkdir()
        for name in ("talk.mp4", "deck.pdf"):
            (d / name).symlink_to(fixtures / name)
        (d / "timeline.txt").write_text("presenter talk.mp4\nslides deck.pdf\n\n0:02 slide 1\n")
        prepare(d / "timeline.txt")
        _prepared[fixtures] = d
    return _prepared[fixtures]


def talk_folder(library: Path, prepared: Path, timeline: str, name: str = "talk", extra: tuple[Path, ...] = ()) -> Path:
    """A copy of the prepared talk with this timeline (and `extra` files linked in); returns its timeline.txt.

    The caches come along: they are keyed on file name, size and mtime, which copytree keeps.
    """
    d = library / name
    shutil.copytree(prepared, d, symlinks=True)
    for f in extra:
        (d / f.name).symlink_to(f)
    (d / "timeline.txt").write_text(timeline)
    if extra:
        prepare(d / "timeline.txt")  # converts them, as a make run would have
    return d / "timeline.txt"


@contextlib.contextmanager
def serve(library: Path):
    """The app on a free port, in a thread; yields its base URL."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(create_app(library), host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.05)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join()
