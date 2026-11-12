"""End-to-end tests on short synthetic clips (each runs real ffmpeg)."""

import json
import re
import subprocess

import numpy as np
import pytest

from meetaras import ffmpeg
from meetaras.align import SAMPLE_RATE, find_lag, load_mono
from meetaras.normalize import normalize
from meetaras.render import render, the_edit
from meetaras.timeline import load

TIMELINE = """\
presenter talk.mp4
slides    deck.pdf
{extra}
0:01   start
0:03   slide 1
0:05.5 slide 3
0:08   presenter
0:11   end
"""


def write_timeline(d, extra=""):
    p = d / "timeline.txt"
    p.write_text(TIMELINE.format(extra=extra))
    return p


def streams(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", "-show_format", "-count_frames", str(path)],
        capture_output=True, text=True, check=True,
    )  # fmt: skip
    return json.loads(out.stdout)


def loudness(path):
    """(integrated LUFS, true peak dBTP) of the file's audio."""
    err = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostats", "-i", str(path), "-af", "ebur128=peak=true", "-f", "null", "-"],
        capture_output=True, text=True,
    ).stderr  # fmt: skip
    summary = err[err.index("Summary:") :]
    lufs = float(re.search(r"I:\s+(-?[\d.]+) LUFS", summary).group(1))
    peak = float(re.search(r"Peak:\s+(-?[\d.]+) dBFS", summary).group(1))
    return lufs, peak


@pytest.mark.slow
def test_render_final(fixtures):
    tl_path = write_timeline(fixtures)
    out = render(tl_path, load(tl_path), fixtures / "out.mp4")
    info = streams(out)
    v = next(s for s in info["streams"] if s["codec_type"] == "video")
    a = next(s for s in info["streams"] if s["codec_type"] == "audio")
    assert (v["width"], v["height"]) == (1920, 1080)
    assert v["codec_name"] == "h264" and v["color_primaries"] == "bt709"
    assert int(v["nb_read_frames"]) == 300  # 1 s .. 11 s at 30 fps, exact
    assert a["codec_name"] == "aac" and a["sample_rate"] == "48000"
    assert float(a["duration"]) == pytest.approx(10.0, abs=0.05)
    lufs, peak = loudness(out)
    assert lufs == pytest.approx(-16, abs=0.5)
    assert peak < -0.5


@pytest.mark.slow
def test_rerender_uses_cache(fixtures, capsys):
    tl_path = write_timeline(fixtures)
    render(tl_path, load(tl_path), fixtures / "out.mp4")
    capsys.readouterr()
    render(tl_path, load(tl_path), fixtures / "out.mp4")
    log = capsys.readouterr().out
    assert "using cached" in log and "rendering" not in log
    assert "(measured before)" in log  # loudness too: only the export itself reads the audio again


def test_folder_name_with_quote(tmp_path):
    """The concat list quotes paths: a talk folder named `it's` still exports."""
    from meetaras.render import concat_list

    d = tmp_path / "it's"
    d.mkdir()
    clips = [d / "a.mp4", d / "b.mp4"]
    for c in clips:
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=64x36:rate=30:duration=0.5",
                        "-c:v", "libx264", "-preset", "ultrafast", str(c)], check=True)  # fmt: skip
    ffmpeg.run([*concat_list(clips, d / "list.txt"), "-c", "copy", str(d / "joined.mp4")])
    v = streams(d / "joined.mp4")["streams"][0]
    assert int(v["nb_read_frames"]) == 30


@pytest.mark.slow
def test_render_preview(fixtures):
    tl_path = write_timeline(fixtures)
    out = render(tl_path, load(tl_path), fixtures / "preview.mp4", preview=True)
    v = next(s for s in streams(out)["streams"] if s["codec_type"] == "video")
    assert (v["width"], v["height"]) == (1280, 720)


@pytest.mark.slow
def test_external_audio_is_in_sync(fixtures):
    """The external track replaces camera audio and lines up with it at both ends of the output."""
    tl_path = write_timeline(fixtures, "audio mic.wav")
    out = render(tl_path, load(tl_path), fixtures / "ext.mp4")
    got = load_mono(out)
    sr = SAMPLE_RATE
    ref = load_mono(fixtures / "camera.wav")  # camera audio, presenter clock
    for at in (1.5, 9.0):  # output time; presenter time = at + 1 (start is 0:01)
        window = ref[int((at + 1) * sr) : int((at + 2) * sr)]
        pos, conf = find_lag(window, got[int((at - 1.5) * sr) : int((at + 2.5) * sr)], sr)
        assert conf > 8
        assert pos == pytest.approx(1.5, abs=0.003)


@pytest.mark.slow
def test_external_audio_on_its_second_track(fixtures):
    """A recorder file whose sound is on its second (default) track: that one is aligned and used."""
    two = fixtures / "two-tracks.mka"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "anullsrc=r=48000:cl=mono", "-i", str(fixtures / "mic.wav"),
         "-map", "0:a", "-map", "1:a", "-c:a", "flac", "-disposition:a:0", "0", "-disposition:a:1", "default",
         "-shortest", str(two)],
        check=True,
    )  # fmt: skip
    tl_path = write_timeline(fixtures, "audio two-tracks.mka")
    out = render(tl_path, load(tl_path), fixtures / "two-tracks.mp4", preview=True)
    got, ref, sr = load_mono(out), load_mono(fixtures / "camera.wav"), SAMPLE_RATE
    pos, conf = find_lag(ref[int(6.0 * sr) : int(7.0 * sr)], got[int(3.5 * sr) : int(7.5 * sr)], sr)
    assert conf > 8 and pos == pytest.approx(1.5, abs=0.003)  # output 5 s = talk time 6 s




@pytest.mark.slow
def test_manual_offset_overrides(fixtures, capsys):
    tl_path = write_timeline(fixtures, "audio mic.wav\naudio_offset 2.5\naudio_drift_ppm 500")
    render(tl_path, load(tl_path), fixtures / "manual.mp4", preview=True)
    assert "manual audio_offset +2.500" in capsys.readouterr().out


@pytest.mark.slow
def test_normalize_hdr(hdr_video, tmp_path):
    src = ffmpeg.probe_video(hdr_video)
    assert src.is_hdr
    out = normalize(hdr_video, tmp_path, "presenter")
    info = ffmpeg.probe_video(out)
    assert not info.is_hdr and info.color_transfer == "bt709"
    assert (info.width, info.height) == (1920, 1080)
    # tone-mapped picture is not black
    raw = ffmpeg.run_raw(["-i", str(out), "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "gray", "-"])
    assert np.frombuffer(raw, np.uint8).mean() > 40


def test_normalize_converts_bt601(tmp_path):
    """A webcam's BT.601 colours are converted to BT.709, not only tagged as such."""
    src = tmp_path / "webcam.mp4"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=0xC03020:s=640x480:r=30:d=1,format=rgb24",
         "-vf", "scale=out_color_matrix=bt601:out_range=tv,format=yuv420p", "-colorspace", "smpte170m",
         "-color_primaries", "smpte170m", "-color_trc", "smpte170m", "-c:v", "libx264", "-preset", "ultrafast",
         "-crf", "0", str(src)],
        check=True,
    )  # fmt: skip
    out = normalize(src, tmp_path / "work", "presenter")
    raw = ffmpeg.run_raw(["-i", str(out), "-frames:v", "1", "-vf", "scale=in_color_matrix=bt709:in_range=tv,format=rgb24",
                          "-f", "rawvideo", "-"])  # fmt: skip
    pixel = np.frombuffer(raw, np.uint8).reshape(1080, 1920, 3)[540, 960].astype(int)
    assert np.abs(pixel - [0xC0, 0x30, 0x20]).max() <= 6  # only tagged, it would be about (202, 60, 28)


def test_normalize_vfr(vfr_video, tmp_path):
    out = normalize(vfr_video, tmp_path, "presenter")
    info = ffmpeg.probe_video(out)
    assert info.fps == pytest.approx(30)


def test_audio_track_that_can_be_read_is_used(tmp_path):
    # An iPhone recording with spatial audio: an `apac` track ffmpeg can't decode, then the stereo one.
    spatial = {"codec_type": "audio", "codec_name": "unknown", "codec_tag_string": "apac", "disposition": {"default": 0}}
    stereo = {"codec_type": "audio", "codec_name": "aac", "sample_rate": "48000", "disposition": {"default": 1}}
    assert ffmpeg.audio_track({"streams": [{"codec_type": "video"}, spatial, stereo]}) == (1, stereo)
    assert ffmpeg.audio_track({"streams": [{"codec_type": "video"}, spatial]}) is None

    # The same choice in a real conversion: the first track is silent, the default second one has the sound.
    src = tmp_path / "two-tracks.mov"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30:duration=2",
         "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
         "-map", "0:v", "-map", "1:a", "-map", "2:a", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-disposition:a:0", "0", "-disposition:a:1", "default", "-t", "2", str(src)],
        check=True,
    )  # fmt: skip
    assert ffmpeg.probe_video(src).audio_index == 1
    out = normalize(src, tmp_path, "presenter")
    assert np.abs(load_mono(out)).max() > 0.05  # the tone, not the silence


def test_portrait_rejected(portrait_video, tmp_path):
    with pytest.raises(ffmpeg.FFmpegError, match="portrait"):
        normalize(portrait_video, tmp_path, "presenter")


@pytest.mark.slow
def test_render_all_layouts(fixtures, screen_video):
    tl_path = fixtures / "layouts.txt"
    tl_path.write_text(
        "presenter talk.mp4\nslides deck.pdf\nscreen screen.mov\nscreen_offset 1.0\n"
        "0:01 start\n0:02 slide 2 full\n0:03 slide 2\n0:04 slide 2 side\n0:06 screen\n0:07 screen side\n"
        "0:08 screen full\n0:10 presenter\n0:11 end\n"
    )
    out = render(tl_path, load(tl_path), fixtures / "layouts.mp4", preview=True)
    v = next(s for s in streams(out)["streams"] if s["codec_type"] == "video")
    assert int(v["nb_read_frames"]) == 300

    def frame(t):
        raw = ffmpeg.run_raw(["-ss", str(t), "-i", str(out), "-frames:v", "1", "-vf", "scale=192:108", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"])
        return np.frombuffer(raw, np.uint8).reshape(108, 192, 3).astype(int)

    full, corner, side, screen_full = frame(1.5), frame(2.5), frame(3.5), frame(7.5)
    assert (full[54, 96] > 200).all() and (full[2, 2] > 200).all()  # white slide fills the frame
    assert (corner[60, 96] > 200).all()  # slide nearly full frame…
    assert corner[95, 160].std() > 10 or abs(corner[95, 160] - 255).sum() > 60  # …with the speaker in the corner
    assert (side[100, 60] < 60).all()  # side by side: background below the slide
    assert screen_full[54, 2].sum() < 120  # 16:10 recording gets side bars in the background colour


@pytest.mark.slow
def test_cut_removes_a_part_and_keeps_sync(fixtures):
    tl_path = fixtures / "cut.txt"
    tl_path.write_text("presenter talk.mp4\nslides deck.pdf\n0:01 start\n0:03 slide 1\n0:05 cut\n0:08 presenter\n0:11 end\n")
    out = render(tl_path, load(tl_path), fixtures / "cut.mp4")  # final quality: also covers the loudness passes
    info = streams(out)
    v = next(s for s in info["streams"] if s["codec_type"] == "video")
    a = next(s for s in info["streams"] if s["codec_type"] == "audio")
    assert int(v["nb_read_frames"]) == 210  # 1–5 s and 8–11 s: 7 s at 30 fps
    assert float(a["duration"]) == pytest.approx(7.0, abs=0.05)

    # Output 4.5 s is talk time 8.5 s (4 s kept before the cut, then 3 s were dropped).
    got = load_mono(out)
    ref = load_mono(fixtures / "camera.wav")
    sr = SAMPLE_RATE
    for out_t, talk_t in ((2.0, 3.0), (4.5, 8.5), (6.0, 10.0)):
        window = ref[int(talk_t * sr) : int((talk_t + 0.8) * sr)]
        pos, conf = find_lag(window, got[int((out_t - 1.0) * sr) : int((out_t + 1.8) * sr)], sr)
        assert conf > 8
        assert pos == pytest.approx(1.0, abs=0.003)


@pytest.mark.slow
def test_intro_and_outro(fixtures, bumpers):
    tl_path = fixtures / "bumpers.txt"
    tl_path.write_text(
        "presenter talk.mp4\nslides deck.pdf\nintro intro.mov\noutro outro.mp4\n0:01 start\n0:03 slide 1\n0:05 cut\n0:08 presenter\n0:11 end\n"
    )
    out = render(tl_path, load(tl_path), fixtures / "bumpers.mp4")
    info = streams(out)
    v = next(s for s in info["streams"] if s["codec_type"] == "video")
    a = next(s for s in info["streams"] if s["codec_type"] == "audio")
    assert int(v["nb_read_frames"]) == 60 + 210 + 45  # 2 s intro, 7 s talk (one cut), 1.5 s outro
    assert float(a["duration"]) == pytest.approx(10.5, abs=0.05)
    edit = the_edit(load(tl_path), fixtures / ".work")  # what chapters and captions use: the same lengths
    assert (edit.intro, edit.total, edit.output_time(2.0), edit.output_time(6.0)) == (2.0, 10.5, 3.0, None)

    got = load_mono(out)
    sr = SAMPLE_RATE

    def level(t0, t1):
        return float(np.sqrt(np.mean(got[int(t0 * sr) : int(t1 * sr)] ** 2)))

    assert level(0.2, 1.8) > 0.05  # intro tone, levelled
    assert level(9.2, 10.4) < 1e-3  # silent outro
    # The talk part is still in sync: output 3.0 s is talk time 2.0 s (intro 2 s, start at 1 s).
    ref = load_mono(fixtures / "camera.wav")
    pos, conf = find_lag(ref[int(2.0 * sr) : int(2.8 * sr)], got[int(2.0 * sr) : int(4.8 * sr)], sr)
    assert conf > 8 and pos == pytest.approx(1.0, abs=0.003)


def test_already_normalized_video_is_copied(tmp_path, capsys):
    src = tmp_path / "ready.mp4"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=1920x1080:rate=30:duration=2",
         "-f", "lavfi", "-i", "sine=duration=2:sample_rate=48000",
         "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(src)],
        check=True,
    )  # fmt: skip
    out = normalize(src, tmp_path / "work", "presenter")
    assert "copying instead of converting" in capsys.readouterr().out
    info = ffmpeg.probe_video(out)
    assert info.color_transfer == "bt709" and (info.width, info.height) == (1920, 1080)
    assert int(streams(out)["streams"][0]["nb_read_frames"]) == 60


@pytest.mark.slow
def test_labels(fixtures):
    tl_path = fixtures / "labels.txt"
    tl_path.write_text(
        "presenter talk.mp4\nslides deck.pdf\nevent Puppet Masters Meetup #42\ndate 1 October 2026\n"
        "speaker Ada Lovelace\ntalk Engines and Ideas\n0:00 presenter\n0:10 slide 2\n0:11 slide 2 side\n"
    )
    out = render(tl_path, load(tl_path), fixtures / "labels.mp4", preview=True)

    def frame(t):
        raw = ffmpeg.run_raw(["-ss", str(t), "-i", str(out), "-frames:v", "1", "-vf", "scale=1920:1080", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"])
        return np.frombuffer(raw, np.uint8).reshape(1080, 1920, 3).astype(int)

    def dark_box(img, x, y, w, h):  # mostly dark pixels: a label box drawn over the bright test pattern
        return (img[y : y + h, x : x + w].mean(axis=2) < 90).mean() > 0.6

    early, late, corner, side = frame(3.0), frame(9.5), frame(10.5), frame(11.5)
    assert dark_box(early, 34, 30, 200, 36)  # event badge on the presenter shot
    assert dark_box(early, 70, 830, 500, 60)  # lower third in the first presenter shot…
    assert not dark_box(late, 70, 830, 500, 60)  # …gone after ~8 s
    assert dark_box(corner, 1376, 995, 400, 26)  # name strip at the bottom of the corner inset
    assert dark_box(side, 1325, 475, 450, 26)  # name strip on the side-by-side inset


def test_screen_outside_recording_shows_presenter():
    from meetaras.render import screen_or_presenter
    from meetaras.timeline import Segment

    segs = [Segment(0, 10, "slide", 1), Segment(10, 100, "screen"), Segment(100, 120, "screen_full")]
    out = screen_or_presenter(segs, 20, 110)  # the recording covers 20–110 s of the talk
    assert [(s.start, s.end, s.layout) for s in out] == [
        (0, 10, "slide"), (10, 20, "presenter"), (20, 100, "screen"), (100, 110, "screen_full"), (110, 120, "presenter"),
    ]


@pytest.mark.slow
def test_audio_without_compression(fixtures):
    from meetaras.render import FINAL, build_audio_graph

    inputs = ["-i", str(fixtures / "talk.mp4")]
    assert "acompressor" in build_audio_graph("aresample=48000", [(0, 300)], FINAL, inputs, index=0)
    graph = build_audio_graph("aresample=48000", [(0, 300)], FINAL, inputs, index=0, compress=False)
    assert "acompressor" not in graph and "alimiter" in graph  # the level and the peak limiter stay


@pytest.mark.slow
def test_4k_masters(hdr_video, uhd_video, tmp_path, monkeypatch, capsys):
    from meetaras.layouts import UHD
    from meetaras.normalize import normalized_path

    runs = []
    run = ffmpeg.run
    monkeypatch.setattr(ffmpeg, "run", lambda args, **kw: runs.append(args) or run(args, **kw))
    # Neither there: one decoding pass makes both, HDR tone-mapped once (at 4K).
    big = normalize(hdr_video, tmp_path / "a", "presenter", UHD.canvas)
    assert len(runs) == 1 and "split=2" in runs[0][runs[0].index("-filter_complex") + 1]
    hd = normalized_path(hdr_video, tmp_path / "a", "presenter")
    for path, size in ((big, (3840, 2160)), (hd, (1920, 1080))):
        info = ffmpeg.probe_video(path)
        assert (info.width, info.height) == size and not info.is_hdr and info.color_transfer == "bt709"
        assert int(streams(path)["streams"][0]["nb_read_frames"]) == 60
    raw = ffmpeg.run_raw(["-i", str(big), "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "gray", "-"])
    assert np.frombuffer(raw, np.uint8).mean() > 40  # tone-mapped, not black
    # The 1080p file there already (a talk switched to 4K): only the 4K one is made.
    normalize(hdr_video, tmp_path / "b", "presenter")
    runs.clear()
    normalize(hdr_video, tmp_path / "b", "presenter", UHD.canvas)
    assert len(runs) == 1 and "-vf" in runs[0]
    # A 4K30 H.264 camera: its 4K master is a copy, picture only; the 1080p file keeps the sound.
    capsys.readouterr()
    big = normalize(uhd_video, tmp_path / "c", "presenter", UHD.canvas)
    assert "already 4K30 H.264 SDR: copying" in capsys.readouterr().out
    assert not ffmpeg.probe_video(big).has_audio
    assert ffmpeg.probe_video(normalize(uhd_video, tmp_path / "c", "presenter")).has_audio


def test_4k_geometry_is_1080p_scaled():
    from meetaras import layouts
    from meetaras.layouts import HD, UHD

    assert UHD.canvas == (3840, 2160)
    for name, (content, pip, inset) in HD.styles.items():
        assert UHD.styles[name] == (content.scaled(2), pip.scaled(2), inset)
    assert (UHD.inset_radius, UHD.shadow_sigma, UHD.shadow_pad, UHD.shadow_offset) == (28, 24, 72, 12)
    graph = layouts.clip_filter("slide", "", decor=(2, 3), g=UHD)
    assert "s=3840x2160" in graph and "scale=960:540" in graph and "overlay=2736:1518" in graph  # the corner inset
    assert "overlay=2664:1458" in graph  # its shadow: 72 px around, 12 px down
    assert layouts.clip_filter("slide", "", (1280, 720), g=UHD).endswith("scale=1280:720:flags=bicubic," + layouts.BT709 + "[v]")
