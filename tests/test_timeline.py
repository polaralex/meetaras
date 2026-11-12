from pathlib import Path

import pytest

from meetaras.timeline import Segment, TimelineError, format_time, parse, parse_time

BASE = Path("/talks/demo")

SAMPLE = """\
presenter  talk.mov
audio      mic.wav          # optional
slides     deck.pdf
audio_offset 0.25

0:00   start
0:12   presenter
0:45   slide 1
2:10   slide 2
5:30   presenter
6:15   slide 3
41:20  end
"""


@pytest.mark.parametrize(
    "text,expected",
    [("12", 12), ("0:12", 12), ("2:10.5", 130.5), ("1:02:03", 3723), ("45.25", 45.25), ("10:00", 600)],
)
def test_parse_time(text, expected):
    assert parse_time(text) == expected


@pytest.mark.parametrize("text", ["1:75", "1:60:00", "abc", "1::2", "-3"])
def test_parse_time_rejects(text):
    with pytest.raises(ValueError):
        parse_time(text)


def test_format_time_roundtrip():
    for t in (0, 12, 130.5, 3723, 59.125):
        assert parse_time(format_time(t)) == t


def test_parse_sample():
    tl = parse(SAMPLE, BASE)
    assert tl.presenter == BASE / "talk.mov"
    assert tl.audio == BASE / "mic.wav"
    assert tl.audio_offset == 0.25
    assert tl.start == 0 and tl.end == 41 * 60 + 20
    segs = tl.segments(video_duration=45 * 60)
    assert segs == [
        Segment(0, 45, "presenter"),
        Segment(45, 130, "slide", 1),
        Segment(130, 330, "slide", 2),
        Segment(330, 375, "presenter"),
        Segment(375, 2480, "slide", 3),
    ]


def test_defaults_and_opening_layout():
    tl = parse("presenter a.mov\nslides d.pdf\n0:05 slide 2\n0:20 start\n0:30 presenter\n", BASE)
    # no end → video end; the slide set before start is shown from start
    assert tl.segments(60) == [Segment(20, 30, "slide", 2), Segment(30, 60, "presenter")]


def test_full_and_screen_layouts():
    tl = parse(
        "presenter a.mov\nslides d.pdf\nscreen s.mov\nscreen_offset 12.5\n"
        "0:10 slide 2 full\n0:20 slide 2\n0:25 slide 2 side\n0:30 screen\n0:35 screen side\n0:40 screen full\n"
        "0:50 presenter\n",
        BASE,
    )
    assert tl.screen == BASE / "s.mov" and tl.screen_offset == 12.5
    assert [(s.layout, s.slide, s.label) for s in tl.segments(60)] == [
        ("presenter", None, "presenter"),
        ("slide_full", 2, "slide 2 full"),
        ("slide", 2, "slide 2"),
        ("slide_side", 2, "slide 2 side"),
        ("screen", None, "screen"),
        ("screen_side", None, "screen side"),
        ("screen_full", None, "screen full"),
        ("presenter", None, "presenter"),
    ]
    assert [(s.kind, s.style) for s in tl.segments(60)[1:4]] == [("slide", "full"), ("slide", ""), ("slide", "side")]
    again = parse(tl.to_text(), BASE)
    assert again.segments(60) == tl.segments(60) and again.screen_offset == 12.5


def test_cut():
    tl = parse("presenter a.mov\nslides d.pdf\n0:10 slide 1\n0:20 cut\n0:30 slide 2\n", BASE)
    assert [(s.start, s.end, s.label) for s in tl.segments(40)] == [
        (0, 10, "presenter"), (10, 20, "slide 1"), (20, 30, "cut"), (30, 40, "slide 2"),
    ]
    assert "0:20     cut" in tl.to_text()


def test_changes_after_end_are_ignored():
    tl = parse("presenter a.mov\nslides d.pdf\n0:10 slide 1\n0:20 end\n0:30 slide 2\n", BASE)
    assert tl.segments(60) == [Segment(0, 10, "presenter"), Segment(10, 20, "slide", 1)]


def test_presenter_before_first_command():
    tl = parse("presenter a.mov\nslides d.pdf\n0:10 slide 1\n", BASE)
    assert tl.segments(20) == [Segment(0, 10, "presenter"), Segment(10, 20, "slide", 1)]


def test_repeated_same_layout_merges():
    tl = parse("presenter a.mov\nslides d.pdf\n0:10 slide 1\n0:15 slide 1\n", BASE)
    assert tl.segments(20) == [Segment(0, 10, "presenter"), Segment(10, 20, "slide", 1)]


@pytest.mark.parametrize(
    "body,line,fragment",
    [
        ("0:10 slide 1\n0:05 presenter\n", 4, "before the previous"),
        ("0:10 slides 1\n", 3, "unknown command"),
        ("0:10 slide x\n", 3, "page number"),
        ("0:10 slide 0\n", 3, "page number"),
        ("1:99 presenter\n", 3, "invalid time"),
        ("0:01 start\n0:02 start\n", 4, "twice"),
        ("crop 1 2 3 4\n", 3, "unknown setting"),
        ("pip_center 0.5\n", 3, "unknown setting"),
        ("0:10 slide 2 big\n", 3, "`full` or `speaker`"),
        ("0:10 screen\n", 3, "screen <recording>"),
        ("screen s.mov\n0:10 screen half\n", 4, "screen side"),
        ("presenter b.mov\n", 3, "set twice"),
    ],
)
def test_errors_report_line(body, line, fragment):
    with pytest.raises(TimelineError) as exc:
        parse("presenter a.mov\nslides d.pdf\n" + body, BASE)
    assert exc.value.line == line
    assert fragment in str(exc.value)


def test_missing_required():
    with pytest.raises(TimelineError, match="slides"):
        parse("presenter a.mov\n", BASE)


def test_validate_slides():
    tl = parse("presenter a.mov\nslides d.pdf\n0:10 slide 4\n", BASE)
    with pytest.raises(TimelineError, match="3 pages") as exc:
        tl.validate_slides(3)
    assert exc.value.line == 3


def test_end_beyond_video():
    tl = parse("presenter a.mov\nslides d.pdf\n0:50 end\n", BASE)
    with pytest.raises(TimelineError, match="beyond"):
        tl.segments(30)


def test_hash_in_filename_is_not_comment():
    tl = parse("presenter talk#2.mov\nslides d.pdf # deck\n", BASE)
    assert tl.presenter.name == "talk#2.mov"
    assert tl.slides.name == "d.pdf"


def test_notes_survive_rewrite():
    tl = parse("presenter a.mov\nslides d.pdf\n0:10 slide 1   # match 0.40  CHECK\n", BASE)
    assert tl.events[0].note == "match 0.40  CHECK"
    assert parse(tl.to_text(), BASE).events[0].note == "match 0.40  CHECK"


def test_event_info_and_hash_in_text():
    tl = parse(
        "#comment without space\npresenter a.mov\nslides d.pdf\nevent Puppet Masters Meetup #42   # the meetup\n"
        "date 1 October 2026\nspeaker Υπατία Αλεξανδρινή\ntalk Engines and Ideas\nlower_third_every 10\n",
        BASE,
    )
    assert tl.event == "Puppet Masters Meetup #42"
    assert tl.info.event_line() == "Puppet Masters Meetup #42 · 1 October 2026"
    assert tl.info.who_line() == "Υπατία Αλεξανδρινή · Engines and Ideas"
    again = parse(tl.to_text(), BASE)
    assert again.info == tl.info and again.lower_third_every == 10


def test_to_text_roundtrip():
    tl = parse(SAMPLE, BASE)
    again = parse(tl.to_text(), BASE)
    assert again.segments(45 * 60) == tl.segments(45 * 60)
    assert again.audio == tl.audio and again.audio_offset == tl.audio_offset


def test_compression_switch():
    assert parse("presenter a.mov\nslides d.pdf\n", BASE).compression
    tl = parse("presenter a.mov\nslides d.pdf\ncompression off\n", BASE)
    assert not tl.compression and "compression off" in tl.to_text()
    assert "compression" not in parse("presenter a.mov\nslides d.pdf\ncompression on\n", BASE).to_text()
    with pytest.raises(TimelineError, match="on or off"):
        parse("presenter a.mov\nslides d.pdf\ncompression maybe\n", BASE)


def test_resolution_setting():
    tl = parse("presenter a.mov\nslides d.pdf\n", BASE)
    assert tl.resolution == "1080p" and "resolution" not in tl.to_text()  # existing talks: unchanged
    tl = parse("presenter a.mov\nslides d.pdf\ncompression off\nresolution 4K\n", BASE)
    assert tl.resolution == "4k"
    assert tl.to_text().splitlines()[2:4] == ["compression off", "resolution 4k"]  # next to the other settings
    assert parse(tl.to_text(), BASE).resolution == "4k"
    assert "resolution" not in parse("presenter a.mov\nslides d.pdf\nresolution 1080p\n", BASE).to_text()
    with pytest.raises(TimelineError, match="line 3: resolution must be 1080p or 4k, got '720p'"):
        parse("presenter a.mov\nslides d.pdf\nresolution 720p\n", BASE)
