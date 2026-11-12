import os
import shutil
import time

import pytest

from meetaras import make

from .webapp import BASE, app_client, prepared_talk, talk_folder

TIMELINE = "presenter talk.mp4\nslides deck.pdf\n\n0:01 start\n0:03 slide 1   # match 0.9\n0:06 presenter\n0:11 end\n"


@pytest.fixture(scope="module")
def talk_dir(fixtures, tmp_path_factory):
    return talk_folder(tmp_path_factory.mktemp("library"), prepared_talk(tmp_path_factory, fixtures), TIMELINE, "web").parent


@pytest.fixture
def client(talk_dir):
    # Fresh timeline per test; the prepared media in .work/ is shared. Returns (client, url prefix, timeline).
    tl = talk_dir / "timeline.txt"
    tl.write_text(TIMELINE)
    (talk_dir / "timeline.txt.orig").unlink(missing_ok=True)
    c = app_client(talk_dir.parent)
    assert c.get("/talk/web/api/state").status_code == 200  # what the page does first
    return c, "/talk/web", tl


def test_state(client):
    c, t, _ = client
    st = c.get(f"{t}/api/state").json()
    assert st["slides"] == 3 and st["duration"] == pytest.approx(12, abs=0.1)
    assert [e["kind"] for e in st["timeline"]["events"]] == ["start", "slide", "presenter", "end"]
    assert st["timeline"]["events"][1]["note"] == "match 0.9"
    assert st["layout"]["styles"]["side"]["pip"]["w"] == pytest.approx(576 / 1920)


def test_save_writes_timeline_and_keeps_original(client):
    c, t, tl = client
    st = c.get(f"{t}/api/state").json()["timeline"]
    events = st["events"] + [{"time": 8.5, "kind": "slide", "slide": 2, "style": "full", "note": ""}]
    r = c.put(f"{t}/api/timeline", json={"events": events, "screen_offset": 0, "base_mtime": st["mtime"]})
    assert r.status_code == 200, r.text
    text = tl.read_text()
    assert "0:08.5   slide 2 full" in text and "# match 0.9" in text
    assert (tl.parent / "timeline.txt.orig").read_text() == TIMELINE


def test_invalid_slide_rejected(client):
    c, t, tl = client
    st = c.get(f"{t}/api/state").json()["timeline"]
    events = [{"time": 2, "kind": "slide", "slide": 9, "style": ""}]
    r = c.put(f"{t}/api/timeline", json={"events": events, "screen_offset": 0, "base_mtime": st["mtime"]})
    assert r.status_code == 422 and "3 pages" in r.json()["detail"]
    assert tl.read_text() == TIMELINE


def test_external_change_is_a_conflict(client):
    c, t, tl = client
    st = c.get(f"{t}/api/state").json()["timeline"]
    time.sleep(0.01)
    tl.write_text(TIMELINE + "# edited by hand\n")
    os.utime(tl)
    r = c.put(f"{t}/api/timeline", json={"events": st["events"], "screen_offset": 0, "base_mtime": st["mtime"]})
    assert r.status_code == 412
    assert c.post(f"{t}/api/reload").status_code == 200


def test_video_supports_range_requests(client):
    c, t, _ = client
    r = c.get(f"{t}/media/presenter.mp4", headers={"Range": "bytes=0-99"})
    assert r.status_code == 206 and len(r.content) == 100
    assert c.get(f"{t}/media/slide/1.png").headers["content-type"] == "image/png"
    assert c.get(f"{t}/").status_code == 200
    assert c.get(t, follow_redirects=False).headers["location"] == f"{t}/"


def test_library_lists_the_talks(client):
    c, t, _ = client
    assert 'alt="Meetaras"' in c.get("/").text
    talks = c.get("/api/talks").json()["talks"]
    web = next(x for x in talks if x["id"] == "web")
    assert web["url"] == f"{t}/" and web["status"] is None and not web["output"]


def test_only_this_machine_may_ask(client):
    c, t, _ = client
    assert c.get("/api/talks", headers={"Host": "evil.example:8770"}).status_code == 403  # DNS rebinding
    assert c.post(f"{t}/api/make", json={}, headers={"Origin": "https://evil.example"}).status_code == 403
    assert c.get("/api/talks", headers={"Origin": BASE}).status_code == 200
    assert c.post(f"{t}/api/make", json={}, headers={"Origin": "http://127.0.0.1:3000"}).status_code == 403  # another app


def test_other_sites_cant_read_through_the_browser(client):
    # A GET from another site carries no Origin, but the browser says where it comes from.
    c, t, _ = client
    for site in ("cross-site", "same-site"):  # same-site: e.g. another app on localhost
        assert c.get(f"{t}/api/state", headers={"Sec-Fetch-Site": site}).status_code == 403
        assert c.get("/api/setup", headers={"Sec-Fetch-Site": site}).status_code == 403
        assert c.get(f"{t}/media/slide/1.png", headers={"Sec-Fetch-Site": site}).status_code == 403
    assert c.get(f"{t}/", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 200  # a link to a page works
    assert c.get("/", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 200
    for site in ("same-origin", "none"):  # the app's own pages; a typed URL
        assert c.get(f"{t}/media/slide/1.png", headers={"Sec-Fetch-Site": site}).status_code == 200
    assert c.get("/docs").status_code == 404 and c.get("/openapi.json").status_code == 404


def test_file_picker_lists_folders_and_matching_files(fixtures, tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / ".hidden").mkdir()
    for name in ("deck.pdf", "talk.MOV", "notes.txt"):
        (tmp_path / name).write_text("x")
    c = app_client(tmp_path)
    data = c.get("/api/files", params={"path": str(tmp_path), "kind": "pdf"}).json()
    assert data["dirs"] == ["sub"] and [f["name"] for f in data["files"]] == ["deck.pdf"]
    data = c.get("/api/files", params={"path": str(tmp_path / "talk.MOV"), "kind": "video"}).json()
    assert data["path"] == str(tmp_path) and [f["name"] for f in data["files"]] == ["talk.MOV"]
    assert {"label": "Talks", "path": str(tmp_path)} in data["places"]
    assert c.get("/api/files", params={"path": str(tmp_path / "missing")}).status_code == 404


def test_new_talk_from_the_form(recordings, tmp_path):
    c = app_client(tmp_path)
    form = {"name": "my-talk", "video": str(recordings / "IMG_0001.MOV"), "slides": str(recordings / "deck.pdf"),
            "speaker": "Ada Lovelace", "make": False}  # fmt: skip
    r = c.post("/api/talks", json=form)
    assert r.status_code == 200, r.text
    assert r.json() == {"id": "my-talk", "url": "/talk/my-talk/"}
    assert "speaker    Ada Lovelace" in (tmp_path / "my-talk" / "timeline.txt").read_text()
    # Not drafted yet: the page shows Make video instead of the editor.
    m = c.get("/talk/my-talk/api/make").json()
    assert not m["ready"] and "Make video" in m["reason"] and m["status"] is None
    assert c.get("/talk/my-talk/api/state").status_code == 409

    r = c.post("/api/talks", json={**form, "name": "other", "slides": str(recordings / "missing.pdf")})
    assert r.status_code == 422 and "slides file not found" in r.json()["detail"]
    assert c.post("/api/talks", json={**form, "name": "../escape"}).status_code == 422


def test_talks_outside_the_library_can_be_opened(talk_dir, tmp_path):
    c = app_client(tmp_path)
    url = c.post("/api/open", json={"path": str(talk_dir)}).json()["url"]
    assert url == "/talk/web/"
    assert c.get("/talk/web/api/state").status_code == 200
    assert c.post("/api/open", json={"path": str(tmp_path / "nothing")}).status_code == 404


def test_opened_talks_survive_a_restart(talk_dir, tmp_path):
    c = app_client(tmp_path)
    url = c.post("/api/open", json={"path": str(talk_dir)}).json()["url"]
    st = c.get(f"{url}api/state").json()["timeline"]
    c = app_client(tmp_path)  # the app restarted while the editor page stayed open
    assert c.get(url).status_code == 200
    r = c.put(f"{url}api/timeline", json={"events": st["events"], "screen_offset": 0, "base_mtime": st["mtime"]})
    assert r.status_code == 200, r.text  # prepared again on the way, not "reload the page"
    assert c.get(f"{url}media/slide/1.png").status_code == 200


def test_a_library_folder_doesnt_hide_an_opened_talk(talk_dir, tmp_path):
    c = app_client(tmp_path)
    assert c.post("/api/open", json={"path": str(talk_dir)}).json()["url"] == "/talk/web/"
    (tmp_path / "web").mkdir()  # later, a talk of the same name in the library
    (tmp_path / "web" / "timeline.txt").write_text(TIMELINE)
    talks = {t["id"]: t["folder"] for t in c.get("/api/talks").json()["talks"]}
    assert talks == {"web": str(tmp_path / "web"), "web-2": str(talk_dir)}
    assert c.post("/api/open", json={"path": str(talk_dir)}).json()["url"] == "/talk/web-2/"


def test_an_opened_talk_that_is_gone_is_left_out(talk_dir, tmp_path):
    elsewhere = tmp_path / "elsewhere"
    shutil.copytree(talk_dir, elsewhere, symlinks=True)
    c = app_client(tmp_path / "talks")
    c.post("/api/open", json={"path": str(elsewhere)})
    assert len(c.get("/api/talks").json()["talks"]) == 1
    shutil.rmtree(elsewhere)  # moved or deleted
    r = c.get("/api/talks")
    assert r.status_code == 200 and r.json()["talks"] == []


@pytest.mark.slow
def test_make_video_from_the_page(recordings, tmp_path):
    c = app_client(tmp_path)
    form = {"name": "t", "video": str(recordings / "IMG_0001.MOV"), "slides": str(recordings / "deck.pdf"), "review": True}
    assert c.post("/api/talks", json=form).status_code == 200  # make: True by default
    assert c.post("/talk/t/api/make", json={}).status_code == 409  # one run at a time
    deadline = time.monotonic() + 120
    while (m := c.get("/talk/t/api/make").json())["status"]["state"] == "running" and time.monotonic() < deadline:
        time.sleep(0.5)
    assert m["status"]["state"] == "review", m["log"]
    assert m["ready"] and any("== draft" in line for line in m["log"])
    assert not any(line.endswith("%") for line in m["log"])  # progress lines are left out
    assert c.get("/talk/t/api/state").status_code == 200
    assert make.read_status(tmp_path / "t")["steps"]["export"]["state"] == "waiting"


def test_open_folder_uses_a_file_manager(client, monkeypatch):
    from meetaras.web import jobs

    c, t, tl = client
    opened = []
    monkeypatch.setattr(jobs.subprocess, "Popen", lambda cmd, **kw: opened.append(cmd))
    monkeypatch.setattr(jobs.shutil, "which", lambda name: {"thunar": "/usr/bin/thunar", "xdg-open": "/usr/bin/xdg-open"}.get(name))
    monkeypatch.setattr(jobs.sys, "platform", "linux")
    assert c.post(f"{t}/api/open-folder").json() == {"folder": str(tl.parent)}
    assert opened == [["/usr/bin/thunar", str(tl.parent)]]  # a file manager, not whatever opens folders by default
    monkeypatch.setattr(jobs.shutil, "which", lambda name: None)
    assert c.post(f"{t}/api/open-folder").status_code == 501


def test_new_talk_export_size(recordings, uhd_video, tmp_path):
    c = app_client(tmp_path)
    form = {"video": str(uhd_video), "slides": str(recordings / "deck.pdf"), "make": False}
    for name, size, written in (("match", "auto", True), ("hd", "1080p", False), ("uhd", "4k", True)):
        assert c.post("/api/talks", json={**form, "name": name, "resolution": size}).status_code == 200
        assert ("resolution 4k" in (tmp_path / name / "timeline.txt").read_text()) == written
    r = c.post("/api/talks", json={**form, "name": "camera", "video": str(recordings / "IMG_0001.MOV")})
    assert r.status_code == 200 and "resolution" not in (tmp_path / "camera" / "timeline.txt").read_text()  # 720p: 1080p
    r = c.post("/api/talks", json={**form, "name": "bad", "resolution": "8k"})
    assert r.status_code == 422 and "export size" in r.json()["detail"]


def test_export_size_is_saved_like_the_other_settings(client):
    c, t, tl = client
    st = c.get(f"{t}/api/state").json()
    assert st["timeline"]["info"]["resolution"] == "1080p" and st["camera"] == [1280, 720]
    info = {**st["timeline"]["info"], "resolution": "4k"}
    body = {"events": st["timeline"]["events"], "screen_offset": 0, "info": info, "base_mtime": st["timeline"]["mtime"]}
    r = c.put(f"{t}/api/timeline", json=body)
    assert r.status_code == 200, r.text
    assert "resolution 4k\n" in tl.read_text()
    assert c.get(f"{t}/api/state").json()["layout"] == st["layout"]  # the editor's geometry stays 1080p's
    body = {**body, "info": {**info, "resolution": "8k"}, "base_mtime": r.json()["mtime"]}
    assert c.put(f"{t}/api/timeline", json=body).status_code == 422
