# Meetaras

Turns a recorded tech talk into a YouTube-ready 1080p or 4K video: camera video + slides PDF (+ optional
screen recording, external mic, intro/outro) → the talk's `output/` folder: `<Title> - <Speaker>.mp4`, the
`.srt` with the same name and the YouTube text (`… - YouTube.txt` / `.md`). Python 3.11+ (uv), FFmpeg via
subprocess, local web app (library, New talk, Setup, editor). macOS and Linux (x86_64, arm64); CPU only
is fine, a GPU is used for whisper.cpp when there is one.

This machine and its user (where files arrive, real talks to leave alone): `CLAUDE.local.md`, if present
(not in git).

**Making a video for the user: follow `.claude/skills/talk-video/SKILL.md`.**
Usage for humans: README.md (install, quick start), docs/guide.md (details), docs/reference.md (CLI, timeline format); CONTRIBUTING.md (tests).

## How it works

`timeline.txt` is the single source of truth. Header settings + timed commands; each command means
"from this time on, show this" (times on the camera video's clock):

```
presenter  talk.mov           # camera video (required)
slides     deck.pdf           # required
screen     screen.mov         # optional laptop screen recording; screen_offset = talk time it starts
audio      mic.wav            # optional external mic (auto-aligned, or audio_offset/audio_drift_ppm)
intro / outro <file>          # optional videos before/after
event / date / speaker / role / talk / lower_third_every / language   # labels + captions
compression off               # no evening out of the sound (for already cleaned-up audio); default on
resolution 4k                 # export size (default 1080p); editing, draft, captions, previews stay 1080p

0:00   start | end            # trim
1:23   presenter              # camera full frame
2:10   slide 5 [side|full|speaker]   # default: slide large, camera small in the corner
                              # side: side by side · full: slide only · speaker: camera large, slide small
40:00  screen [side|full|speaker]    # same styles with the screen recording
41:00  cut                    # drop everything until the next command
```

Pipeline (`meetaras render`): normalize camera to 1080p30 SDR H.264 (HDR tone-mapped; a 4K talk also
gets picture-only 4K masters of videos bigger than 1080p, one pass with the 1080p file) → slides to PNGs → each segment rendered to its own clip (frame-exact) → concat → audio (align, cuts with fades,
compression + −16 LUFS, per-talk `loudness`) → intro/outro → the video in `output/`, plus its `.srt` if a
transcript exists.
Everything intermediate is cached in `<talk>/.work/` by content keys; edits re-render only changed clips.

Product flow: `meetaras new` writes a header-only timeline (no changes = not drafted yet); `meetaras make`
is the job runner for CLI and app alike: convert → slides → draft → captions → metadata → export, each
step skipped when done, one run per talk (flock `.work/make.lock`), progress in `.work/status.json`,
`--detach` / the app's Make video start it as its own process (log `.work/make.log`). The app polls.
Talk time → output time (trim, cuts, intro) is one map, `render.Edit` (`render.the_edit`), shared by
captions and chapters.

## Commands

```sh
uv run meetaras                                 # the app: library, New talk, editor (http://127.0.0.1:8765)
uv run meetaras new talk --video V --slides S.pdf [--screen R] [--audio M] [--speaker …] [--resolution auto|1080p|4k]
uv run meetaras make talk [--review] [--detach] # convert → slides → draft → captions → export, resumable
uv run meetaras status talk                     # progress of make (.work/status.json)
uv run meetaras metadata talk [--no-ai]         # YouTube text: chapters (no AI) + Claude's titles/description/tags
uv run meetaras doctor [--fix]                  # check tools; --fix: captions model + whisper.cpp build
uv run meetaras draft VIDEO DECK.pdf [--screen REC] -o talk/timeline.txt [--force]   # same draft as make; --force keeps the header
uv run meetaras edit talk                       # the app at that talk
uv run meetaras captions talk                   # whisper.cpp transcript (CPU: ≈1–2× talk length, once) + the .srt
uv run meetaras captions talk --mac-bundle DIR  # Transcribe on a Mac (Metal); --import DIR takes the results
uv run meetaras render talk/timeline.txt [--preview] [-o OUT] [--resolution 1080p|4k]   # default: the video in output/
uv run meetaras check talk/timeline.txt         # parse + list segments
uv run meetaras align talk/timeline.txt         # external-audio offset/drift
```

## Code map

| File | Role |
|---|---|
| `timeline.py` | parser/serializer, `Segment`/`Event` model, layouts list in the docstring |
| `normalize.py` | any camera/screen/intro video → canvas-sized 30 fps H.264 (HDR→SDR, BT.601→BT.709, copy fast path); `kind` is always explicit; `canvas=` 4K: a picture-only export master (made in one pass with the 1080p file when both are missing); `master_canvas()` decides who gets one (only sources bigger than 1080p: smaller ones are scaled up in the clip graphs), `export_masters()` lists them; `talk_videos()` converts all of a talk's videos (make and the app; `export=True`: the 4K masters too where they can share the 1080p pass; make --review leaves them to the export) |
| `make.py` | `new`/`make`/`status`: the steps of Make video (`draft_timeline` shared with `meetaras draft`), shared with the app |
| `jobs.py` | long jobs the app polls (make, captions setup): lock file, JSON status, detached start |
| `draft.py` | slide recognition (SIFT homography + band-passed grid correlation), screen-recording sync, context rules (`apply_context`) |
| `render.py` | segments → clips → export (`FINAL`/`FINAL_4K`/`PREVIEW`; previews always from the 1080p pipeline); `masters()`: sound and timing from the 1080p file, picture at the export size; `clip_key`; audio graph, loudness (measurements cached in `.work/loudness-*.json`), bumpers; `Edit`/`the_edit`: talk time → output time; `kept_segments` |
| `slides.py` | PDF → slide PNGs at a canvas size; all PDF text reading (`page_texts`, `page_titles`, `page_stats`) |
| `layouts.py` | all geometry, in 1080p units (`STYLES`); `Geometry` (`HD`, `UHD`; `RESOLUTIONS`) scales it for 4K; filter graphs (`clip_filter` composes a clip's whole graph, for a `g`) |
| `labels.py` | event/speaker overlays and the insets' decor (rounded mask, shadow) as PNGs (bundled Noto Sans in `fonts/`); labels drawn in 1080p units, rendered at the geometry's scale |
| `captions.py` | whisper.cpp transcription (`WHISPER_FLAGS`, shared with the Mac script), the talk's transcript (`transcript_key`/`_path`, `transcribe_talk`), words → output time via `render.Edit`, SRT cues, the `.srt` in `output/` (`deliver_srt`) |
| `tools.py` | doctor/Setup: finding whisper.cpp + model, checks with per-OS install hints, checked model download, pinned whisper.cpp build |
| `metadata.py` | YouTube metadata (state in `youtube.json`, text files in `output/`): chapters from `render.the_edit` (YouTube's rules; never converts), `claude -p` / API for the rest; optional `about.txt` in the talk folder (abstract, links) goes into the prompt; `MEETARAS_CLAUDE=0` turns Claude off (tests do) |
| `deliver.py` | the talk's `output/` folder: finished files named `<Title> - <Speaker>` (.mp4, .srt, YouTube .txt/.md); replaces the old-named files it made |
| `macbundle.py` | Transcribe on a Mac: shared folder with model, pinned whisper.cpp source, CMake, script, audio pieces (`talks/<folder>-<hash>/` per talk); import |
| `align.py` | external audio offset + drift by cross-correlation |
| `web/` | FastAPI + plain JS app (no build): library home + New talk with a file picker, editor page per talk under `/talk/<id>/` (relative URLs); never renders: Make video runs `meetaras make`, Export preview `meetaras render` |
| `web/server.py` | app assembly + request checks (Host, Origin, `Sec-Fetch-Site`: only its own pages may call `/api` and `/media`) |
| `web/common.py` | shared state (`AppState`, `Library`, `Project`, `Job`) and the `Depends` aliases; talks opened from outside the library are remembered in `<library>/.opened.json` |
| `web/library.py`, `jobs.py`, `editor.py` | routers: home/Setup/file picker/new/open · Make video, Export preview, captions, YouTube · editor state, timeline, media |
| `web/static/` | `common.js` (shared by all pages: `$`, `el`, `api()`, errors); editor ES modules `model.js` (no DOM), `stage.js`, `edit.js`, `bar.js`, `panels.js`, `app.js` (wiring) |

## Conventions and gotchas

- FFmpeg only via `meetaras/ffmpeg.py` with argument lists, never shell strings.
- Geometry and filter graphs only in `layouts.py`; the editor gets the geometry from `/api/state`, so preview == export.
- Geometry is written in 1080p units and scaled in one place, `layouts.Geometry` (4K = ×2: boxes, radius,
  shadow; labels are drawn at 1080p and rendered at 2×). Never a second table: pass the `g` through. The
  editor always gets the 1080p geometry (as fractions of the canvas).
- Segments: use `Segment.kind` / `.style`, not the layout string (which stays in clip keys).
- Write intermediate files through `cache.atomic_output(path)` (unique temp name, renamed on success).
- Bump `cache.CACHE_VERSION` (or `draft.CLASSIFY_VERSION`) when a step's output changes.
- Input files enter cache keys only via `cache.file_fingerprint(path)`: name + size + mtime, so a moved
  talk folder keeps its caches. Never change an existing key by accident: users have hours of cached
  conversions, transcripts and clips (a transcript's key: `captions.transcript_key`). `Quality.__str__` and
  `timeline.Info`'s repr are part of clip/label keys. `tests/test_cache_keys.py` pins 1080p names; 4K
  keys differ only through the scaled values (or an extra part added only when not 1080p).
- Header paths: relative inside the talk folder, as written otherwise (`timeline.header_path`); symlinks aren't followed.
- ffmpeg 7 takes colour tags from frames: keep the `BT709` setparams at the end of filter graphs.
- Timeline comments are `# text` (hash + space); `#101` in text is not a comment.
- No presenter crop / speaker tracking by design (user decision): insets show the whole camera frame.
- The user prefers "good enough" automation + fixing in the editor over tuning detection.
- whisper.cpp: `tools.find_whisper()` (MEETARAS_WHISPER_DIR / _MODEL, meetaras's data folder `MEETARAS_DATA`,
  PATH). `meetaras doctor --fix` builds the pinned commit (`tools.WHISPER_COMMIT`) into the data folder.
- Tests: `uv run pytest` (~5 min; `-n auto --dist loadfile` ~3.5 min); quick check
  `uv run pytest -m "not slow and not ui and not whisper"` (~40 s). Marks: `slow` (real
  convert/draft/make/render), `ui` (Playwright + Chromium), `whisper` (real whisper.cpp, opt-in via the
  `real_whisper` fixture). conftest isolates every test (empty `MEETARAS_DATA`, no whisper.cpp, no Mac shared
  folder, `MEETARAS_CLAUDE=0`) and stops detached runs; talks made from `recordings` get the session's
  converted/drafted `.work/` (`seed_caches`). App servers use free ports, never 8765. Run heavy tests with
  `nice` while a long job runs. CI (`.github/workflows/tests.yml`): the quick check on push/PR; the full
  suite by hand (Actions → tests → Run workflow).
- Stop a dev app: `pkill -f "[b]in/meetaras"` (the bracket stops pkill matching its own shell).
- zsh doesn't word-split `$var`: use `bash -c` for loops over option strings.
- Jobs longer than ~2 h must be started detached: `meetaras make --detach`, or `setsid nohup … > log 2>&1
  < /dev/null &` for single steps; not as a harness background task (killed at its 2-hour limit). Transcription is resumable (10-minute pieces in `.work/`).
- Don't use whisper.cpp's `--vad`: it shifts timestamps by seconds (measured on a real talk).
- Editor saves: a conflict with a change on disk is HTTP 412; the editor then offers Reload.
