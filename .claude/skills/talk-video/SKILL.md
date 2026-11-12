---
name: talk-video
description: Turn a recorded talk (camera video + slides PDF, optional screen recording / mic / intro / outro) into a YouTube-ready video with meetaras. Use when the user wants to make, edit or export a talk video, add captions, labels or YouTube metadata, or asks what to do with new recordings in the shared folder.
---

# Making a talk video with Meetaras

Work from the Meetaras repository (see its CLAUDE.md); run commands as `uv run meetaras …`. You use the
same CLI as the app's buttons, so the user can follow along in the app (`meetaras`, http://127.0.0.1:8765)
and take over at any point. Talks live in the library folder (`$MEETARAS_LIBRARY`, default `~/talks/<name>/`).
Where the user's recordings arrive, and talks not to touch: `CLAUDE.local.md` in the repository, if present.

## 0. Tools

`meetaras doctor` lists what's missing. `meetaras doctor --fix` sets up captions (model + whisper.cpp
build); anything needing sudo, give the user the command. Captions are optional (Mac or later).

## 1. Collect inputs (ask only for what's missing)

- **Camera video** (required): iPhone .MOV is fine (HEVC/HDR handled). For experiments cut an excerpt:
  `ffmpeg -ss 600 -t 180 -i IN -map 0:v:0 -map 0:a:0 -c copy excerpt.mov`.
- **Slides as PDF** (required). Keynote can't be read on Linux: ask for a PDF export on the Mac.
- **Screen recording** (optional, strongly preferred): exact slide detection and demos. Check it's the
  same talk (similar length, slides visible).
- External mic, intro/outro videos, video info (talk title, speaker, role, event, date, language).
- Check `df -h ~` (a 1-hour talk needs ~6 GB) and that `ffprobe` reads every file.

## 2. New talk and Make video

```sh
uv run meetaras new ~/talks/NAME --video CAM.MOV --slides DECK.pdf [--screen REC.mov] [--audio MIC.wav] \
    [--intro I.mp4] [--outro O.mp4] [--talk "…" --speaker "…" --role "…" --event "…" --date "…" --language en]
uv run meetaras make ~/talks/NAME --detach [--review]
uv run meetaras status ~/talks/NAME          # steps, details, errors; the log is .work/make.log
```

`make` runs convert (~45 min for 55 min of HDR on a 6-core CPU) → slides → draft (~12 min) → captions
(whisper.cpp: 1–2 h per hour of talk on a CPU, minutes with a GPU or on a Mac; skipped without it) →
metadata (the YouTube text, ~1 min with Claude) → export (~45 min).
Always `--detach` for whole talks: it survives the 2-hour limit of harness background tasks and the
app shows the same progress. One-shot by default; `--review` stops after the draft (running `make`
again finishes). Re-running skips finished work, so it is also the way to re-export after edits.

## 3. Check the draft

When the draft step is done (`meetaras status`), read `~/talks/NAME/timeline.txt`:
- Screen sync (`meetaras status`, draft line: `auto: N slide changes agree`): fine with N ≥ 3;
  otherwise the user sets the offset in the editor (±0.1/±1 s buttons).
- Many short alternating changes or many `CHECK` notes mean recognition struggled there; say where.
- Defaults (`draft.apply_context`): presenter until the first slide; non-slide screen content before
  the talk proper → `screen speaker`; slides with ≤ 10 words → `slide N speaker`; slides held > 90 s
  alternate 45 s slide / 30 s speaker; demos stay `screen`.
- To spot-check, grab frames: `ffmpeg -ss T -i .work/presenter-*.mp4 -frames:v 1 -vf scale=640:-2 f.png`.

## 4. Review in the app

`uv run meetaras edit ~/talks/NAME` (the editor opens once the draft exists; Help explains it). While the
user edits, don't rewrite timeline.txt yourself; if you must, tell them to click "Reload".

## 5. Captions on the Mac (no or slow whisper here)

`uv run meetaras captions ~/talks/NAME --mac-bundle "<folder shared with the Mac>/meetaras-captions"`, then
the user runs `bash transcribe-mac.sh` in that folder on the Mac (~8 min per hour of talk on an M3). The
app's Captions panel and the next `make` pick the results up; or `meetaras captions NAME --import FOLDER`.
New captions only rewrite the `.srt` in `output/`; the video isn't exported again.

## 6. Deliver

Everything to upload is in `~/talks/NAME/output/`: `<Title> - <Speaker>.mp4`, the `.srt` with the same
name, and the YouTube text as `… - YouTube.txt` / `.md`. Copy them to the shared folder if the user wants
them on another machine. `youtube.json` in the talk folder keeps the user's edits from the app's YouTube
dialog; `meetaras metadata NAME` writes it again (chapters follow the current edit).

## When something is off

- Colours dull/cyan → tone-mapping (`normalize.tonemap_filter`); compare frames before changing.
- Audio out of sync with an external mic → `meetaras align`, then pin `audio_offset` in the header.
- Wrong slide in a stretch → fix in the editor; don't tune detection unless it's systematic (the user
  prefers fixing in the editor over tuning).
- `timeline.txt` errors name the line: `uv run meetaras check ~/talks/NAME`.
- A failed or interrupted run: `meetaras status` says where; `make` again continues.
