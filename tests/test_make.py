"""meetaras new / make / status on a small generated talk (whisper mocked; caches seeded from one session run)."""

import json
import time

import cv2
import numpy as np
import pytest
from typer.testing import CliRunner

from meetaras import captions, cli, ffmpeg, make
from meetaras.labels import render_overlays
from meetaras.layouts import UHD
from meetaras.normalize import export_masters, normalized_path
from meetaras.render import render, the_edit
from meetaras.slides import render_slides
from meetaras.timeline import load

from .test_render import streams


def fake_transcribe(audio_inputs, audio_filter, duration, prompt, language, work, key_parts):
    """Stands in for whisper.cpp: two words, saved like a real transcript."""
    out = work / "transcript-fake.json"
    tokens = [{"text": " Hello", "offsets": {"from": 1000, "to": 1400}}, {"text": " world.", "offsets": {"from": 1500, "to": 1900}}]
    out.write_text(json.dumps({"transcription": [{"tokens": tokens}]}))
    out.with_suffix(".key").write_text(repr(key_parts))
    return out


def test_new_writes_the_header(recordings, tmp_path):
    folder = tmp_path / "mytalk"
    out = make.new_talk(folder, recordings / "IMG_0001.MOV", recordings / "deck.pdf", speaker="Ada  Lovelace", talk="Engines")
    text = out.read_text()
    assert f"presenter  {recordings / 'IMG_0001.MOV'}" in text  # referenced in place
    assert "slides     deck.pdf" in text and (folder / "deck.pdf").is_file()  # copied in
    assert "speaker    Ada Lovelace" in text
    assert load(out).events == []  # not drafted yet
    with pytest.raises(make.MakeError, match="exists already"):
        make.new_talk(folder, recordings / "IMG_0001.MOV", recordings / "deck.pdf")


def test_new_matches_the_recordings(recordings, uhd_video, tmp_path):
    deck, camera = recordings / "deck.pdf", recordings / "IMG_0001.MOV"  # (that camera is 1280×720)
    assert "resolution 4k\n" in make.new_talk(tmp_path / "a", uhd_video, deck).read_text()
    assert "resolution" not in make.new_talk(tmp_path / "b", camera, deck).read_text()
    assert "resolution" not in make.new_talk(tmp_path / "c", uhd_video, deck, resolution="1080p").read_text()
    assert "resolution 4k\n" in make.new_talk(tmp_path / "d", camera, deck, resolution="4k").read_text()
    # the screen recording counts too: one bigger than 1080p (a Mac's Retina screen) makes it 4K
    retina = tmp_path / "retina.mp4"
    ffmpeg.run(["-f", "lavfi", "-i", "testsrc2=size=2880x1800:rate=30:duration=0.2", "-c:v", "libx264",
                "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(retina)], quiet=True)  # fmt: skip
    assert "resolution 4k\n" in make.new_talk(tmp_path / "g", camera, deck, screen=retina).read_text()
    assert "resolution" not in make.new_talk(tmp_path / "h", camera, deck, screen=camera).read_text()  # a screen no bigger than 1080p
    with pytest.raises(make.MakeError, match="export size must be auto, 1080p or 4k"):
        make.new_talk(tmp_path / "e", uhd_video, deck, resolution="8k")
    result = CliRunner().invoke(cli.app, ["new", str(tmp_path / "f"), "--video", str(uhd_video), "--slides", str(deck),
                                          "--resolution", "1080p"])  # fmt: skip
    assert result.exit_code == 0 and "export size 1080p" in result.output, result.output
    assert "resolution" not in (tmp_path / "f" / "timeline.txt").read_text()


def test_only_videos_bigger_than_1080p_get_4k_masters(recordings, uhd_video, bumpers, tmp_path):
    intro, outro = bumpers  # 1280×720 and 1920×1080: scaled up in a 4K export, no master of their own
    tl = load(make.new_talk(tmp_path / "a", uhd_video, recordings / "deck.pdf", intro=intro, outro=outro))
    assert export_masters(tl, tmp_path / "a" / ".work") == {
        "presenter": normalized_path(uhd_video, tmp_path / "a" / ".work", "presenter", UHD.canvas)
    }
    tl.resolution = "1080p"
    assert export_masters(tl, tmp_path / "a" / ".work") == {}


def test_new_checks_the_inputs(recordings, tmp_path):
    with pytest.raises(make.MakeError, match="presenter file not found"):
        make.new_talk(tmp_path / "a", recordings / "missing.mov", recordings / "deck.pdf")
    with pytest.raises(make.MakeError, match="must be a PDF"):
        make.new_talk(tmp_path / "b", recordings / "IMG_0001.MOV", recordings / "IMG_0001.MOV")
    assert not (tmp_path / "a").exists() and not (tmp_path / "b").exists()


@pytest.mark.slow
def test_make_review_then_finish(recordings, seed_caches, tmp_path, monkeypatch):
    monkeypatch.setattr(captions, "available", lambda: True)
    monkeypatch.setattr(captions, "transcribe", fake_transcribe)
    folder = tmp_path / "talk"
    make.new_talk(folder, recordings / "IMG_0001.MOV", recordings / "deck.pdf", audio=recordings / "mic.wav")
    seed_caches(folder)
    assert "audio      " in (folder / "timeline.txt").read_text()  # the mic line make-talk.sh used to patch in

    make.make(folder, review=True)
    status = make.read_status(folder)
    assert status["state"] == "review"
    assert [s["state"] for s in status["steps"].values()] == ["done", "done", "done", "waiting", "waiting", "waiting"]
    tl = load(folder / "timeline.txt")
    assert [e.slide for e in tl.events] == [1, 2, 3]
    assert not (folder / "output").exists()

    make.make(folder)  # continues: the draft is kept, captions and export run
    status = make.read_status(folder)
    assert status["state"] == "done"
    assert status["steps"]["draft"]["state"] == "skipped"
    assert status["steps"]["captions"]["state"] == "done"
    out = folder / "output"
    assert (out / "talk.mp4").is_file()  # no video info: named after the folder
    assert "Hello world." in (out / "talk.srt").read_text()
    assert (out / "talk - YouTube.txt").is_file() and (out / "talk - YouTube.md").is_file()
    assert "Done:" in make.describe(status, folder)

    make.make(folder)  # nothing changed: no new export
    assert make.read_status(folder)["steps"]["export"]["detail"] == "talk.mp4 is up to date; captions written"
    timeline = (folder / "timeline.txt").read_text()
    (folder / "timeline.txt").write_text(timeline.replace("slides ", "talk       Engines\nspeaker    Ada\nslides ", 1) + "0:10 presenter\n")
    make.make(folder)  # the video info names the files; the old-named ones Meetaras made are replaced
    assert make.read_status(folder)["steps"]["export"]["detail"] == "Engines - Ada.mp4 and its captions"
    assert sorted(p.name for p in out.iterdir()) == [
        "Engines - Ada - YouTube.md", "Engines - Ada - YouTube.txt", "Engines - Ada.mp4", "Engines - Ada.srt",
    ]  # fmt: skip


def test_draft_command_keeps_the_header(recordings, seed_caches, tmp_path):
    folder = tmp_path / "talk"
    tl_file = make.new_talk(folder, recordings / "IMG_0001.MOV", recordings / "deck.pdf", audio=recordings / "mic.wav", speaker="Ada")
    seed_caches(folder)
    args = ["draft", str(recordings / "IMG_0001.MOV"), str(folder / "deck.pdf"), "-o", str(tl_file)]
    assert CliRunner().invoke(cli.app, args).exit_code == 1  # exists: only with --force
    result = CliRunner().invoke(cli.app, [*args, "--force"])
    assert result.exit_code == 0, result.output
    tl = load(tl_file)
    assert tl.speaker == "Ada" and tl.audio == recordings / "mic.wav"  # the header from `meetaras new` stays
    assert [e.slide for e in tl.events] == [1, 2, 3] and tl_file.read_text() == tl.to_text()  # as make drafts it


@pytest.mark.slow
def test_make_without_whisper_skips_captions(recordings, seed_caches, tmp_path, monkeypatch):
    monkeypatch.setattr(captions, "available", lambda: False)
    folder = tmp_path / "talk"
    make.new_talk(folder, recordings / "IMG_0001.MOV", recordings / "deck.pdf")
    seed_caches(folder)
    make.make(folder / "timeline.txt")  # the timeline file works as well as the folder
    status = make.read_status(folder)
    assert status["steps"]["captions"] == {**status["steps"]["captions"], "state": "skipped", "detail": "whisper.cpp not set up (meetaras doctor)"}
    assert (folder / "output" / "talk.mp4").is_file() and not (folder / "output" / "talk.srt").exists()
    make.make(folder)
    assert make.read_status(folder)["steps"]["export"]["detail"] == "talk.mp4 is up to date"

    # `meetaras render` exports to the same place, replacing the video it made under another name.
    tl_file = folder / "timeline.txt"
    tl_file.write_text(tl_file.read_text().replace("slides ", "speaker    Ada\nslides ", 1))
    result = CliRunner().invoke(cli.app, ["render", str(folder)])
    assert result.exit_code == 0, result.output
    assert (folder / "output" / "Ada.mp4").is_file() and not (folder / "output" / "talk.mp4").exists()


def test_one_run_per_talk_and_interrupted_runs(recordings, tmp_path):
    folder = tmp_path / "talk"
    make.new_talk(folder, recordings / "IMG_0001.MOV", recordings / "deck.pdf")
    work = folder / ".work"
    work.mkdir()
    with make.lock(work):
        assert make.running(work)
        with pytest.raises(make.MakeError, match="already running"):
            make.make(folder)
    assert not make.running(work)
    # A run that was killed leaves "running" behind, but no lock.
    (work / "status.json").write_text(json.dumps({
        "state": "running", "pid": 1, "started": time.time(),
        "steps": {"convert": {"state": "running", "detail": "normalize 40%"}},
    }))  # fmt: skip
    status = make.read_status(folder)
    assert status["state"] == "interrupted" and status["steps"]["convert"]["state"] == "interrupted"
    assert "continues where it stopped" in make.describe(status, folder)


@pytest.mark.slow
def test_detached_run_outlives_the_caller(recordings, seed_caches, tmp_path):
    folder = tmp_path / "talk"
    make.new_talk(folder, recordings / "IMG_0001.MOV", recordings / "deck.pdf")
    seed_caches(folder)
    pid = make.start_detached(folder, review=True)
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        status = make.read_status(folder)
        if status and status["state"] != "running" and status["pid"] == pid:
            break
        time.sleep(0.5)
    assert status["state"] == "review", (folder / ".work" / make.LOG).read_text()
    assert "== draft" in (folder / ".work" / make.LOG).read_text()


@pytest.mark.slow
def test_4k_export_and_changing_the_size(recordings, uhd_video, tmp_path, capsys):
    """A 1080p talk switched to 4K: the export makes only the 4K master, exports again (the timeline changed)
    and replaces the video in output/; the editor's and the draft's video stays 1080p, previews too."""
    folder = tmp_path / "talk"
    tl_file = make.new_talk(folder, uhd_video, recordings / "deck.pdf", resolution="1080p", speaker="Ada", talk="Engines", event="Meetup")
    tl_file.write_text(tl_file.read_text() + "0:00 presenter\n0:00.5 slide 1\n0:01 slide 2 speaker\n0:01.5 presenter\n")
    make.make(folder)
    video = folder / "output" / "Engines - Ada.mp4"
    assert ffmpeg.probe_video(video).height == 1080

    tl_file.write_text(tl_file.read_text().replace("talk       Engines\n", "talk       Engines\nresolution 4k\n"))
    make.make(folder)
    steps = make.read_status(folder)["steps"]
    assert steps["convert"]["detail"].endswith("of video; the export makes the 4K masters")  # no pass to share
    assert steps["export"]["detail"] == "Engines - Ada.mp4"  # not "up to date": exported again
    assert sorted(p.name for p in (folder / "output").iterdir()) == ["Engines - Ada - YouTube.md", "Engines - Ada - YouTube.txt", "Engines - Ada.mp4"]
    info = ffmpeg.probe_video(video)
    assert (info.width, info.height, info.color_primaries) == (3840, 2160, "bt709") and info.has_audio

    work, tl = folder / ".work", load(tl_file)
    assert ffmpeg.probe_video(normalized_path(uhd_video, work, "presenter")).height == 1080  # the editor's
    master = ffmpeg.probe_video(normalized_path(uhd_video, work, "presenter", UHD.canvas))
    assert (master.width, master.height, master.has_audio) == (3840, 2160, False)  # picture only: the sound is the 1080p file's
    assert cv2.imread(str(render_slides(tl.slides, work, UHD.canvas)[0])).shape == (2160, 3840, 3)
    badge = render_overlays(tl.info, work, UHD)["badge"]
    assert cv2.imread(str(badge.path), cv2.IMREAD_UNCHANGED).shape[:2] == (badge.box.h, badge.box.w)
    raw = ffmpeg.run_raw(["-ss", "0.2", "-i", str(video), "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "gray", "-"])
    frame = np.frombuffer(raw, np.uint8).reshape(2160, 3840)
    b = badge.box
    assert (frame[b.y + 6 : b.y + b.h - 6, b.x + 6 : b.x + b.w - 6] < 90).mean() > 0.6  # the badge, at its 4K place

    capsys.readouterr()
    preview = render(tl_file, tl, folder / "output-preview.mp4", preview=True)
    assert ffmpeg.probe_video(preview).height == 720
    assert "at 4K" not in capsys.readouterr().out  # made from the 1080p slides and video


@pytest.mark.slow
def test_4k_after_review_and_a_1080p_intro(recordings, uhd_video, bumpers, tmp_path, monkeypatch):
    """--review converts only the 1080p files (the editor opens sooner); continuing, the export makes the
    camera's 4K master, with progress. The 720p intro gets none: the export scales its 1080p file up."""
    intro = bumpers[0]
    folder = tmp_path / "talk"
    tl_file = make.new_talk(folder, uhd_video, recordings / "deck.pdf", intro=intro)
    assert "resolution 4k" in tl_file.read_text()  # matches the camera
    tl_file.write_text(tl_file.read_text() + "0:00 presenter\n0:01 slide 1\n")
    work = folder / ".work"
    camera_4k = normalized_path(uhd_video, work, "presenter", UHD.canvas)
    assert export_masters(load(tl_file), work) == {"presenter": camera_4k}

    make.make(folder, review=True)
    assert normalized_path(uhd_video, work, "presenter").exists() and not camera_4k.exists()
    assert make.read_status(folder)["steps"]["convert"]["detail"].endswith("; the export makes the 4K masters")

    reported = []
    progress = make.Status.progress
    monkeypatch.setattr(make.Status, "progress", lambda st, label, f: reported.append((st.current, label)) or progress(st, label, f))
    make.make(folder)
    assert make.read_status(folder)["state"] == "done"
    assert ("export", "copy 4K") in reported  # the camera's 4K master, made by the export step
    assert camera_4k.exists() and not normalized_path(intro, work, "intro", UHD.canvas).exists()
    video = streams(folder / "output" / "talk.mp4")["streams"][0]
    assert (video["width"], video["height"]) == (3840, 2160)
    edit = the_edit(load(tl_file), work)
    assert edit.intro == 2 and int(video["nb_read_frames"]) == round(edit.total * 30)  # the intro, then the talk
