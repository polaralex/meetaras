"""Synthetic fixtures generated with ffmpeg/numpy/PyMuPDF (nothing binary is committed)."""

from __future__ import annotations

import hashlib
import http.server
import os
import shutil
import signal
import subprocess
import threading
import time
from pathlib import Path

import cv2
import numpy as np
import pymupdf
import pytest
from scipy.io import wavfile

SR = 48000
DURATION = 12.0
EXT_OFFSET = 2.5  # external recorder started 2.5 s before the camera
EXT_DRIFT_PPM = 500.0  # exaggerated so it is measurable in a 12 s clip


TOOLS_ENV = ("MEETARAS_DATA", "MEETARAS_WHISPER_DIR", "MEETARAS_WHISPER_MODEL")
REAL_ENV = {k: os.environ.get(k) for k in TOOLS_ENV}  # this machine's, before any test changes them


@pytest.fixture(autouse=True)
def no_claude(monkeypatch):
    """Tests never call the real Claude (also not from the make runs they start)."""
    monkeypatch.setenv("MEETARAS_CLAUDE", "0")


@pytest.fixture(autouse=True)
def no_real_tools(monkeypatch, tmp_path_factory):
    """Every machine runs the same path: an empty data folder, no whisper.cpp (not even on PATH: an
    explicit MEETARAS_WHISPER_DIR is the only place looked at), no folder shared with a Mac. Set in the
    environment, so the make runs the tests start see it too. `real_whisper` undoes it."""
    from meetaras import macbundle

    data = tmp_path_factory.mktemp("meetaras-data")
    monkeypatch.setenv("MEETARAS_DATA", str(data))
    monkeypatch.setenv("MEETARAS_WHISPER_DIR", str(data / "no-whisper"))
    monkeypatch.delenv("MEETARAS_WHISPER_MODEL", raising=False)
    monkeypatch.setattr(macbundle, "SHARED_FOLDERS", ())


@pytest.fixture
def real_whisper(monkeypatch) -> tuple[Path, Path]:
    """This machine's whisper.cpp and model, as Meetaras finds them outside the tests (MEETARAS_WHISPER_DIR /
    _MODEL, the data folder, PATH); skips the test without them. Tests using it get the `whisper` mark.
    Ask for it before fixtures that replace tools.find_whisper (tools_here)."""
    from meetaras import tools

    for key, value in REAL_ENV.items():
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)
    cli, model = tools.find_whisper()
    if cli is None or model is None:
        pytest.skip("needs whisper.cpp and its model (meetaras doctor --fix, or MEETARAS_WHISPER_DIR)")
    return cli, model


def pytest_collection_modifyitems(items):
    for item in items:
        if "real_whisper" in getattr(item, "fixturenames", ()):
            item.add_marker(pytest.mark.whisper)


@pytest.fixture(autouse=True)
def stop_detached_runs(monkeypatch):
    """Processes the test started with jobs.start (detached make runs, captions setup) are stopped
    afterwards, with everything they started, if they still run (a failed or timed-out test)."""
    from meetaras import jobs

    started: list[int] = []
    start = jobs.start

    def tracked(cmd, log):
        started.append(start(cmd, log))
        return started[-1]

    monkeypatch.setattr(jobs, "start", tracked)
    yield started
    for pid in started:  # each leads its own process group (start_new_session)
        try:
            for sig, wait in ((signal.SIGTERM, 10.0), (signal.SIGKILL, 5.0)):
                os.killpg(pid, sig)
                deadline = time.monotonic() + wait
                while time.monotonic() < deadline:
                    time.sleep(0.05)
                    os.killpg(pid, 0)  # ProcessLookupError once the whole group is gone
        except ProcessLookupError:
            pass


def ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args], check=True)


def bursts(duration: float, seed: int = 1, sr: int = SR) -> np.ndarray:
    """Noise bursts at random times plus quiet noise: unambiguous to cross-correlate, like speech."""
    rng = np.random.default_rng(seed)
    n = int(duration * sr)
    x = rng.normal(0, 0.01, n)
    t = 0.2
    while t < duration - 0.3:
        length = rng.uniform(0.05, 0.25)
        i, j = int(t * sr), int((t + length) * sr)
        x[i:j] += rng.normal(0, 0.12, j - i) * np.hanning(j - i)
        t += length + rng.uniform(0.1, 0.6)
    return x.astype(np.float32)


def external_from(x: np.ndarray, offset: float, drift_ppm: float, extra: float = 4.0, sr: int = SR) -> np.ndarray:
    """Resample so that ext_time = offset + t × (1 + drift)."""
    rate = 1 + drift_ppm / 1e6
    n_ext = int((len(x) / sr * rate + offset + extra) * sr)
    ext_t = np.arange(n_ext) / sr
    t = (ext_t - offset) / rate
    y = np.interp(t * sr, np.arange(len(x)), x, left=0, right=0)
    rng = np.random.default_rng(99)
    return (y + rng.normal(0, 0.005, n_ext)).astype(np.float32)


@pytest.fixture(scope="session")
def talk_signal() -> np.ndarray:
    return bursts(DURATION)


@pytest.fixture(scope="session")
def fixtures(tmp_path_factory, talk_signal) -> Path:
    d = tmp_path_factory.mktemp("talk")
    wavfile.write(d / "camera.wav", SR, talk_signal)
    ffmpeg(
        "-f", "lavfi", "-i", f"testsrc2=size=1280x720:rate=30:duration={DURATION}",
        "-i", str(d / "camera.wav"),
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest",
        str(d / "talk.mp4"),
    )  # fmt: skip
    wavfile.write(d / "mic.wav", SR, external_from(talk_signal, EXT_OFFSET, EXT_DRIFT_PPM))
    make_pdf(d / "deck.pdf")
    return d


def make_pdf(path: Path) -> None:
    doc = pymupdf.open()
    for i, (w, h) in enumerate([(960, 540), (960, 540), (800, 600)], 1):  # last page is 4:3
        page = doc.new_page(width=w, height=h)
        page.draw_rect(page.rect, color=None, fill=(1, 1, 1))
        page.insert_text((60, 120), f"Slide {i}", fontsize=64)
    doc.save(path)


@pytest.fixture(scope="session")
def hdr_video(tmp_path_factory) -> Path:
    """10-bit HEVC tagged as HLG/BT.2020, like an iPhone HDR recording."""
    out = tmp_path_factory.mktemp("hdr") / "hdr.mov"
    ffmpeg(
        "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30:duration=2",
        "-vf", "zscale=t=arib-std-b67:m=2020_ncl:p=2020:tin=709:min=709:pin=709,format=yuv420p10le",
        "-c:v", "libx265", "-preset", "ultrafast", "-x265-params", "log-level=error",
        "-tag:v", "hvc1",
        "-color_primaries", "bt2020", "-color_trc", "arib-std-b67", "-colorspace", "bt2020nc",
        str(out),
    )  # fmt: skip
    return out


@pytest.fixture(scope="session")
def uhd_video(tmp_path_factory) -> Path:
    """2 s of 3840×2160 30 fps H.264 with sound, like a 4K camera (its 4K master is a copy)."""
    out = tmp_path_factory.mktemp("uhd") / "4k.mp4"
    ffmpeg(
        "-f", "lavfi", "-i", "testsrc2=size=3840x2160:rate=30:duration=2",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(out),
    )  # fmt: skip
    return out


@pytest.fixture(scope="session")
def vfr_video(tmp_path_factory) -> Path:
    out = tmp_path_factory.mktemp("vfr") / "vfr.mp4"
    ffmpeg(
        "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=60:duration=3",
        "-vf", "select='not(mod(n\\,3))+lt(n\\,30)',setpts=N/(40*TB)",
        "-fps_mode", "vfr", "-c:v", "libx264", "-preset", "ultrafast",
        str(out),
    )  # fmt: skip
    return out


@pytest.fixture(scope="session")
def portrait_video(tmp_path_factory) -> Path:
    d = tmp_path_factory.mktemp("portrait")
    ffmpeg("-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30:duration=1", "-c:v", "libx264", str(d / "raw.mp4"))
    ffmpeg("-display_rotation", "90", "-i", str(d / "raw.mp4"), "-c", "copy", str(d / "portrait.mp4"))
    return d / "portrait.mp4"


@pytest.fixture(scope="session")
def screen_video(fixtures) -> Path:
    """Silent 16:10 screen recording, like a Mac screen capture."""
    out = fixtures / "screen.mov"
    ffmpeg(
        "-f", "lavfi", "-i", "smptebars=size=1920x1200:rate=30:duration=10",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(out),
    )  # fmt: skip
    return out


@pytest.fixture(scope="session")
def bumpers(fixtures) -> tuple[Path, Path]:
    """An intro with a loud tone at another size and frame rate, and a silent outro."""
    intro, outro = fixtures / "intro.mov", fixtures / "outro.mp4"
    ffmpeg(
        "-f", "lavfi", "-i", "smptebars=size=1280x720:rate=25:duration=2",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=2:sample_rate=44100",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(intro),
    )  # fmt: skip
    ffmpeg(
        "-f", "lavfi", "-i", "testsrc2=size=1920x1080:rate=30:duration=1.5",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(outro),
    )  # fmt: skip
    return intro, outro


def filmed_projector(pngs, order, seconds, out):
    """Simulate a camera filming a projector: slides warped in perspective, washed out, noisy,
    with a 'speaker' block walking in front of the screen."""
    w, h, fps = 1280, 720, 30
    dst = np.float32([[300, 60], [1180, 100], [1160, 560], [320, 600]])
    proc = subprocess.Popen(
        ["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "gray", "-s", f"{w}x{h}", "-r", str(fps),
         "-i", "-", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(out)],
        stdin=subprocess.PIPE,
    )  # fmt: skip
    rng = np.random.default_rng(0)
    for k, page in enumerate(order):
        slide = cv2.imread(str(pngs[page - 1]), cv2.IMREAD_GRAYSCALE)
        sh, sw = slide.shape
        M = cv2.getPerspectiveTransform(np.float32([[0, 0], [sw, 0], [sw, sh], [0, sh]]), dst)
        screen = cv2.warpPerspective(slide, M, (w, h), borderValue=60)
        washed = (0.45 * screen + 120).astype(np.float32)
        for n in range(seconds * fps):
            frame = washed + rng.normal(0, 3, (h, w))
            x = 100 + (k * seconds * fps + n) * 3 % 900  # speaker walking across
            frame[250:720, x : x + 140] = 50
            proc.stdin.write(np.clip(frame, 0, 255).astype(np.uint8).tobytes())
    proc.stdin.close()
    proc.wait()


def distinct_deck(path):
    """Three pages with different layouts, like real slides (title, boxes diagram, bullet list)."""
    doc = pymupdf.open()
    p = doc.new_page(width=960, height=540)
    p.insert_text((200, 280), "Engines and Ideas", fontsize=48)
    p = doc.new_page(width=960, height=540)
    for i in range(4):
        p.draw_rect(pymupdf.Rect(60 + i * 220, 200, 220 + i * 220, 300), color=(0, 0, 0), fill=(0.6, 0.8, 1), width=2)
        p.insert_text((80 + i * 220, 255), f"Step {i + 1}", fontsize=22)
    p = doc.new_page(width=800, height=600)  # 4:3, letterboxed
    for i, line in enumerate(["Collection", "Ingestion", "Validation", "Training", "Deployment"]):
        p.insert_text((120, 140 + i * 80), f"- {line}", fontsize=36)
    doc.save(path)


@pytest.fixture(scope="session")
def _recordings(tmp_path_factory) -> tuple[Path, Path]:
    """(the shared folder, a .work/ with its deck's slides rendered)."""
    from meetaras.slides import render_slides

    d = tmp_path_factory.mktemp("share")
    distinct_deck(d / "deck.pdf")
    work = tmp_path_factory.mktemp("recordings-work")
    pngs = render_slides(d / "deck.pdf", work)
    filmed_projector(pngs, [1, 2, 3], seconds=4, out=d / "silent.mp4")
    sound = bursts(12.0)
    wavfile.write(d / "camera.wav", SR, sound)
    wavfile.write(d / "mic.wav", SR, external_from(sound, 1.0, 0.0))
    ffmpeg("-i", str(d / "silent.mp4"), "-i", str(d / "camera.wav"), "-c:v", "copy", "-c:a", "aac", "-shortest", str(d / "IMG_0001.MOV"))
    return d, work


@pytest.fixture(scope="session")
def recordings(_recordings) -> Path:
    """A small real-looking talk in a 'shared folder': a camera filming the projector (slides 1, 2, 3,
    4 s each) with sound, a separate mic, and the deck."""
    return _recordings[0]


RUN_FILES = ("status.json", "make.log", "make.lock", "export.key")  # a run's own files, not caches


@pytest.fixture(scope="session")
def recordings_work(_recordings, tmp_path_factory) -> Path:
    """The .work/ of a talk made from `recordings` up to the draft (make --review), without the run's
    status: the converted video, the slides and the slide recognition, made once per session."""
    from meetaras import make

    recordings, slides_work = _recordings
    folder = tmp_path_factory.mktemp("reviewed") / "talk"
    make.new_talk(folder, recordings / "IMG_0001.MOV", recordings / "deck.pdf")
    shutil.copytree(slides_work, folder / ".work")
    make.make(folder, review=True)
    for name in RUN_FILES:
        (folder / ".work" / name).unlink(missing_ok=True)
    return folder / ".work"


@pytest.fixture
def seed_caches(recordings_work):
    """seed_caches(talk folder): give a new talk made from `recordings` the caches of an earlier run
    on the same files, as a second talk from the same recordings finds them. make's convert, slides
    and draft steps then run as usual and find their results (they still report "done"); the
    first-run path is covered by the app tests, which start from nothing.

    The caches are keyed on file name, size and mtime (the deck is copied in with its mtime), and
    copytree keeps mtimes."""

    def seed(folder: Path) -> None:
        shutil.copytree(recordings_work, Path(folder) / ".work", dirs_exist_ok=True)

    return seed


def whisper_json(words):
    """What whisper.cpp writes with -ojf: tokens with times (ms) and DTW times (cs)."""
    tokens = [{"text": f" {w}", "offsets": {"from": ms, "to": ms + 300}, "t_dtw": ms // 10} for w, ms in words]
    return {"transcription": [{"text": " ".join(w for w, _ in words), "tokens": tokens}]}


@pytest.fixture
def tools_here(tmp_path, monkeypatch):
    """No whisper.cpp on this machine; a small model, CMake and whisper.cpp source to bundle."""
    from meetaras import macbundle, tools

    model = tmp_path / "model.bin"
    model.write_bytes(b"model")
    monkeypatch.setattr(tools, "find_whisper", lambda: (None, model))
    (tmp_path / "www").mkdir()
    (tmp_path / "www" / "cmake.tar.gz").write_bytes(b"cmake" * 100)
    handler = lambda *a: http.server.SimpleHTTPRequestHandler(*a, directory=str(tmp_path / "www"))  # noqa: E731
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setattr(macbundle, "CMAKE_URL", f"http://127.0.0.1:{server.server_port}/cmake.tar.gz")
    monkeypatch.setattr(macbundle, "CMAKE_SHA256", hashlib.sha256(b"cmake" * 100).hexdigest())
    repo = tmp_path / "whisper-src"
    repo.mkdir()
    git = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run([*git, "init", "-q"], check=True)
    (repo / "CMakeLists.txt").write_text("# whisper.cpp\n")
    subprocess.run([*git, "add", "."], check=True)
    subprocess.run([*git, "commit", "-qm", "pinned"], check=True)
    commit = subprocess.run([*git, "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    monkeypatch.setattr(tools, "WHISPER_COMMIT", commit)
    monkeypatch.setattr(tools, "whisper_source", lambda: repo)
    yield
    server.shutdown()
