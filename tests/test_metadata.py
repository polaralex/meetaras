"""YouTube metadata: chapters from the edit, the prompt, Claude through the CLI or the API, the YouTube text."""

import json
import sys
import types
from pathlib import Path

import pytest

from meetaras import make, metadata
from meetaras.render import Edit, kept_segments
from meetaras.timeline import parse

from .conftest import whisper_json

ANSWER = {
    "titles": ["Engines and Ideas", "How the Analytical Engine Thinks"],
    "description": "A talk about engines.\n\nWhat you'll learn:\n- Gears",
    "tags": ["engines", "history"],
    "chapters": ["Welcome", "The engine", "", "Demo time"],
}


def edit_of(text, duration=600.0, intro=20.0):
    tl = parse(text, Path("/talks/x"))
    return tl, Edit(kept_segments(tl.segments(duration)), intro)


TALK = """presenter t.mov
slides d.pdf
screen s.mov

0:05 start
0:10 slide 1
1:00 slide 2
1:30 slide 2 speaker
2:00 cut
2:30 slide 3
4:00 presenter
5:00 screen
6:00 end
"""


def test_chapters_follow_the_edit():
    tl, edit = edit_of(TALK)
    found = metadata.chapters(tl, edit, ["Welcome", "The Engine", "Results"])
    # intro 20 s; the talk starts at 0:05; 0:30 cut out before slide 3; speaker shots continue a chapter
    assert [(metadata.fmt(c.time), c.title) for c in found] == [
        ("0:00", "Intro"), ("0:25", "Welcome"), ("1:15", "The Engine"), ("2:15", "Results"), ("4:45", "Demo"),
    ]  # fmt: skip
    assert edit.total == 20 + 325


def test_youtube_rules():
    C = metadata.Chapter
    # a slide that only passed through gives its place to the next; the last needs 10 s before the end
    got = metadata.merge([C(0, "A"), C(30, "B"), C(34, "C"), C(80, "C"), C(120, "D"), C(195, "E")], 200)
    assert [(c.time, c.title) for c in got] == [(0, "A"), (30, "C"), (120, "D")]
    assert metadata.merge([C(3, "A"), C(40, "B"), C(90, "C")], 200)[0].time == 0  # the first is always at 0:00
    assert metadata.merge([C(0, "A"), C(40, "B")], 200) == []  # fewer than 3: YouTube shows none


def test_prompt_has_the_transcript_in_output_time(tmp_path):
    tl, edit = edit_of(TALK)
    transcript = tmp_path / "t.json"
    transcript.write_text(json.dumps(whisper_json([("Hello", 12_000), ("again", 160_000)])))  # talk time 0:12, 2:40
    lines = metadata.transcript_lines(transcript, edit)
    assert lines == ["[0:27] Hello", "[2:25] again"]
    found = metadata.chapters(tl, edit, ["Welcome", "The Engine", "Results"])
    prompt = metadata.build_prompt(tl, found, ["Welcome text"], lines)
    assert "2. 0:25 Welcome" in prompt and "Slide 1: Welcome text" in prompt and "[2:25] again" in prompt
    assert "Organizer's notes" not in prompt
    prompt = metadata.build_prompt(tl, found, ["Welcome text"], lines, "Abstract: gears.\nSlides: https://example.org/s")
    assert "Organizer's notes about the talk:\nAbstract: gears.\nSlides: https://example.org/s" in prompt


def test_claudes_chapter_names_merge():
    C = metadata.Chapter
    found = [C(0, "Intro"), C(60, "Slide 2"), C(120, "Slide 3"), C(200, "Demo")]
    got = metadata.apply_names(found, ANSWER["chapters"], 400)
    assert [(c.time, c.title) for c in got] == [(0, "Welcome"), (60, "The engine"), (200, "Demo time")]
    assert metadata.apply_names(found, ["a", "b"], 400) == found  # a wrong count: keep the defaults


@pytest.fixture
def made(recordings, seed_caches, tmp_path):
    tl_file = make.new_talk(tmp_path / "talk", recordings / "IMG_0001.MOV", recordings / "deck.pdf", talk="Engines")
    seed_caches(tl_file.parent)
    make.make(tl_file, review=True)  # converted and drafted (from the caches: in a second)
    return tl_file


def test_without_claude_and_edits_kept(made):
    data, detail = metadata.generate(made)
    assert not data["ai"] and data["title"] == "Engines" and "no Claude" in detail
    text = (made.parent / "output" / "Engines - YouTube.txt").read_text()
    assert text.startswith("TITLE\nEngines\n") and "DESCRIPTION" in text and "TAGS" in text
    assert metadata.generate(made, force=False)[1] == "the YouTube text is up to date"
    (made.parent / "about.txt").write_text("The talk's abstract.")  # new notes: the text is written again
    assert metadata.generate(made, force=False)[1] != "the YouTube text is up to date"
    data["edited"] = True
    metadata.save(made, data)
    assert metadata.generate(made, force=False)[1] == "kept the YouTube text as you edited it"
    assert (made.parent / "output" / "Engines - YouTube.md").read_text() == metadata.to_markdown(data)


def test_claude_code_cli(made, tmp_path, monkeypatch):
    """A stand-in `claude` on PATH that checks how it is called and answers like `claude -p --output-format json`."""
    calls = tmp_path / "calls.txt"
    fake = tmp_path / "bin" / "claude"
    fake.parent.mkdir()
    fake.write_text(f"""#!{sys.executable}
import json, os, sys
args = sys.argv[1:]
assert args[:4] == ["-p", "--tools", "", "--output-format"] and "--no-session-persistence" in args and "--strict-mcp-config" in args, args
schema = json.loads(args[args.index("--json-schema") + 1])
assert schema["required"] == ["titles", "description", "tags", "chapters"]
prompt = sys.stdin.read()
assert "Slide 1: Engines and Ideas" in prompt and not os.listdir(".")  # run in an empty folder
open({str(calls)!r}, "a").write("call\\n")
print(json.dumps({{"type": "result", "is_error": False, "structured_output": {json.dumps(ANSWER)}}}))
""")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake.parent}:{__import__('os').environ['PATH']}")
    monkeypatch.delenv("MEETARAS_CLAUDE")
    data, detail = metadata.generate(made)
    assert data["ai"] == "claude" and data["titles"] == ANSWER["titles"] and data["tags"] == ["engines", "history"]
    assert "by Claude" in detail
    assert "Other title options:\n- How the Analytical Engine Thinks" in (made.parent / "output" / "Engines - YouTube.txt").read_text()
    metadata.generate(made)  # the same inputs: the answer is cached
    assert calls.read_text() == "call\n"


def test_api_key_fallback(made, monkeypatch):
    sent = {}

    class Messages:
        def create(self, **kwargs):
            sent.update(kwargs)
            block = types.SimpleNamespace(type="text", text=json.dumps(ANSWER))
            return types.SimpleNamespace(stop_reason="end_turn", content=[block])

    fake = types.ModuleType("anthropic")
    fake.APIError = type("APIError", (Exception,), {})
    fake.Anthropic = lambda: types.SimpleNamespace(beta=types.SimpleNamespace(messages=Messages()))
    monkeypatch.setitem(sys.modules, "anthropic", fake)
    monkeypatch.delenv("MEETARAS_CLAUDE")
    which = metadata.shutil.which
    monkeypatch.setattr(metadata.shutil, "which", lambda name, *a, **k: None if name == "claude" else which(name, *a, **k))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    data, _ = metadata.generate(made)
    assert data["ai"] == "api" and data["description"].startswith("A talk about engines.")
    assert sent["model"] == "claude-opus-5-5" and sent["output_config"]["format"]["schema"] == metadata.SCHEMA
    assert sent["fallbacks"] == "default"


@pytest.mark.parametrize("stop, error", [("max_tokens", "cut off"), ("end_turn", "no text in the answer")])
def test_api_answer_without_text(monkeypatch, stop, error):
    class Messages:
        def create(self, **kwargs):
            return types.SimpleNamespace(stop_reason=stop, content=[types.SimpleNamespace(type="thinking")])

    fake = types.ModuleType("anthropic")
    fake.APIError = type("APIError", (Exception,), {})
    fake.Anthropic = lambda: types.SimpleNamespace(beta=types.SimpleNamespace(messages=Messages()))
    monkeypatch.setitem(sys.modules, "anthropic", fake)
    monkeypatch.setattr(metadata, "claude_available", lambda: "api")
    with pytest.raises(metadata.MetadataError, match=error):
        metadata.ask_claude("prompt")


@pytest.mark.slow
def test_make_writes_the_youtube_text(made):
    make.make(made)
    status = make.read_status(made)
    assert status["steps"]["metadata"]["state"] == "done"
    assert (made.parent / "output" / "Engines - YouTube.txt").is_file() and not (made.parent / "youtube.txt").exists()


def test_metadata_command_prints_the_text(made):
    from typer.testing import CliRunner

    from meetaras.cli import app

    result = CliRunner().invoke(app, ["metadata", str(made.parent), "--no-ai"])
    assert result.exit_code == 0, result.output
    assert "TITLE\nEngines\n" in result.output and "Engines - YouTube.txt" in result.output
    assert not (made.parent / "youtube.txt").exists()
