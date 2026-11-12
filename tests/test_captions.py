import json

import pytest

from meetaras.captions import MAX_LINE, Word, build_cues, srt_time, to_output, vocabulary, words_from_json, write_srt
from meetaras.render import Edit, FrameSegment
from meetaras.timeline import Segment


def words(text, start=0.0, step=0.4):
    return [Word(start + i * step, start + i * step + 0.3, w) for i, w in enumerate(text.split())]


def test_to_output_handles_trim_cuts_and_intro():
    ws = words("one two three four five six seven eight nine ten", start=0.0, step=1.0)  # word i at i s
    # keep 1–4 s and 6–9 s (frames at 30 fps), with a 2 s intro in front
    edit = Edit([FrameSegment(30, 120, Segment(1, 4, "presenter")), FrameSegment(180, 270, Segment(6, 9, "presenter"))], 2.0)
    out = to_output(ws, edit)
    assert [w.text for w, _ in out] == ["two", "three", "four", "seven", "eight", "nine"]
    assert out[0][0].start == pytest.approx(2.0)  # "two" at talk 1 s → after the 2 s intro
    assert out[3][0].start == pytest.approx(5.0)  # "seven" at talk 6 s → 2 + 3 s kept before the cut
    assert out[3][0].start == pytest.approx(edit.output_time(6.0))  # the same map as the chapters
    assert [new for _, new in out] == [True, False, False, True, False, False]


def test_cues_respect_line_length_pauses_and_cuts():
    long_text = "this is a fairly long sentence that keeps going and going so that it needs more than one caption to fit"
    ws = [(w, i == 0) for i, w in enumerate(words(long_text))]
    ws += [(w, False) for w in words("after a pause", start=60.0)]
    ws += [(w, i == 0) for i, w in enumerate(words("after a cut", start=62.0))]
    cues = build_cues(ws)
    assert all(len(line) <= MAX_LINE for c in cues for line in c.lines)
    # a sentence with long words that can't be split evenly still never exceeds the line length
    awkward = [(w, False) for w in words("Why is it different? Because AIOps normally refers to doing operations with using AI.")]
    assert all(len(line) <= MAX_LINE for c in build_cues(awkward) for line in c.lines)
    assert all(len(c.lines) <= 2 for c in cues)
    texts = [" ".join(c.lines) for c in cues]
    assert "after a pause" in texts and "after a cut" in texts
    assert all(c.end > c.start for c in cues)
    assert all(a.end <= b.start for a, b in zip(cues, cues[1:]))


def test_words_from_whisper_json(tmp_path):
    data = {"transcription": [{"tokens": [
        {"text": "[_BEG_]", "offsets": {"from": 0, "to": 0}},
        {"text": " Hello", "offsets": {"from": 100, "to": 400}, "t_dtw": 12},
        {"text": " M", "offsets": {"from": 500, "to": 600}, "t_dtw": 52},
        {"text": "LOps", "offsets": {"from": 600, "to": 900}, "t_dtw": 60},
        {"text": ".", "offsets": {"from": 900, "to": 950}, "t_dtw": 90},
    ]}]}
    p = tmp_path / "t.json"
    p.write_text(json.dumps(data))
    ws = words_from_json(p)
    assert [w.text for w in ws] == ["Hello", "MLOps."]
    assert ws[0].start == pytest.approx(0.12) and ws[1].start == pytest.approx(0.52)
    assert ws[1].end == pytest.approx(0.95)


def _tok(text, ms, dtw):
    return {"text": text, "offsets": {"from": ms, "to": ms}, "t_dtw": dtw}


def test_words_on_one_instant_are_spread_over_the_gap(tmp_path):
    # Seen on a real talk: whisper put a whole sentence on the end of its window; speech resumes 3 s later.
    seg1 = {"text": " Next. It might be on SD cards.", "tokens": [_tok(" Next", 0, 0), _tok(".", 300, 30)]
            + [_tok(f" {w}", 1000, 100) for w in "It might be on SD cards.".split()]}  # fmt: skip
    seg2 = {"text": " Ingestion.", "tokens": [_tok(" Ingestion.", 1000, 400)]}
    p = tmp_path / "t.json"
    p.write_text(json.dumps({"transcription": [seg1, seg2]}))
    ws = words_from_json(p)
    starts = [w.start for w in ws]
    assert starts == sorted(starts) and len(set(starts)) == len(starts)
    assert starts[1] == pytest.approx(1.0) and starts[-2] < 4.0 and starts[-1] == pytest.approx(4.0)
    assert all(w.end <= nxt.start for w, nxt in zip(ws, ws[1:]))
    cues = build_cues([(w, False) for w in ws])
    assert all(a.end <= b.start for a, b in zip(cues, cues[1:]))


def test_repetition_loops_are_dropped(tmp_path):
    def seg(text, t):
        return {"text": f" {text}", "tokens": [_tok(f" {w}", t, t // 10 + i * 20) for i, w in enumerate(text.split())]}

    loop = [seg("Let's see, of course.", 0), seg("Is that good?", 1000), seg("Let's see, of course.", 2000),
            seg("Is that good?", 3000), seg("Okay.", 4000), seg("Is that good?", 30000)]  # fmt: skip
    p = tmp_path / "t.json"
    p.write_text(json.dumps({"transcription": loop}))
    text = " ".join(w.text for w in words_from_json(p))
    assert text == "Let's see, of course. Is that good? Okay. Is that good?"  # the last one is 30 s later


def test_srt_format(tmp_path):
    cues = build_cues([(w, False) for w in words("hello world")])
    write_srt(cues, tmp_path / "x.srt")
    text = (tmp_path / "x.srt").read_text()
    assert text.startswith("1\n00:00:00,000 --> 00:00:01,") and "hello world" in text
    assert srt_time(3725.5) == "01:02:05,500"


def test_vocabulary_picks_distinctive_terms(fixtures):
    terms = vocabulary(fixtures / "deck.pdf", ["kagent"])
    assert terms[0] == "kagent" and "Slide" in terms
