"""Portable talk folders: relative header paths, caches that survive moving a talk."""

import shutil
from pathlib import Path

import pytest

from meetaras.captions import transcript_key, transcript_path
from meetaras.slides import render_slides
from meetaras.timeline import header_path, load, parse

BASE = Path("/talks/demo")


def test_header_paths_kept_as_written():
    text = "presenter  /mnt/share/IMG 1.MOV\nslides     ./deck.pdf\nintro      ../assets/intro.mp4\n\n0:00     presenter\n"
    tl = parse(text, BASE)
    assert tl.presenter == Path("/mnt/share/IMG 1.MOV")
    assert tl.slides == BASE / "deck.pdf" and tl.intro == Path("/talks/assets/intro.mp4")
    assert tl.to_text() == text


def test_new_header_paths_relative_inside_the_talk_folder():
    tl = parse("presenter /mnt/share/IMG.MOV\nslides deck.pdf\n", BASE)
    tl.presenter, tl.slides = BASE / "media" / "talk.mov", Path("/elsewhere/deck.pdf")
    assert tl.to_text().splitlines()[:2] == ["presenter  media/talk.mov", "slides     /elsewhere/deck.pdf"]
    assert header_path(Path("/talks/demo/../demo/a.mov"), BASE) == "a.mov"


def test_symlinked_video_stays_in_the_talk_folder(tmp_path):
    (tmp_path / "share").mkdir()
    (tmp_path / "share" / "IMG.MOV").write_bytes(b"x")
    talk = tmp_path / "talk"
    talk.mkdir()
    (talk / "talk.mov").symlink_to(tmp_path / "share" / "IMG.MOV")
    (talk / "timeline.txt").write_text("presenter talk.mov\nslides deck.pdf\n")
    tl = load(talk / "timeline.txt")
    assert tl.presenter == talk / "talk.mov"
    assert header_path(tl.presenter, talk) == "talk.mov"


@pytest.mark.slow
def test_caches_survive_moving_the_talk_folder(fixtures, tmp_path):
    a = tmp_path / "a"
    a.mkdir()
    shutil.copy2(fixtures / "deck.pdf", a / "deck.pdf")  # copy2 keeps the mtime, like cp -p, rsync -a, Finder
    first = render_slides(a / "deck.pdf", a / ".work")
    mtime = first[0].stat().st_mtime_ns
    b = tmp_path / "elsewhere" / "b"
    b.parent.mkdir()
    shutil.move(a, b)
    second = render_slides(b / "deck.pdf", b / ".work")
    assert [p.relative_to(b) for p in second] == [p.relative_to(a) for p in first]
    assert second[0].stat().st_mtime_ns == mtime  # reused, not rendered again



def test_transcript_key_names_the_audio_not_its_place(fixtures, tmp_path):
    """The key recorded next to a transcript names the inputs, not their places (hours of work: never change it)."""
    tl = parse(f"presenter {fixtures / 'talk.mp4'}\naudio {fixtures / 'mic.wav'}\nslides deck.pdf\n", BASE)
    master = tmp_path / "presenter-0123456789abcdef.mp4"
    key = (master.name, f"mic.wav|{tl.audio.stat().st_size}|{tl.audio.stat().st_mtime_ns}", None, 0.0)
    assert transcript_key(tl, master) == key
    (tmp_path / "transcript-0123.json").write_text("{}")
    (tmp_path / "transcript-0123.key").write_text(repr(key))
    assert transcript_path(tl, master, tmp_path) == tmp_path / "transcript-0123.json"
    (tmp_path / "transcript-0123.key").write_text(repr((master.name, None, None, 0.0)))  # other audio
    assert transcript_path(tl, master, tmp_path) is None
