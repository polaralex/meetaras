# Using Meetaras

The [README](../README.md) has the install and the quick start. This page has the details: the steps
in the app, captions, 4K, the output, and what stays on your computer.

## Make a video

1. **New talk**: choose the camera video and the slides PDF (and, if you have them, the screen
   recording, mic, intro and outro), fill in the video info (talk title, speaker, event, date).
   Videos stay where they are; the PDF is copied into the talk folder in `~/talks`. **Export size**:
   *Match the recordings* (the default) exports in 4K when the camera video is 4K or the screen recording is
   bigger than 1080p (a Mac's screen records at 2880×1800 or more: code stays readable), else in 1080p (see
   [4K](#4k)).
2. **Make video** converts the videos, drafts the edit, transcribes, writes the YouTube text and
   exports. It runs in the background: you can close the page; the home page shows progress.
   Tick *Stop after the draft* to review before the export.
3. **Review and fix** (optional) in the editor, which opens as soon as the draft is there. Press
   **Help** in it. Main keys: Space play, N/B next/previous slide, S slide + speaker in the corner,
   Shift+S side by side, V speaker large + slide small, F full screen, P speaker only, C screen
   recording, X leave out, K split, [ ] start/end, Ctrl+Z undo. **Export preview** for a quick 720p check;
   **Make video** again for the final video.
   If you have the talk's abstract or links (speaker profile, slides, event page), put them in a file
   `about.txt` in the talk folder: the YouTube text is written from it too, and the links end up in
   the description.
4. **Upload** the video from the `output/` folder (**Open folder**), add the `.srt` as subtitles, and
   paste from **YouTube** (title, description with chapters, tags). After changing the edit, Make video
   updates the chapter times too.

The first time, open **Setup** on the home page: it checks every tool, says what each is for and how to
install what's missing (commands to copy; Meetaras never runs `sudo`). **Set up captions** downloads
the speech model (checksum-verified) and builds whisper.cpp for your machine, with the GPU if there
is one (Metal on a Mac, CUDA or Vulkan on Linux). Same from the terminal: `meetaras doctor [--fix]`.

## Captions on a Mac, when Meetaras runs on Linux or in a VM

Running Meetaras itself on a Mac? Skip this: **Set up captions** builds whisper.cpp with Metal and
Make video transcribes right there.

This is for running Meetaras on Linux, or in a virtual machine (which has no GPU), while a Mac is at
hand. Transcribing an hour of talk takes 1–2 hours on a CPU but about 8 minutes on an Apple Silicon Mac.
In the editor's **Captions** panel, **Transcribe on a Mac** writes a folder (e.g. in a folder shared
with the Mac) with the audio and everything the Mac needs. On the Mac run `bash transcribe-mac.sh` in
that folder (it needs Apple's command line tools; nothing is installed elsewhere). Meetaras picks up
the results by itself.

## Output

Everything to upload is in the talk's `output/` folder, named after the talk (**Open folder** in the app):
`<Title> - <Speaker>.mp4`, the `.srt` with the same name, and the YouTube text as `… - YouTube.txt` and
`… - YouTube.md`. The talk folder itself keeps the working files.

Video: 1920×1080 (3840×2160 with `resolution 4k`), 30 fps, H.264 High (CRF 18), BT.709, AAC 192 kbps
48 kHz, −16 LUFS / −1 dBTP, `+faststart`.

## 4K

A talk can be exported in 4K: the slides, labels and screen recording are then drawn at full 4K
resolution, so text stays sharp. That pays off for a 4K camera and even more for a screen recording
bigger than 1080p (code, terminals, demos; a 1080p camera is simply scaled up). YouTube also streams 4K
uploads at a higher bitrate, so they look crisper even to viewers watching in 1080p. New talks match the
recordings (New talk's **Export size**, `meetaras new --resolution`); the editor's **Export size** (under
Sound) changes it, and Make video then exports again. What it costs on a CPU:

- **Time**: the final export takes about 3–4× as long, and so does converting the videos: each video
  bigger than 1080p gets a 4K master next to its 1080p copy, made in the same pass (smaller ones, e.g. a
  1080p intro, are scaled up in the export instead). With *Stop after the draft* (`--review`) only the
  1080p copies are made first, so the editor opens sooner; the export then makes the 4K masters.
- **Disk**: the 4K masters take about 4× the space of the 1080p files (plan ~25 GB per hour of talk in
  `.work/`). Switching a talk back to 1080p leaves them there: delete `.work/` to get the space back.

Editing, the draft, captions and **Export preview** always use the 1080p copies, so they are as fast
as before and look the same (the preview is the same picture, smaller).

## Faster input

iPhone HDR/HEVC recordings work as they are but convert at about real time without hardware help.
Recording with **Most Compatible** and **HDR Video off** avoids that. `scripts/mac-preconvert.sh`
can pre-convert on a Mac with Apple's built-in `avconvert` (faster, but its colours are punchier).

## Privacy and security

Everything runs on your computer; nothing is uploaded. Only the YouTube text step sends the transcript,
slide text and video info to Claude, and only if you have Claude Code or an API key set up
(`MEETARAS_CLAUDE=0` turns it off).

The app listens on 127.0.0.1 only and answers only its own pages (Host, Origin and `Sec-Fetch-Site`
checks), so other websites can't drive it. It has no login, though: other users on the same computer
could reach it. Use it on a computer that is yours alone. Talks you open from outside the library are
remembered in `.opened.json` in the library folder.
