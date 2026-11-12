"""meetaras doctor / Setup: finding whisper.cpp, the checks, the checked download, setting up captions."""

import hashlib
import http.server
import threading
import time

import pytest

from meetaras import captions, tools


@pytest.fixture
def data(tmp_path, monkeypatch):
    """An empty data folder, no whisper.cpp anywhere: a fresh machine as far as captions go."""
    monkeypatch.setenv("MEETARAS_DATA", str(tmp_path / "data"))
    monkeypatch.delenv("MEETARAS_WHISPER_DIR", raising=False)
    monkeypatch.delenv("MEETARAS_WHISPER_MODEL", raising=False)
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    return tmp_path / "data"


def touch(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x")
    path.chmod(0o755)
    return path


def test_whisper_is_found_where_meetaras_looks(data, tmp_path, monkeypatch):
    assert tools.find_whisper() == (None, None) and not captions.available()
    # whisper-cli on PATH (e.g. Homebrew) with the model in the data folder
    touch(tmp_path / "bin" / "whisper-cli")
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}:/usr/bin:/bin")
    model = touch(data / "models" / tools.MODEL_NAME)
    assert tools.find_whisper() == (tmp_path / "bin" / "whisper-cli", model)
    # meetaras's own build comes first
    own = touch(data / "whisper.cpp" / "build" / "bin" / "whisper-cli")
    assert tools.find_whisper() == (own, model) and captions.available()
    # an explicit whisper.cpp folder wins, and only it counts
    monkeypatch.setenv("MEETARAS_WHISPER_DIR", str(tmp_path / "elsewhere"))
    assert tools.find_whisper() == (None, model) and not captions.available()


def test_checks_offer_to_set_up_captions(data, tmp_path, monkeypatch):
    monkeypatch.setattr(tools, "compiler_ok", lambda: True)  # a machine that can build whisper.cpp,
    monkeypatch.setattr(tools, "cmake", lambda: ["cmake"])  # whatever this one has installed
    results = {c.name: c for c in tools.checks(tmp_path)}
    assert results["ffmpeg"].ok and results["ffmpeg"].required  # the test machine has a full ffmpeg
    assert not results["whisper.cpp"].ok and results["whisper.cpp"].action == "captions"
    assert not results["captions model"].ok and results["captions model"].action == "captions"
    text = tools.describe(list(results.values()))
    assert "meetaras doctor --fix" in text and "for: captions" in text


def test_whisper_source_needs_git(data, tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    with pytest.raises(tools.SetupError, match="git is needed"):
        tools.whisper_source()


def test_build_settings_per_gpu():
    assert "-DGGML_METAL_EMBED_LIBRARY=ON" in tools.cmake_args("metal")
    assert "-DGGML_CUDA=ON" in tools.cmake_args("cuda") and "-DGGML_VULKAN=ON" in tools.cmake_args("vulkan")
    assert all("GGML_" not in a for a in tools.cmake_args("cpu"))
    assert "-DBUILD_SHARED_LIBS=OFF" in tools.cmake_args("cpu")  # one self-contained whisper-cli


def test_cmake_from_pypi_is_pinned(monkeypatch):
    monkeypatch.setattr(tools.shutil, "which", lambda name: "/opt/uv" if name == "uv" else None)
    assert tools.cmake() == ["/opt/uv", "tool", "run", "--from", f"cmake=={tools.CMAKE_VERSION}", "cmake"]


@pytest.fixture
def web_file(tmp_path):
    """A small file served over HTTP, like the model on Hugging Face."""
    body = b"not really a model" * 1000
    (tmp_path / "www").mkdir()
    (tmp_path / "www" / "model.bin").write_bytes(body)
    handler = lambda *a: http.server.SimpleHTTPRequestHandler(*a, directory=str(tmp_path / "www"))  # noqa: E731
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}/model.bin", hashlib.sha256(body).hexdigest()
    server.shutdown()


def test_download_is_checked(web_file, tmp_path):
    url, sha = web_file
    dest = tmp_path / "models" / "m.bin"
    with pytest.raises(tools.SetupError, match="damaged"):
        tools.download_model(url, "0" * 64, dest)
    assert not dest.exists() and not dest.with_suffix(".part").exists()
    assert tools.download_model(url, sha, dest) == dest and dest.stat().st_size == 18000


def test_set_up_captions_does_what_is_missing(data, monkeypatch):
    done = []
    monkeypatch.setattr(tools, "download_model", lambda: done.append("download") or touch(data / "models" / tools.MODEL_NAME))
    monkeypatch.setattr(tools, "build_whisper", lambda: done.append("build") or touch(tools._cli(tools.own_whisper())))
    tools.set_up_captions()
    assert done == ["download", "build"] and tools.setup_status()["state"] == "done"
    tools.set_up_captions()  # nothing left to do
    assert done == ["download", "build"] and captions.available()


def test_failed_setup_is_reported(data, monkeypatch):
    def broken():
        raise tools.SetupError("no network")

    monkeypatch.setattr(tools, "download_model", broken)
    with pytest.raises(tools.SetupError):
        tools.set_up_captions()
    assert tools.setup_status() | {"started": 0} == {"state": "failed", "error": "no network", "started": 0, "step": "download", "progress": 0}


def test_killed_setup_is_interrupted(data):
    tools._write_status(state="running", started=time.time(), step="build", progress=0.5, error=None)
    assert tools.setup_status()["state"] == "running"  # just started: the process may not have the lock yet
    tools._write_status(started=time.time() - 60)
    assert tools.setup_status()["state"] == "interrupted"  # nothing holds the lock
