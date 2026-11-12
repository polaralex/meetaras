"""Command line: meetaras new | make | status | edit, and the single steps (draft, captions, render, check, align)."""

from __future__ import annotations

import time
from pathlib import Path

import typer

from . import align as align_mod
from . import ffmpeg, timeline
from .normalize import normalize
from .render import render as render_timeline

app = typer.Typer(add_completion=False, help="Turn a recorded tech talk into a YouTube-ready video.")


def _file(path: Path) -> Path:
    """Commands take a talk folder or its timeline file."""
    from .make import timeline_file

    return timeline_file(path)


def _load(path: Path) -> timeline.Timeline:
    try:
        return timeline.load(path)
    except timeline.TimelineError as exc:
        typer.secho(f"{path}: {exc}", fg="red", err=True)
        raise typer.Exit(1) from None


def _fail(exc: Exception) -> None:
    typer.secho(f"error: {exc}", fg="red", err=True)
    raise typer.Exit(1) from None


@app.command()
def render(
    timeline_file: Path = typer.Argument(..., help="Talk folder or its timeline.txt"),
    output: Path | None = typer.Option(
        None, "-o", "--output", help="Output file (default: the talk's video in output/, as make exports it; previews: output-preview.mp4)"
    ),
    preview: bool = typer.Option(False, "--preview", help="Fast 720p low-quality render for checking"),
    resolution: str | None = typer.Option(None, "--resolution", help="1080p or 4k for this render (default: the timeline's)"),
) -> None:
    """Render the timeline to a YouTube-ready MP4."""
    from . import deliver

    timeline_file = _file(timeline_file)
    tl = _load(timeline_file)
    if resolution is not None:
        if resolution.lower() not in timeline.RESOLUTIONS:
            _fail(ValueError(f"--resolution must be {' or '.join(timeline.RESOLUTIONS)}"))
        tl.resolution = resolution.lower()
    delivered = output is None and not preview  # the finished video, where make puts it
    out = output or (timeline_file.parent / "output-preview.mp4" if preview else deliver.files(timeline_file, tl).video)
    t0 = time.monotonic()
    try:
        render_timeline(timeline_file, tl, out, preview=preview)
    except (ffmpeg.FFmpegError, timeline.TimelineError, align_mod.AlignmentError, FileNotFoundError) as exc:
        _fail(exc)
    if delivered:
        deliver.made(timeline_file, "video", out)
        if out.with_suffix(".srt").exists():
            deliver.made(timeline_file, "srt", out.with_suffix(".srt"))
    typer.secho(f"Done in {time.monotonic() - t0:.0f} s → {out}", fg="green")


@app.command()
def align(timeline_file: Path = typer.Argument(..., help="Talk folder or its timeline.txt")) -> None:
    """Detect the external audio's offset and drift, and print header lines to paste."""
    timeline_file = _file(timeline_file)
    tl = _load(timeline_file)
    if tl.audio is None:
        _fail(ValueError("the timeline has no `audio` setting; nothing to align"))
    work = timeline_file.parent / ".work"
    try:
        master = normalize(tl.presenter, work, "presenter")
        a = align_mod.align_files(master, tl.audio, work)
    except (ffmpeg.FFmpegError, align_mod.AlignmentError) as exc:
        _fail(exc)
    typer.echo(align_mod.describe(a))
    typer.echo("\nTo pin these values, add to the timeline header:")
    typer.echo(f"audio_offset {a.offset:.3f}\naudio_drift_ppm {a.drift_ppm:.1f}")


@app.command()
def check(timeline_file: Path = typer.Argument(..., help="Talk folder or its timeline.txt")) -> None:
    """Parse the timeline and list its segments without rendering."""
    tl = _load(_file(timeline_file))
    try:
        duration = ffmpeg.probe_duration(tl.presenter)
        from .slides import page_count

        tl.validate_slides(page_count(tl.slides))
        segments = tl.segments(duration)
    except (ffmpeg.FFmpegError, timeline.TimelineError) as exc:
        _fail(exc)
    for s in segments:
        typer.echo(f"{timeline.format_time(s.start):>9} – {timeline.format_time(s.end):<9} {s.label}")
    typer.secho(f"OK: {len(segments)} segments", fg="green")


@app.command()
def draft(
    video: Path = typer.Argument(..., help="Presenter video"),
    slides_pdf: Path = typer.Argument(..., help="Slides PDF"),
    screen: Path | None = typer.Option(None, "--screen", help="Screen recording of the laptop (slides + demos)"),
    output: Path | None = typer.Option(None, "-o", "--output", help="Timeline to write (default: timeline.txt next to the video)"),
    force: bool = typer.Option(False, "--force", help="Draft an existing timeline again (its header is kept)"),
) -> None:
    """Draft a timeline by recognising the slides in the video (and in the screen recording, if given).

    Defaults: presenter only until the first slide; slides with the presenter in the corner; with a
    screen recording, anything on screen that isn't a slide (demos) is shown with the presenter in the corner.
    The same draft as `meetaras make`; with --force, the header of the existing timeline (speaker,
    audio, intro, …) stays and only the changes are drafted again.
    """
    from . import progress
    from .make import draft_timeline

    out = timeline.absolute(output or video.parent / "timeline.txt")
    if out.exists() and not force:
        _fail(FileExistsError(f"{out} exists; use --force to draft it again or -o to choose another file"))
    video, slides_pdf = timeline.absolute(video), timeline.absolute(slides_pdf)
    if out.exists():
        tl = _load(out)
        tl.presenter, tl.slides = video, slides_pdf
    else:
        tl = timeline.Timeline(base_dir=out.parent, presenter=video, slides=slides_pdf)
    if screen is not None:
        tl.screen = timeline.absolute(screen)
    work = out.parent / ".work"
    t0 = time.monotonic()
    last: dict[str, float] = {}

    def show(label: str, fraction: float) -> None:
        if abs(fraction - last.get(label, -1.0)) >= 0.01:
            last[label] = fraction
            typer.echo(f"\r  {label}: {fraction * 100:5.1f}%   ", nl=False, err=True)

    try:
        typer.echo("[1/2] Normalizing videos")
        master = normalize(tl.presenter, work, "presenter")
        screen_master = normalize(tl.screen, work, "screen") if tl.screen else None
        typer.echo(f"[2/2] Recognising slides in {timeline.format_time(ffmpeg.probe_duration(master))} of video")
        progress.hook = show
        try:
            detail = draft_timeline(out, tl, master, screen_master)
        finally:
            progress.hook = None
    except (ffmpeg.FFmpegError, timeline.TimelineError) as exc:
        _fail(exc)
    typer.echo("", err=True)
    typer.secho(f"Wrote {out}: {detail} in {time.monotonic() - t0:.0f} s", fg="green")


@app.command()
def captions(
    timeline_file: Path = typer.Argument(..., help="Talk folder or its timeline.txt"),
    mac_bundle: Path | None = typer.Option(None, "--mac-bundle", help="Write a folder for transcribing on a Mac instead"),
    import_from: Path | None = typer.Option(None, "--import", help="Import what the Mac transcribed in this folder"),
) -> None:
    """Transcribe the talk with whisper.cpp (once; slow) and write captions for the current timeline.

    After this, every export also writes an .srt next to the video, fitted to that export. Without
    whisper.cpp here (or for speed: Metal), --mac-bundle writes everything a Mac needs; --import takes
    the results back.
    """
    from . import captions as cap
    from . import macbundle, tools

    timeline_file = _file(timeline_file)
    tl = _load(timeline_file)
    work = timeline_file.parent / ".work"
    t0 = time.monotonic()
    try:
        master = normalize(tl.presenter, work, "presenter")
        if mac_bundle is not None:
            info = macbundle.write_bundle(timeline_file, mac_bundle)
            typer.secho(f"Wrote {info['pieces']} piece(s) to {info['talk_folder']}.", fg="green")
            typer.echo(macbundle.INSTRUCTIONS.format(folder=mac_bundle))
            return
        if import_from is not None:
            transcript = macbundle.import_results(timeline_file, import_from)
        else:
            transcript = cap.transcribe_talk(tl, master, work)
        out, n = cap.deliver_srt(timeline_file, tl, transcript)
    except (ffmpeg.FFmpegError, cap.CaptionError, timeline.TimelineError, align_mod.AlignmentError,
            macbundle.MacBundleError, tools.SetupError) as exc:  # fmt: skip
        _fail(exc)
    typer.secho(f"Wrote {out} ({n} captions) in {time.monotonic() - t0:.0f} s. Exports now include captions.", fg="green")


@app.command()
def metadata(
    folder: Path = typer.Argument(..., help="Talk folder or its timeline.txt"),
    no_ai: bool = typer.Option(False, "--no-ai", help="Only the chapters, without Claude"),
) -> None:
    """YouTube title options, description, tags and chapters for the final edit, printed and in output/.

    Run it again after changing the edit: chapter times move with trims and cuts.
    """
    from . import deliver
    from . import metadata as meta

    tl_file = _file(folder)
    _load(tl_file)
    try:
        data, detail = meta.generate(tl_file, ai=not no_ai)
    except (ffmpeg.FFmpegError, timeline.TimelineError) as exc:
        _fail(exc)
    typer.echo(meta.to_text(data))
    typer.secho(f"{detail}\n→ {deliver.files(tl_file).youtube_txt}", fg="green")


@app.command()
def new(
    folder: Path = typer.Argument(..., help="Folder to create for the talk, e.g. ~/talks/mytalk"),
    video: Path = typer.Option(..., "--video", help="Camera recording of the talk"),
    slides: Path = typer.Option(..., "--slides", help="Slides as PDF (copied into the folder)"),
    screen: Path | None = typer.Option(None, "--screen", help="Screen recording of the laptop"),
    audio: Path | None = typer.Option(None, "--audio", help="External mic recording (aligned automatically)"),
    intro: Path | None = typer.Option(None, "--intro", help="Video to play before the talk"),
    outro: Path | None = typer.Option(None, "--outro", help="Video to play after the talk"),
    event: str | None = typer.Option(None, help="Event name, e.g. 'Puppet Masters Meetup #42'"),
    date: str | None = typer.Option(None, help="Date as shown in the video"),
    speaker: str | None = typer.Option(None, help="Speaker name"),
    role: str | None = typer.Option(None, help="Speaker's role or company"),
    talk: str | None = typer.Option(None, help="Talk title"),
    language: str | None = typer.Option(None, help="Spoken language for captions (en, el, …); default: detect"),
    resolution: str = typer.Option("auto", "--resolution", help="Export size: auto (4K for a 4K camera or a screen recording bigger than 1080p), 1080p or 4k"),
) -> None:
    """Create a talk folder from the recordings and the slides (videos stay where they are)."""
    from .make import MakeError, new_talk

    try:
        out = new_talk(folder, video, slides, screen, audio, intro, outro, resolution.lower(),
                       event=event, date=date, speaker=speaker, role=role, talk=talk, language=language)  # fmt: skip
    except (MakeError, ffmpeg.FFmpegError) as exc:
        _fail(exc)
    size = "4K" if timeline.load(out).resolution == "4k" else "1080p"
    typer.secho(f"Created {out} (export size {size}). Next: meetaras make {folder}", fg="green")


@app.command()
def make(
    folder: Path = typer.Argument(..., help="Talk folder (from `meetaras new`)"),
    review: bool = typer.Option(False, "--review", help="Stop after the draft, to review it before the export"),
    detach: bool = typer.Option(False, "--detach", help="Run in the background (log in .work/make.log)"),
) -> None:
    """Everything unattended: convert, slides, draft, captions (if whisper.cpp is installed), final export.

    Finished steps are skipped, so running it again continues an interrupted run, or re-exports after edits.
    """
    from . import captions as cap
    from . import make as make_mod

    try:
        if detach:
            pid = make_mod.start_detached(folder, review)
            typer.secho(f"Started (pid {pid}). Progress: meetaras status {folder}", fg="green")
            return
        t0 = time.monotonic()
        make_mod.make(folder, review)
    except (make_mod.MakeError, ffmpeg.FFmpegError, timeline.TimelineError, cap.CaptionError,
            align_mod.AlignmentError, FileNotFoundError) as exc:  # fmt: skip
        _fail(exc)
    typer.secho(f"Finished in {(time.monotonic() - t0) / 60:.0f} min.", fg="green")
    typer.echo(make_mod.describe(make_mod.read_status(folder), folder))


@app.command()
def status(folder: Path = typer.Argument(..., help="Talk folder")) -> None:
    """Show what `meetaras make` is doing, or did last, for a talk."""
    from .make import describe, read_status

    typer.echo(describe(read_status(folder), folder))


@app.command()
def doctor(fix: bool = typer.Option(False, "--fix", help="Set up captions: download the model and build whisper.cpp")) -> None:
    """Check the tools Meetaras needs and say how to install what's missing."""
    from . import tools

    if fix:
        try:
            tools.set_up_captions()
        except tools.SetupError as exc:
            _fail(exc)
        except OSError as exc:  # network
            _fail(tools.SetupError(f"download failed: {exc}"))
    results = tools.checks()
    typer.echo(tools.describe(results))
    if any(c.required and not c.ok for c in results):
        raise typer.Exit(1)


def _serve(library: Path | None, folder: Path | None, port: int, no_browser: bool) -> None:
    """Run the app (or use the one already running on the port) and open it, at a talk if one is given."""
    import json
    import socket
    import threading
    import urllib.request
    import webbrowser

    base = f"http://127.0.0.1:{port}"

    def call(path: str, body: dict | None = None) -> dict:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(base + path, data, {"Content-Type": "application/json"} if data else {})
        with urllib.request.urlopen(req, timeout=2) as r:
            return json.load(r)

    with socket.socket() as s:
        busy = s.connect_ex(("127.0.0.1", port)) == 0
    if busy:
        try:
            assert call("/api/app")["app"] == "meetaras"
            url = base + (call("/api/open", {"path": str(folder.absolute())})["url"] if folder else "/")
        except Exception:
            _fail(OSError(f"port {port} is in use by something else (an older Meetaras editor?); try --port {port + 1}"))
        typer.echo(f"meetaras is already running: {url}")
        if not no_browser:
            webbrowser.open(url)
        return

    import uvicorn

    from .web.server import LIBRARY, create_app

    web = create_app(library or LIBRARY)
    url = base + (_talk_path(web, folder) if folder else "/")
    typer.secho(f"meetaras running at {url}  (Ctrl+C to stop)", fg="green")
    if not no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    uvicorn.run(web, host="127.0.0.1", port=port, log_level="warning")


def _talk_path(web, folder: Path) -> str:
    from .web.server import talk_url

    return talk_url(web.state.library.add(folder))


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    library: Path | None = typer.Option(None, help="Folder with the talks (default: $MEETARAS_LIBRARY or ~/talks)"),
    port: int = typer.Option(8765, help="Port for the app"),
    no_browser: bool = typer.Option(False, "--no-browser", help="Don't open a browser window"),
) -> None:
    """Turn a recorded tech talk into a YouTube-ready video. Without a command: open the app."""
    if ctx.invoked_subcommand is None:
        _serve(library, None, port, no_browser)


@app.command()
def edit(
    folder: Path = typer.Argument(..., help="Talk folder or its timeline.txt"),
    port: int = typer.Option(8765, help="Port for the app"),
    no_browser: bool = typer.Option(False, "--no-browser", help="Don't open a browser window"),
) -> None:
    """Open the app at a talk: review and fix the edit, make the video."""
    tl_file = _file(folder)
    if not tl_file.is_file():
        _fail(FileNotFoundError(f"{tl_file} not found; create the talk with `meetaras new`"))
    _serve(None, tl_file.parent, port, no_browser)


if __name__ == "__main__":
    app()
