"""YouTube metadata (`meetaras metadata` → youtube.json; the texts to paste go to output/).

Chapters need no AI: the slide changes of the final edit in output time (trim, cuts and intro
included, like captions), named from the slide titles and merged to YouTube's rules (the first at 0:00,
at least 3, each at least 10 s). When Claude is available it writes title options, a description,
tags and better chapter names from the transcript, the slides and the video info: through the Claude
Code CLI (`claude -p`, tools and MCP servers off, so it only reads what is passed in), or the API
with an API key.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

from . import cache, timeline
from .captions import to_output, transcript_path, words_from_json
from .normalize import normalized_path
from .render import Edit, the_edit
from .slides import page_texts, page_titles

METADATA_VERSION = 2  # bump when the prompt or the chapter rules change
ABOUT = "about.txt"  # optional, in the talk folder: the talk's abstract, links, anything the organizer knows
CHAPTER_MIN = 10.0  # seconds; YouTube ignores chapters that are shorter
CHAPTERS_MIN = 3  # YouTube shows no chapters with fewer
API_MODEL = "claude-opus-5-5"
TRANSCRIPT_LINE = 30.0  # seconds of transcript per timestamped line in the prompt

SCHEMA = {
    "type": "object",
    "properties": {
        "titles": {"type": "array", "items": {"type": "string"}, "description": "2 or 3 title options, best first"},
        "description": {"type": "string"},
        "tags": {"type": "array", "items": {"type": "string"}},
        "chapters": {"type": "array", "items": {"type": "string"}, "description": "one name per given chapter, same order"},
    },
    "required": ["titles", "description", "tags", "chapters"],
    "additionalProperties": False,
}
SYSTEM = """You write YouTube metadata for recordings of tech talks at meetups and conferences. You get
the video info, the slide text, the chapters with their times and default names, and the transcript
(automatic speech recognition: names and terms may be misspelled; the slides spell them right).
There may also be the organizer's notes about the talk (its abstract, links): trust them for names,
spelling and what the talk is about. Write in the language of the talk. Be factual: only claim what
the talk and the notes say. No hype, no emoji, no clickbait, no hashtags.

- titles: 2 or 3 options, at most 70 characters each, best first. Clear and searchable: what the talk
  is about; the talk's own title is a good option if it is clear.
- description: a 2-4 sentence summary, then "What you'll learn:" with 3-6 short bullet points ("- "),
  then who gave the talk (speaker, role) at which event and when, as far as the info says. No chapter
  list (it is added separately). Links only if the organizer's notes give them: then end with them,
  one per line as "Label: URL", exactly as given.
- tags: 8-15 tags: topics, technologies and tools named in the talk, the event, the speaker.
- chapters: one name per chapter you were given, in the same order, at most 50 characters: what that
  part of the talk is about (the slide title is only a hint). Use an empty string to merge a chapter
  into the one before it when they belong together; aim for about one chapter per 3-6 minutes."""


class MetadataError(RuntimeError):
    pass


@dataclass
class Chapter:
    time: float  # output time, seconds
    title: str


def slide_texts(pdf: Path, limit: int = 500) -> list[str]:
    return [" ".join(text.split())[:limit] for text in page_texts(pdf)]


def fmt(t: float) -> str:
    """Chapter times as YouTube reads them: m:ss or h:mm:ss, whole seconds."""
    s = int(t)
    return f"{s // 3600}:{s // 60 % 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"


def merge(chapters: list[Chapter], total: float) -> list[Chapter]:
    """YouTube's rules: the first at 0:00, each at least CHAPTER_MIN long, at least CHAPTERS_MIN of them."""
    out: list[Chapter] = []
    for c in chapters:
        if out and c.title == out[-1].title:
            continue
        if out and c.time - out[-1].time < CHAPTER_MIN:
            out[-1] = Chapter(out[-1].time, c.title)  # a slide only passed through: the one that stays names it
            if len(out) > 1 and out[-1].title == out[-2].title:
                out.pop()
            continue
        out.append(Chapter(0.0, c.title) if not out else c)
    while len(out) > 1 and total - out[-1].time < CHAPTER_MIN:
        out.pop()
    return out if len(out) >= CHAPTERS_MIN else []


def chapters(tl: timeline.Timeline, edit: Edit, titles: list[str]) -> list[Chapter]:
    """A chapter wherever the edit moves to another slide (or to a demo); speaker shots continue one."""
    found = [Chapter(0.0, "Intro")] if edit.intro else []
    for fs in edit.segments:
        s = fs.segment
        if s.slide is not None and s.kind == "slide":
            name = titles[s.slide - 1] if s.slide <= len(titles) and titles[s.slide - 1] else f"Slide {s.slide}"
        elif s.kind == "screen":
            name = "Demo"
        else:
            continue
        t = edit.output_time(s.start)
        if t is not None and (not found or found[-1].title != name):
            found.append(Chapter(t, name))
    if not found or found[0].time >= CHAPTER_MIN:
        found.insert(0, Chapter(0.0, "Introduction"))
    return merge(found, edit.total)


def transcript_lines(transcript: Path | None, edit: Edit) -> list[str]:
    """The transcript in output time, a timestamped line per TRANSCRIPT_LINE seconds."""
    if transcript is None:
        return []
    lines: list[str] = []
    start, words = None, []
    for w, _ in to_output(words_from_json(transcript), edit):
        if start is None or w.start - start >= TRANSCRIPT_LINE:
            if words:
                lines.append(f"[{fmt(start)}] {' '.join(words)}")
            start, words = w.start, []
        words.append(w.text)
    if words:
        lines.append(f"[{fmt(start)}] {' '.join(words)}")
    return lines


def build_prompt(tl: timeline.Timeline, found: list[Chapter], texts: list[str], transcript: list[str], about: str = "") -> str:
    info = "\n".join(f"{k}: {getattr(tl, k)}" for k in timeline.INFO_KEYS if getattr(tl, k))
    language = tl.language if tl.language != "auto" else "the language spoken in the transcript"
    parts = [
        f"Video info:\n{info or '(none given)'}\nLanguage: {language}",
        *([f"Organizer's notes about the talk:\n{about.strip()}"] if about.strip() else []),
        "Chapters (time, default name):\n" + "\n".join(f"{i + 1}. {fmt(c.time)} {c.title}" for i, c in enumerate(found)),
        "Slides:\n" + "\n".join(f"Slide {i + 1}: {t}" for i, t in enumerate(texts) if t),
        "Transcript (times in the video):\n" + ("\n".join(transcript) if transcript else "(no transcript: use the slides)"),
    ]
    return "\n\n".join(parts)


def claude_available() -> str | None:
    """How Claude would be asked: "claude" (Claude Code CLI), "api" (API key), or None (MEETARAS_CLAUDE=0: never)."""
    if os.environ.get("MEETARAS_CLAUDE") == "0":
        return None
    if shutil.which("claude"):
        return "claude"
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "api"
    return None


def ask_claude(prompt: str) -> dict:
    how = claude_available()
    if how == "claude":
        with tempfile.TemporaryDirectory() as empty:  # nothing to read but the prompt: no project files, no CLAUDE.md
            cmd = ["claude", "-p", "--tools", "", "--output-format", "json", "--json-schema", json.dumps(SCHEMA),
                   "--no-session-persistence", "--strict-mcp-config", "--system-prompt", SYSTEM]  # fmt: skip
            try:
                proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True, cwd=empty, timeout=900)
            except subprocess.TimeoutExpired:
                raise MetadataError("Claude didn't answer within 15 minutes") from None
        try:
            data = json.loads(proc.stdout)
        except json.JSONDecodeError:
            raise MetadataError(f"claude -p failed (exit {proc.returncode}): {(proc.stderr or proc.stdout)[-500:]}") from None
        if data.get("is_error") or not isinstance(data.get("structured_output"), dict):
            raise MetadataError(f"claude -p: {data.get('result') or data.get('subtype')}")
        return data["structured_output"]
    if how == "api":
        try:
            import anthropic
        except ImportError:
            raise MetadataError("ANTHROPIC_API_KEY is set, but the anthropic package isn't installed (uv sync --extra api)") from None
        client = anthropic.Anthropic()
        try:
            response = client.beta.messages.create(
                model=API_MODEL, max_tokens=16000, system=SYSTEM,
                messages=[{"role": "user", "content": prompt}],
                output_config={"format": {"type": "json_schema", "schema": SCHEMA}},
                betas=["server-side-fallback-2026-07-01"], fallbacks="default",  # another model takes over a refusal
            )  # fmt: skip
        except anthropic.APIError as exc:
            raise MetadataError(f"Claude API: {exc}") from None
        if response.stop_reason == "refusal":
            raise MetadataError("Claude declined to write the metadata")
        if response.stop_reason == "max_tokens":
            raise MetadataError("Claude's answer was cut off (max_tokens)")
        text = next((b.text for b in response.content if b.type == "text"), None)
        if text is None:
            raise MetadataError(f"Claude API: no text in the answer (stop reason {response.stop_reason})")
        return json.loads(text)
    raise MetadataError("Claude isn't available (install Claude Code or set ANTHROPIC_API_KEY)")


def apply_names(found: list[Chapter], names: list[str], total: float) -> list[Chapter]:
    """Claude's chapter names; an empty name merges the chapter into the one before."""
    if not found or len(names) != len(found):
        return found
    named = [Chapter(c.time, " ".join(n.split())[:80]) for c, n in zip(found, names) if n.strip() or c is found[0]]
    if not named[0].title:
        named[0] = Chapter(0.0, found[0].title)
    return merge(named, total) or found


def to_text(data: dict) -> str:
    """The YouTube text: each field ready to paste; the chapters go at the end of the description."""
    lines = ["TITLE", data["title"], ""]
    others = [t for t in data["titles"] if t != data["title"]]
    if others:
        lines += ["Other title options:", *[f"- {t}" for t in others], ""]
    lines += ["DESCRIPTION", description_with_chapters(data), "", "TAGS", ", ".join(data["tags"]), ""]
    return "\n".join(lines)


def to_markdown(data: dict) -> str:
    """Everything as one Markdown text; each field in its own plain-text block, so it keeps its line
    breaks when copied on to YouTube."""

    def block(text: str) -> str:
        return f"```text\n{text.strip()}\n```"

    title = data["title"].strip()
    chapters = "\n".join(f"{fmt(c['time'])} {c['title']}" for c in data["chapters"])
    parts = [f"# {title}", "## Title", block(title)]
    others = [t for t in data["titles"] if t != data["title"]]
    if others:
        parts += ["## Other title options", "\n".join(f"- {t}" for t in others)]
    parts += ["## Description (with chapters, as it goes into YouTube)", block(description_with_chapters(data))]
    if chapters:
        parts += ["## Chapters", block(chapters)]
    parts += ["## Tags", block(", ".join(data["tags"]))]
    return "\n\n".join(parts) + "\n"


def description_with_chapters(data: dict) -> str:
    chapter_lines = "\n".join(f"{fmt(c['time'])} {c['title']}" for c in data["chapters"])
    return "\n\n".join(x for x in (data["description"].strip(), chapter_lines) if x)


def state_file(tl_file: Path) -> Path:
    """youtube.json in the talk folder: the metadata, and whether you edited it."""
    return tl_file.parent / "youtube.json"


def save(tl_file: Path, data: dict) -> None:
    """youtube.json, and the texts to paste in output/ (… - YouTube.txt and .md)."""
    from . import deliver

    state_file(tl_file).write_text(json.dumps(data, indent=1, ensure_ascii=False))
    deliver.write_texts(tl_file, to_text(data), to_markdown(data))


def load(tl_file: Path) -> dict | None:
    try:
        return json.loads(state_file(tl_file).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def inputs(tl_file: Path) -> tuple[timeline.Timeline, Edit, list[Chapter], str, str]:
    """What the metadata is made from now: (timeline, edit, chapters, prompt, its cache key)."""
    tl = timeline.load(tl_file)
    work = tl_file.parent / ".work"
    edit = the_edit(tl, work)  # (never converts: the app asks this to see whether the text is stale)
    found = chapters(tl, edit, page_titles(tl.slides))
    about = tl_file.parent / ABOUT
    prompt = build_prompt(
        tl, found, slide_texts(tl.slides), transcript_lines(transcript_path(tl, normalized_path(tl.presenter, work, "presenter"), work), edit),
        about.read_text(encoding="utf-8", errors="replace") if about.is_file() else "",
    )
    return tl, edit, found, prompt, cache.key("metadata", METADATA_VERSION, prompt)


def generate(tl_file: Path, ai: bool = True, force: bool = True) -> tuple[dict, str]:
    """Write the metadata (see save); returns (data, what happened).

    Without `force` (a make run), metadata the user edited is left alone, and nothing is redone
    when the edit, transcript, slides and info are unchanged.
    """
    old = load(tl_file)
    if not force and old and old.get("edited"):
        return old, "kept the YouTube text as you edited it"
    tl, edit, found, prompt, key = inputs(tl_file)
    if not force and old and old.get("made_from") == key and (old.get("ai") or not ai or not claude_available()):
        return old, "the YouTube text is up to date"
    title = tl.talk or tl_file.parent.name
    data = {
        "made_from": key, "ai": None, "edited": False, "error": None,
        "titles": [title], "title": title, "description": default_description(tl), "tags": [],
        "chapters": [asdict(c) for c in found],
    }  # fmt: skip
    how = claude_available() if ai else None
    if how:
        work = tl_file.parent / ".work"
        cached = work / f"metadata-{key}.json"  # one Claude call per set of inputs
        try:
            if cached.exists():
                answer = json.loads(cached.read_text())
            else:
                answer = ask_claude(prompt)
                cached.write_text(json.dumps(answer, ensure_ascii=False))
            titles = [" ".join(t.split()) for t in answer["titles"] if t.strip()][:3] or [title]
            data.update(
                ai=how, titles=titles, title=titles[0], description=answer["description"].strip(),
                tags=[t.strip() for t in answer["tags"] if t.strip()],
                chapters=[asdict(c) for c in apply_names(found, answer["chapters"], edit.total)],
            )  # fmt: skip
        except (MetadataError, KeyError, TypeError, json.JSONDecodeError) as exc:
            data["error"] = str(exc) or type(exc).__name__
    save(tl_file, data)
    n = len(data["chapters"])
    what = f"{n} chapters" if n else "no chapters (fewer than 3 slide changes)"
    if data["ai"]:
        return data, f"YouTube text: titles, description and tags by Claude, {what}"
    return data, f"YouTube text: {what}" + (f"; Claude failed: {data['error']}" if data["error"] else "; no Claude, so no title or description")


def default_description(tl: timeline.Timeline) -> str:
    who = ", ".join(x for x in (tl.speaker, tl.role) if x)
    where = ", ".join(x for x in (tl.event, tl.date) if x)
    lines = [tl.talk, " · ".join(x for x in (who, where) if x)]
    return "\n".join(x for x in lines if x)
