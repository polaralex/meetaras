"""Cache names of 1080p talks, pinned: users have hours of converted videos, clips and measurements in
.work/, so a change that renames them by accident must fail here (golden values from before 4K exports)."""

import hashlib
import os
from pathlib import Path

from meetaras import layouts
from meetaras.labels import Overlay, inset_decor, render_overlays
from meetaras.layouts import UHD, Box
from meetaras.normalize import normalized_path
from meetaras.render import FINAL, PREVIEW, FrameSegment, Sources, bumper_clip, clip_key
from meetaras.slides import slides_dir
from meetaras.timeline import Info, Segment

MTIME = 1_760_000_000_000_000_000
INFO = Info("Meetup #42", "1 Oct 2026", "Ada Lovelace", "Engineer", "Engines and Ideas")


def label(name, box):
    return Overlay(name.split("-")[0], Path(name), box)


def fake(path, size):
    """A file with a fixed name, size and mtime: what cache keys see of an input."""
    path.write_bytes(b"\0" * size)
    os.utime(path, ns=(MTIME, MTIME))
    return path


def test_converted_videos_and_slides(tmp_path):
    video, work = fake(tmp_path / "IMG_0001.MOV", 1000), tmp_path / ".work"
    assert normalized_path(video, work, "presenter").name == "presenter-ffcc9a9e37f72305.mp4"
    assert normalized_path(video, work, "screen").name == "screen-879f820e1309550c.mp4"
    assert normalized_path(video, work, "intro").name == "intro-217aa5679ff47c36.mp4"
    assert normalized_path(video, work, "outro").name == "outro-217aa5679ff47c36.mp4"
    assert slides_dir(fake(tmp_path / "deck.pdf", 500), work).name == "7aee5461fd5495c2"
    # 4K masters live next to them under other names
    assert normalized_path(video, work, "presenter", UHD.canvas).name != "presenter-ffcc9a9e37f72305.mp4"


def test_labels_and_decor(tmp_path):
    assert [p.name for p in inset_decor(480, 270, tmp_path)] == ["mask-540884dac43c9577.png", "shadow-540884dac43c9577.png"]
    assert [p.name for p in inset_decor(592, 333, tmp_path)] == ["mask-b6ae0caa32e263fd.png", "shadow-b6ae0caa32e263fd.png"]
    overlays = render_overlays(INFO, tmp_path)
    assert {n: (o.path.name, o.box) for n, o in overlays.items()} == {
        "strip_corner": ("strip_corner-b72030b4fbdf94ff.png", Box(1368, 995, 480, 34)),
        "strip_side": ("strip_side-b72030b4fbdf94ff.png", Box(1320, 470, 576, 34)),
        "panel": ("panel-b72030b4fbdf94ff.png", Box(1320, 532, 576, 170)),
        "badge": ("badge-b72030b4fbdf94ff.png", Box(32, 28, 284, 42)),
        "lower_third": ("lower_third-b72030b4fbdf94ff.png", Box(64, 846, 411, 144)),
    }
    big = render_overlays(INFO, tmp_path, UHD)  # 4K: the same labels, twice the size, other names
    assert big["badge"].box == Box(64, 56, 568, 84) and big["badge"].path != overlays["badge"].path


def test_clips_and_bumpers(tmp_path):
    assert str(FINAL) == "Quality(name='final', size=(1920, 1080), preset='medium', crf=18, two_pass_loudnorm=True)"
    assert str(PREVIEW) == "Quality(name='preview', size=(1280, 720), preset='ultrafast', crf=28, two_pass_loudnorm=False)"
    slides = tmp_path / "slides" / "fedcba9876543210"
    src = Sources(tmp_path / "presenter-0123456789abcdef.mp4", [slides / "slide-001.png", slides / "slide-002.png"])
    strip = label("strip_corner-x.png", Box(1368, 995, 480, 34))
    assert clip_key(FrameSegment(30, 60, Segment(1, 2, "slide", 2)), src, FINAL, [strip]) == "e3520fb09bea0c8e"
    assert clip_key(FrameSegment(95, 120, Segment(3, 4, "slide_full", 1)), src, FINAL) == "a9c728ad9cba5678"
    badge = label("badge-x.png", Box(32, 28, 300, 42))
    lt = label("lower_third-x.png", Box(64, 800, 700, 190))
    assert clip_key(FrameSegment(0, 30, Segment(0, 1, "presenter")), src, PREVIEW, [badge], lt) == "7c6d0a8da851392a"
    src.screen, src.screen_offset, src.screen_crop = tmp_path / "screen-0011223344556677.mp4", 1.0, (1728, 972, 96, 54)
    side = [label("strip_side-x.png", Box(1320, 470, 576, 34)), label("panel-x.png", Box(1320, 532, 576, 150))]
    assert clip_key(FrameSegment(60, 95, Segment(2, 3, "screen_side")), src, FINAL, side) == "6a78c8c380a5975a"
    assert bumper_clip(tmp_path / "intro-0123456789abcdef.mp4", "intro", tmp_path, FINAL).name == "intro-6d56522205eb144d.mp4"


def test_default_style_filter_graph():
    # Clips are keyed on the geometry, not on the graph: a changed graph would leave stale clips in caches.
    graph = layouts.clip_filter("slide", "", (1920, 1080), (2, 3), labels=[(4, Box(1368, 995, 480, 34))])
    assert hashlib.sha256(graph.encode()).hexdigest()[:16] == "74e79e2d7d9434f8"
