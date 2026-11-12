import subprocess

import cv2
import numpy as np
import pytest

from meetaras.draft import draft, draft_with_screen, to_events
from meetaras.slides import render_slides
from meetaras.timeline import Timeline, parse

from .conftest import distinct_deck, filmed_projector


@pytest.mark.slow
def test_draft_recognises_slides(tmp_path):
    distinct_deck(tmp_path / "deck.pdf")
    pngs = render_slides(tmp_path / "deck.pdf", tmp_path)
    video = tmp_path / "projector.mp4"
    filmed_projector(pngs, [1, 2, 3, 2], seconds=5, out=video)
    changes = draft(video, pngs)
    assert [c.slide for c in changes] == [1, 2, 3, 2]
    for c, expected in zip(changes, [0, 5, 10, 15]):
        assert abs(c.time - expected) <= 1.0

    text = Timeline(tmp_path, video, tmp_path / "deck.pdf", events=to_events(changes)).to_text()
    tl = parse(text, tmp_path)  # the draft is a valid timeline
    assert [s.slide for s in tl.segments(20)] == [1, 2, 3, 2]


def screen_recording(pngs, out, lead_demo=3, order=(1, 2, 3, 2), seconds=5, tail_demo=8):
    """A clean 16:10 screen capture: desktop/code first, then the slides full screen, then a demo."""
    w, h, fps = 1440, 900, 10
    proc = subprocess.Popen(
        ["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "gray", "-s", f"{w}x{h}", "-r", str(fps),
         "-i", "-", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(out)],
        stdin=subprocess.PIPE,
    )  # fmt: skip

    def code(n):
        img = np.full((h, w), 30, np.uint8)
        for i in range(40):
            cv2.putText(img, f"def step_{n + i}(x): return x * {i} + {n}", (40, 30 + i * 22), cv2.FONT_HERSHEY_SIMPLEX, 0.6, 200, 1)
        return img

    frames = [code(k // 5) for k in range(lead_demo * fps)]
    for page in order:
        slide = cv2.resize(cv2.imread(str(pngs[page - 1]), cv2.IMREAD_GRAYSCALE), (w, w * 9 // 16))
        img = np.zeros((h, w), np.uint8)
        y = (h - slide.shape[0]) // 2
        img[y : y + slide.shape[0]] = slide
        frames += [img] * (seconds * fps)
    frames += [code(100 + k // 5) for k in range(tail_demo * fps)]
    for f in frames:
        proc.stdin.write(f.tobytes())
    proc.stdin.close()
    proc.wait()


@pytest.mark.slow
def test_draft_with_screen_recording(tmp_path):
    distinct_deck(tmp_path / "deck.pdf")
    pngs = render_slides(tmp_path / "deck.pdf", tmp_path)
    camera = tmp_path / "projector.mp4"
    filmed_projector(pngs, [1, 2, 3, 2], seconds=5, out=camera)  # camera starts with slide 1
    screen = tmp_path / "screen.mp4"
    screen_recording(pngs, screen)  # 3 s of desktop before slide 1, a demo after the slides

    result = draft_with_screen(camera, screen, pngs)
    assert result.offset == pytest.approx(-3, abs=0.6)
    assert result.matches >= 2
    kinds = [(c.kind, c.slide) for c in result.changes]
    assert kinds == [("slide", 1), ("slide", 2), ("slide", 3), ("slide", 2), ("screen", None)]
    assert result.changes[0].time == 0
    assert result.changes[-1].time == pytest.approx(20, abs=1.2)  # demo starts when the slides end

    events = to_events(result.changes)
    text = Timeline(tmp_path, camera, tmp_path / "deck.pdf", screen=screen, screen_offset=result.offset, events=events).to_text()
    tl = parse(text, tmp_path)
    assert tl.screen_offset == pytest.approx(result.offset)
    assert [s.layout for s in tl.segments(28)] == ["slide", "slide", "slide", "slide", "screen"]


def test_context_rules():
    from meetaras.draft import Change, apply_context

    words = [40, 3, 50, 50]  # slide 2 is a title slide
    changes = [
        Change(0, "screen"),  # organiser intro
        Change(60, "slide", 2),  # title: speaker large
        Change(70, "slide", 3),  # first two slides in a row: the talk has started
        Change(80, "slide", 4),  # held for 300 s: rhythm
        Change(380, "screen"),  # demo
    ]
    out = apply_context(changes, words, 600)
    got = [(c.time, c.kind, c.slide, c.style) for c in out]
    assert got[0] == (0, "screen", None, "speaker")
    assert got[1] == (60, "slide", 2, "speaker")
    assert got[2] == (70, "slide", 3, "")
    rhythm = [g for g in got if 80 <= g[0] < 380]
    assert rhythm[:4] == [(80, "slide", 4, ""), (125, "slide", 4, "speaker"), (155, "slide", 4, ""), (200, "slide", 4, "speaker")]
    assert all(380 - t >= 15 for t, *_ in rhythm)
    assert got[-1] == (380, "screen", None, "")  # demos stay large


def test_a_video_that_cannot_be_read_is_an_error(tmp_path):
    from meetaras.draft import frames
    from meetaras.ffmpeg import FFmpegError

    bad = tmp_path / "bad.mp4"
    bad.write_bytes(b"not a video")
    with pytest.raises(FFmpegError, match="reading bad.mp4 failed"):
        list(frames(bad, 1.0))  # not zero frames, which would look like "no slides recognised"
