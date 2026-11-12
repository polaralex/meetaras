"""The finished files of a talk, in its `output/` folder and named after the talk:
`<Title> - <Speaker>.mp4`, the `.srt` with the same name, `… - YouTube.txt` and `… - YouTube.md`.

The talk folder keeps the working files (timeline, caches, previews); `output/` holds only what goes
to YouTube. Meetaras remembers which files it put there (.work/output.json): when the title or speaker
changes, the next file of the same kind replaces the old-named one. Anything else in the folder is
never touched.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from . import timeline

OUTPUT_DIR = "output"
MANIFEST = "output.json"  # in .work/: {kind: file name} of what Meetaras wrote into output/
KINDS = {"video": ".mp4", "srt": ".srt", "youtube_txt": " - YouTube.txt", "youtube_md": " - YouTube.md"}
_UNSAFE = re.compile(r'[\\/:*?"<>|\x00-\x1f]+')  # not allowed in file names on macOS, Windows or Linux


def base_name(tl: timeline.Timeline, folder: Path) -> str:
    """`<Title> - <Speaker>` from the video info, safe as a file name; the folder name without info."""
    name = " - ".join(x for x in (tl.talk, tl.speaker) if x.strip())
    name = " ".join(_UNSAFE.sub(" ", name).split()).strip(" .")[:150]
    return name or folder.name


@dataclass
class Files:
    video: Path
    srt: Path
    youtube_txt: Path
    youtube_md: Path


def files(tl_file: Path, tl: timeline.Timeline | None = None) -> Files:
    tl = tl or timeline.load(tl_file)
    folder = tl_file.parent / OUTPUT_DIR
    base = base_name(tl, tl_file.parent)
    return Files(**{kind: folder / f"{base}{suffix}" for kind, suffix in KINDS.items()})


def made(tl_file: Path, kind: str, path: Path) -> None:
    """Record that Meetaras wrote `path` as this kind of file; the previous one of that kind, if it had
    another name, goes."""
    manifest = tl_file.parent / ".work" / MANIFEST
    try:
        names = json.loads(manifest.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        names = {}
    old = names.get(kind)
    if old and old != path.name:
        (path.parent / old).unlink(missing_ok=True)
    names[kind] = path.name
    manifest.parent.mkdir(exist_ok=True)
    manifest.write_text(json.dumps(names, ensure_ascii=False, indent=1))


def write_texts(tl_file: Path, text: str, markdown: str) -> None:
    """The YouTube text, as plain text and as Markdown."""
    f = files(tl_file)
    f.youtube_txt.parent.mkdir(exist_ok=True)
    for kind, path, content in (("youtube_txt", f.youtube_txt, text), ("youtube_md", f.youtube_md, markdown)):
        path.write_text(content, encoding="utf-8")
        made(tl_file, kind, path)
