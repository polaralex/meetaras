"""Browser tests of the app (skipped when Chromium or Playwright isn't available).

One browser for the module, a fresh context (storage, clipboard) per test; each talk is a copy of one
prepared talk, so the camera video is converted once.
"""

import contextlib
import json
import shutil
import threading
import time
from pathlib import Path

import pytest

from .webapp import prepared_talk, serve, talk_folder

playwright = pytest.importorskip("playwright.sync_api")
pytestmark = pytest.mark.ui
CHROMIUM = shutil.which("chromium") or shutil.which("chromium-browser")  # else Playwright's own, if installed
TIMELINE = "presenter talk.mp4\nslides deck.pdf\n\n0:02 slide 1\n0:06 slide 2\n"


@pytest.fixture(scope="module")
def browser():
    with playwright.sync_playwright() as p:
        try:
            b = p.chromium.launch(executable_path=CHROMIUM, args=["--autoplay-policy=no-user-gesture-required"])
        except playwright.Error as exc:
            pytest.skip(f"no Chromium to drive: {str(exc).splitlines()[0]}")
        yield b
        b.close()


@pytest.fixture
def page(browser):
    """A new tab; page errors (and responses ≥ 400) are collected in page.errors."""
    context = browser.new_context(viewport={"width": 1400, "height": 900}, permissions=["clipboard-read", "clipboard-write"])
    page = context.new_page()
    page.errors = []
    page.on("pageerror", lambda e: page.errors.append(str(e)))
    page.on("response", lambda r: r.status >= 400 and page.errors.append(f"{r.status} {r.url}"))
    yield page
    context.close()


@pytest.fixture(scope="module")
def prepared(tmp_path_factory, fixtures):
    return prepared_talk(tmp_path_factory, fixtures)


class Editor:
    """A talk open in the editor."""

    def __init__(self, page, tl: Path):
        self.page, self.tl = page, tl

    def at(self, t):
        self.page.evaluate(f"presVid.currentTime = {t}")
        self.page.wait_for_function(f"Math.abs(presVid.currentTime - {t}) < 0.05 && !presVid.seeking")

    def saved(self, expected=None):
        self.page.wait_for_function("document.getElementById('saveStatus').textContent.startsWith('Saved')")
        if expected is not None:
            assert changes(self.tl) == expected


@pytest.fixture
def start_app(page):
    """start_app(library) → the base URL of the app on that library, stopped after the test (once its
    tab is closed: the tab may still be streaming a video)."""
    with contextlib.ExitStack() as stack:
        yield lambda library: stack.enter_context(serve(library))
        page.context.close()


@pytest.fixture
def open_talk(page, start_app, prepared, tmp_path):
    """open_talk(timeline, extra files, first_visit) → an Editor on a copy of the prepared talk."""

    def open_(timeline=TIMELINE, extra=(), first_visit=False) -> Editor:
        tl = talk_folder(tmp_path, prepared, timeline, extra=extra)
        base = start_app(tmp_path)
        if not first_visit:
            page.add_init_script("localStorage.setItem('meetaras.helpSeen', '1')")
        page.goto(f"{base}/talk/talk/")
        page.wait_for_function("document.getElementById('presVid').readyState >= 2", timeout=20000)
        return Editor(page, tl)

    return open_


def changes(tl):
    return [line.split("#")[0].split() for line in tl.read_text().splitlines() if line[:1].isdigit()]


def test_keys_edit_and_autosave(open_talk):
    ed = open_talk(first_visit=True)
    page = ed.page
    # Help opens by itself on the first visit, closes with Esc, and comes back with "?"
    assert page.locator("#helpDialog").is_visible()
    page.keyboard.press("Escape")
    assert not page.locator("#helpDialog").is_visible()
    page.keyboard.press("?")
    assert page.locator("#helpDialog").is_visible()
    page.keyboard.press("Space")  # keys don't reach the editor while help is open
    assert page.evaluate("presVid.paused")
    page.keyboard.press("Escape")

    ed.at(4)
    page.keyboard.press("p")
    ed.saved([["0:02", "slide", "1"], ["0:04", "presenter"], ["0:06", "slide", "2"]])
    assert page.inner_text("#modeNow") == "speaker"

    ed.at(8)
    page.keyboard.press("f")  # a new change at the playhead: slide 2 full
    ed.saved([["0:02", "slide", "1"], ["0:04", "presenter"], ["0:06", "slide", "2"], ["0:08", "slide", "2", "full"]])
    assert page.locator("#screenGap").is_hidden()

    page.locator(".ev", has_text="presenter").first.click(position={"x": 150, "y": 8})
    page.keyboard.press("n")  # edits the selected change: presenter → slide 2 (after slide 1)
    ed.saved([["0:02", "slide", "1"], ["0:04", "slide", "2"], ["0:06", "slide", "2"], ["0:08", "slide", "2", "full"]])

    page.keyboard.press("Escape")
    page.keyboard.press("Control+z")
    ed.saved([["0:02", "slide", "1"], ["0:04", "presenter"], ["0:06", "slide", "2"], ["0:08", "slide", "2", "full"]])

    # X cuts until the next change; playback jumps over the cut part
    ed.at(5)
    page.keyboard.press("x")
    ed.saved([["0:02", "slide", "1"], ["0:04", "presenter"], ["0:05", "cut"], ["0:06", "slide", "2"], ["0:08", "slide", "2", "full"]])
    assert page.locator("#cutShade").is_visible()
    ed.at(4.8)
    page.evaluate("presVid.play()")
    page.wait_for_function("presVid.currentTime > 6.2", timeout=5000)
    page.evaluate("presVid.pause()")
    assert page.locator("#cutShade").is_hidden()
    assert page.errors == []


def test_button_bar_does_what_the_keys_do(open_talk):
    ed = open_talk()
    page = ed.page
    bar = page.locator("#toolbar")
    assert bar.get_by_role("button", name="Screen").first.is_hidden()  # no screen recording in this talk
    assert bar.get_by_role("button", name="Unsplit").is_disabled()

    ed.at(4)
    bar.get_by_role("button", name="Only you").click()
    ed.saved([["0:02", "slide", "1"], ["0:04", "presenter"], ["0:06", "slide", "2"]])
    page.keyboard.press("Space")  # the clicked button didn't take focus: Space still plays
    page.wait_for_function("!presVid.paused")
    page.keyboard.press("Space")

    ed.at(8)
    bar.get_by_role("button", name="Side by side").click()
    ed.saved([["0:02", "slide", "1"], ["0:04", "presenter"], ["0:06", "slide", "2"], ["0:08", "slide", "2", "side"]])

    page.locator(".ev", has_text="presenter").first.click(position={"x": 150, "y": 8})
    assert "selected change" in page.inner_text("#toolTarget")
    bar.get_by_role("button", name="Unsplit").click()
    ed.saved([["0:02", "slide", "1"], ["0:06", "slide", "2"], ["0:08", "slide", "2", "side"]])
    assert page.inner_text("#toolTarget") == "From here, show:"

    ed.at(3)  # split slide 1 at 0:03, then give the second part its own style
    blocks = page.locator("#track .seg").count()
    bar.get_by_role("button", name="Split here").click()
    ed.saved([["0:02", "slide", "1"], ["0:03", "slide", "1"], ["0:06", "slide", "2"], ["0:08", "slide", "2", "side"]])
    assert page.locator("#track .seg").count() == blocks + 1  # the split shows on the timeline bar,
    assert page.locator("#track .seg.sel").count() == 1  # selected, though it still looks like the part before
    page.locator("#track .seg.sel").click(position={"x": 30, "y": 20})  # clicking it keeps that part selected
    assert "0:03" in page.inner_text("#toolTarget")
    bar.get_by_role("button", name="Full").click()
    ed.saved([["0:02", "slide", "1"], ["0:03", "slide", "1", "full"], ["0:06", "slide", "2"], ["0:08", "slide", "2", "side"]])
    assert page.errors == []


def test_split_twice_then_leave_the_middle_out(open_talk):
    ed = open_talk()
    page = ed.page
    bar = page.locator("#toolbar")
    ed.at(3)
    bar.get_by_role("button", name="Split here").click()
    ed.at(5)
    bar.get_by_role("button", name="Split here").click()
    ed.saved([["0:02", "slide", "1"], ["0:03", "slide", "1"], ["0:05", "slide", "1"], ["0:06", "slide", "2"]])
    page.locator('#track .seg[data-index="1"]').click(position={"x": 20, "y": 20})  # the piece from 0:03 to 0:05
    bar.get_by_role("button", name="Leave out").click()
    ed.saved([["0:02", "slide", "1"], ["0:03", "cut"], ["0:05", "slide", "1"], ["0:06", "slide", "2"]])
    assert page.locator('#track .seg[data-index="1"]').inner_text() == "left out"
    assert page.locator("#cutShade").is_visible()  # the playhead is inside the part that is left out

    page.locator('#track .seg[data-index="1"]').click(position={"x": 20, "y": 20})
    page.keyboard.press("s")  # choosing a style brings it back
    ed.saved([["0:02", "slide", "1"], ["0:03", "slide", "1"], ["0:05", "slide", "1"], ["0:06", "slide", "2"]])


def test_times_are_shown_as_in_the_timeline_file(open_talk):
    page = open_talk().page
    shown = page.evaluate("""import('/static/model.js').then(({ fmt, parseTime }) =>
        [fmt(59.96), fmt(2939.6, 0), fmt(3599.96), fmt(-5.25), fmt(-0.04), parseTime('1:02:03.5')])""")
    assert shown == ["1:00.0", "49:00", "1:00:00.0", "-0:05.3", "0:00.0", 3723.5]  # never 0:60.0


def test_intro_outro_preview(open_talk, bumpers):
    timeline = "presenter talk.mp4\nslides deck.pdf\nintro intro.mov\noutro outro.mp4\n\n0:02 slide 1\n0:09 end\n"
    page = open_talk(timeline, extra=bumpers).page
    assert page.locator("#playAllBtn").is_visible() and page.locator("#introBtn").is_visible()

    page.keyboard.press("Home")  # intro → talk → outro
    page.wait_for_function("document.getElementById('clock').textContent.startsWith('intro')")
    assert page.locator("#bumperVid").is_visible()
    page.wait_for_function("!presVid.paused && presVid.currentTime > 0.2", timeout=8000)  # talk follows
    assert page.locator("#bumperVid").is_hidden()
    page.evaluate("presVid.currentTime = 8.5")
    page.wait_for_function("document.getElementById('clock').textContent.startsWith('outro')", timeout=8000)
    page.keyboard.press("Escape")
    assert page.locator("#bumperVid").is_hidden()


def test_video_info_labels(open_talk):
    ed = open_talk()
    page = ed.page
    assert page.locator("#infoBox").get_attribute("open") is not None  # empty info: panel invites input

    page.fill("[data-info=speaker]", "Ada Lovelace")
    page.press("[data-info=speaker]", "Enter")
    page.fill("[data-info=event]", "Puppet Masters Meetup #42")
    page.press("[data-info=event]", "Tab")
    ed.saved()
    page.wait_for_timeout(600)
    text = ed.tl.read_text()
    assert "speaker    Ada Lovelace" in text and "event      Puppet Masters Meetup #42" in text
    assert page.is_checked("[data-info=compression]")  # on by default, and not written to the file
    page.uncheck("[data-info=compression]")
    ed.saved()
    page.wait_for_timeout(600)
    assert "compression off" in ed.tl.read_text()
    assert page.input_value("[data-info=resolution]") == "1080p"  # the export size, saved the same way
    assert page.inner_text("#cameraSize") == "The camera video is 1280×720. "
    page.select_option("[data-info=resolution]", "4k")
    ed.saved()
    page.wait_for_timeout(600)
    assert "compression off\nresolution 4k\n" in ed.tl.read_text()

    page.evaluate("presVid.currentTime = 3")  # slide 1 with the speaker in the corner
    page.wait_for_function("document.getElementById('ovl_strip_corner').style.visibility === 'visible'")
    page.evaluate("presVid.currentTime = 1")  # speaker shot: badge + name banner
    page.wait_for_function("document.getElementById('ovl_badge').style.visibility === 'visible'")
    page.wait_for_function("document.getElementById('ovl_lower_third').style.visibility === 'visible'")


def test_new_talk_to_finished_video(page, start_app, recordings, tmp_path):
    """The v1 flow in the browser: New talk → Make video → status → done, with the editor opening on the way.
    No whisper.cpp (conftest): the make run, a separate process, inherits that."""
    library = tmp_path / "talks"
    library.mkdir()
    base = start_app(library)
    page.add_init_script("localStorage.setItem('meetaras.helpSeen', '1')")
    page.goto(base)
    page.wait_for_selector("#empty:not(.hidden)")

    page.click("#newBtn")
    form = page.locator("#newForm")
    form.locator("[name=video]").fill(str(recordings / "IMG_0001.MOV"))
    form.locator("[data-pick=pdf]").click()  # the slides through the file picker
    page.fill("#pickerPath", str(recordings))
    page.press("#pickerPath", "Enter")
    page.locator("#entries button", has_text="deck.pdf").click(timeout=15000)
    assert form.locator("[name=slides]").input_value() == str(recordings / "deck.pdf")
    assert page.locator("#picker").is_hidden()
    form.locator("[name=talk]").fill("Engines and Ideas")
    assert form.locator("[name=name]").input_value() == "engines-and-ideas"
    assert form.locator("[name=resolution]").input_value() == "auto"  # Export size: match the recordings
    with page.expect_request(lambda r: r.url.endswith("/api/talks") and r.method == "POST") as request:
        page.click("#newSubmit")
    assert request.value.post_data_json["resolution"] == "auto"

    page.wait_for_url(f"{base}/talk/engines-and-ideas/")
    try:  # a make run from nothing: minutes on a busy machine (parallel tests)
        page.wait_for_selector("#editorArea:not(.hidden)", timeout=180000)  # opens once the draft is there
        page.wait_for_function("document.getElementById('makeMsg').textContent.startsWith('Done.')", timeout=180000)
    except Exception:
        print("DEBUG msg:", page.inner_text("#makeMsg"), "| status:", (library / "engines-and-ideas" / ".work" / "status.json").read_text())
        print("DEBUG log:", (library / "engines-and-ideas" / ".work" / "make.log").read_text()[-3000:])
        raise
    assert page.locator("#outputLink").is_visible() and page.locator("#openFolder").is_visible()
    assert page.locator(".step.done").count() == 5 and page.locator(".step.skipped").count() == 1  # no captions
    assert (library / "engines-and-ideas" / "output" / "Engines and Ideas.mp4").is_file()
    assert "resolution" not in (library / "engines-and-ideas" / "timeline.txt").read_text()  # a 720p camera: 1080p
    page.wait_for_function("document.getElementById('presVid').readyState >= 2", timeout=20000)

    page.click("#logBtn")  # the log opens at its newest line
    assert page.evaluate("(l => l.scrollHeight - l.scrollTop - l.clientHeight < 8)(document.getElementById('makeLog'))")

    page.goto(base)
    page.wait_for_selector("#talks tbody tr")
    assert page.inner_text("#talks tbody tr .state") == "Done"
    assert page.errors == []


def test_file_picker_with_a_bare_file_name(page, start_app, tmp_path):
    (tmp_path / "share").mkdir()
    (tmp_path / "share" / "deck.pdf").write_text("x")
    base = start_app(tmp_path)
    page.add_init_script(f"localStorage.setItem('meetaras.lastDir', {json.dumps(str(tmp_path / 'share'))})")
    page.goto(base)
    page.click("#newBtn")
    page.locator("#newForm [name=slides]").fill("deck.pdf")  # typed, not chosen: no folder in it
    page.locator("#newForm [data-pick=pdf]").click()
    page.locator("#entries button", has_text="deck.pdf").wait_for()  # the last folder used, not "deck.pd"
    assert page.input_value("#pickerPath") == str(tmp_path / "share")


def test_preview_export_log_stays_in_place(open_talk):
    ed = open_talk()
    page = ed.page
    page.evaluate("document.getElementById('exportLog').dataset.mark = 'same'")
    page.click("#exportPreview")
    page.wait_for_function("document.getElementById('exportHead').textContent.endsWith('done')", timeout=120000)
    # Updated in place (it used to be rebuilt every second, jumping back to the top), at the newest line.
    assert page.evaluate("document.getElementById('exportLog').dataset.mark") == "same"
    assert page.evaluate("(l => l.scrollHeight - l.scrollTop - l.clientHeight < 8)(document.getElementById('exportLog'))")
    assert page.locator("#exportLink a").get_attribute("href") == "media/output/output-preview.mp4"
    assert (ed.tl.parent / "output-preview.mp4").is_file()


def test_setup_page(page, start_app, tmp_path, monkeypatch):
    """A machine without captions: home says so, Setup offers one click and shows the result."""
    from meetaras import tools

    # (conftest: an empty data folder and no whisper.cpp)
    def fake_setup():  # instead of a 574 MB download and a build
        tools._write_status(state="running", started=time.time(), step="download", progress=0.5, error=None)
        threading.Timer(1.5, lambda: tools._write_status(state="done", step="", progress=None)).start()

    monkeypatch.setattr(tools, "start_setup", fake_setup)
    base = start_app(tmp_path)
    page.goto(base)
    page.wait_for_selector("#setupBanner:not(.hidden)")
    assert "Captions aren't set up" in page.inner_text("#setupBanner")
    page.click("#setupBanner a")
    page.wait_for_selector("#checks tbody tr")
    ffmpeg_row = page.locator("#checks tr", has_text="ffmpeg")
    assert ffmpeg_row.locator(".mark").inner_text() == "✓"
    page.click("#setupCaptions")
    page.wait_for_function("document.getElementById('jobHead').textContent.startsWith('Downloading the model')")
    page.wait_for_function("document.getElementById('jobHead').textContent === 'Captions are set up.'", timeout=10000)
    assert page.errors == []


def test_captions_panel_transcribe_on_a_mac(open_talk, tools_here, tmp_path):
    from .conftest import whisper_json

    folder = tmp_path / "share" / "meetaras-captions"
    folder.parent.mkdir()
    ed = open_talk("presenter talk.mp4\nslides deck.pdf\nlanguage en\n\n0:02 slide 1\n")
    page = ed.page
    page.wait_for_function("document.getElementById('captionsMsg').textContent.includes('Transcribe on a Mac')")
    page.fill("#macFolder", str(folder))
    page.click("#macWrite")
    page.wait_for_function("document.getElementById('captionsMsg').textContent === 'Waiting for the Mac: 0 of 1 pieces done.'")
    assert "transcribe-mac.sh" in page.inner_text("#macHelp")
    (next((folder / "talks").iterdir()) / "out" / "part000.json").write_text(json.dumps(whisper_json([("Hello", 3000)])))
    page.wait_for_function("document.getElementById('captionsMsg').textContent.startsWith('Imported from the Mac')", timeout=15000)
    assert "Hello" in (ed.tl.parent / "output" / "talk.srt").read_text()


def test_youtube_dialog(open_talk):
    ed = open_talk()
    page = ed.page
    page.click("#youtubeBtn")
    page.wait_for_function("document.getElementById('ytStatus').textContent.startsWith('Not written yet')")
    page.click("#ytRegenerate")  # without Claude (tests): chapters only
    page.wait_for_function("document.getElementById('ytStatus').textContent.startsWith('Chapters only')")
    page.fill("#ytTitle", "Engines and Ideas")
    page.fill("#ytDescription", "A talk about engines.")
    page.fill("#ytChapters", "0:00 Start\n0:04 Middle\nnot a time")
    page.click("#ytSave")
    page.wait_for_function("document.getElementById('ytSaved').textContent.startsWith('chapter line 3')")
    page.fill("#ytChapters", "0:00 Start\n0:04 Middle")
    page.click("[data-copy=description]")  # saves, then copies the description with its chapters
    page.wait_for_function("document.getElementById('ytSaved').textContent.startsWith('Saved')")
    assert page.evaluate("navigator.clipboard.readText()") == "A talk about engines.\n\n0:00 Start\n0:04 Middle"
    page.fill("#ytTags", "engines, history")
    page.click("#ytMarkdown")  # everything at once, unsaved edits included (it saves them first)
    page.wait_for_function("document.getElementById('ytSaved').textContent.includes('copied all as Markdown')")
    md = page.evaluate("navigator.clipboard.readText()")
    assert md.startswith("# Engines and Ideas\n\n## Title\n\n```text\nEngines and Ideas\n```")
    assert "## Description (with chapters, as it goes into YouTube)\n\n```text\nA talk about engines.\n\n0:00 Start\n0:04 Middle\n```" in md
    assert "## Chapters\n\n```text\n0:00 Start\n0:04 Middle\n```" in md and md.endswith("## Tags\n\n```text\nengines, history\n```\n")
    page.keyboard.press("Escape")
    tl = ed.tl
    text = (tl.parent / "output" / "talk - YouTube.txt").read_text()
    assert text.startswith("TITLE\nEngines and Ideas\n") and "0:04 Middle" in text
    assert (tl.parent / "output" / "talk - YouTube.md").read_text() == md  # the same text as a file, next to the video
    assert json.loads((tl.parent / "youtube.json").read_text())["edited"]
