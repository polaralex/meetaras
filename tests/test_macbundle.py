"""Transcribe on a Mac: the folder for the Mac, its script, and taking the results back."""

import json
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest

from meetaras import captions, macbundle, make, tools
from meetaras.timeline import load

from .conftest import whisper_json


@pytest.fixture
def talk(recordings, seed_caches, tmp_path):
    tl_file = make.new_talk(tmp_path / "talk", recordings / "IMG_0001.MOV", recordings / "deck.pdf", language="en")
    seed_caches(tl_file.parent)
    return tl_file


def test_folder_for_the_mac_and_back(tools_here, talk, tmp_path):
    folder = tmp_path / "share" / "meetaras-captions"
    folder.parent.mkdir()
    info = macbundle.write_bundle(talk, folder)
    piece_dir = Path(info["talk_folder"])
    assert piece_dir.parent == folder / "talks" and piece_dir.name.startswith("talk-")  # readable on the Mac
    assert info["pieces"] == 1 and (piece_dir / "pieces" / "part000.wav").is_file()
    assert (piece_dir / "language.txt").read_text() == "en" and "Terms:" in (piece_dir / "prompt.txt").read_text()
    script = (folder / "transcribe-mac.sh").read_text()
    assert tools.WHISPER_COMMIT in script and "-nfa" in script and "--dtw large.v3.turbo" in script
    assert "--vad" not in script
    assert (folder / tools.MODEL_NAME).read_bytes() == b"model"
    assert (folder / "model.sha256").read_text().startswith(tools.MODEL_SHA256)
    with tarfile.open(folder / f"whisper.cpp-{tools.WHISPER_COMMIT[:7]}.tar.gz") as tar:
        assert "whisper.cpp/CMakeLists.txt" in tar.getnames()  # unpacks into whisper.cpp/, as the script expects
    assert subprocess.run(["bash", "-n", str(folder / "transcribe-mac.sh")]).returncode == 0

    assert macbundle.results(talk)["done"] == 0
    with pytest.raises(macbundle.MacBundleError, match="hasn't transcribed 1 of 1"):
        macbundle.import_results(talk)
    # The Mac's work:
    (piece_dir / "out" / "part000.json").write_text(json.dumps(whisper_json([("Hello", 1000), ("Mac.", 1500)])))
    assert macbundle.results(talk)["done"] == 1
    transcript = macbundle.import_results(talk)
    out, n = captions.deliver_srt(talk, load(talk), transcript)
    assert n == 1 and "Hello Mac." in out.read_text()


@pytest.mark.slow
def test_make_takes_the_macs_results(tools_here, talk, tmp_path):
    folder = tmp_path / "meetaras-captions"
    make.make(talk, review=True)
    piece_dir = Path(macbundle.write_bundle(talk, folder)["talk_folder"])
    make.make(talk)
    assert make.read_status(talk)["steps"]["captions"]["detail"] == "waiting for the Mac (0 of 1 pieces)"
    out = talk.parent / "output"
    assert (out / "talk.mp4").is_file() and not (out / "talk.srt").exists()
    (piece_dir / "out" / "part000.json").write_text(json.dumps(whisper_json([("Later", 2000)])))
    make.make(talk)  # the video is up to date: only the captions are added
    steps = make.read_status(talk)["steps"]
    assert steps["captions"]["detail"] == "transcript from the Mac"
    assert steps["export"]["detail"] == "talk.mp4 is up to date; captions written"
    assert "Later" in (out / "talk.srt").read_text()


def test_results_for_other_audio_are_not_taken(tools_here, talk, tmp_path):
    folder = tmp_path / "meetaras-captions"
    piece_dir = Path(macbundle.write_bundle(talk, folder)["talk_folder"])
    (piece_dir / "out" / "part000.json").write_text(json.dumps(whisper_json([("Hi", 1000)])))
    talk.write_text(talk.read_text().replace("language   en", "language   el"))  # other audio settings, other pieces
    with pytest.raises(macbundle.MacBundleError, match="other audio settings"):
        macbundle.import_results(talk)
    macbundle.write_bundle(talk, folder)  # writing it again starts over
    assert not (piece_dir / "out" / "part000.json").exists()
    assert (piece_dir / "language.txt").read_text() == "el"


def test_talks_with_the_same_folder_name(tools_here, recordings, seed_caches, talk, tmp_path):
    folder = tmp_path / "meetaras-captions"
    other = make.new_talk(tmp_path / "elsewhere" / "talk", recordings / "IMG_0001.MOV", recordings / "deck.pdf", language="el")
    seed_caches(other.parent)
    mine, theirs = (Path(macbundle.write_bundle(t, folder)["talk_folder"]) for t in (talk, other))
    assert mine != theirs and (mine / "pieces" / "part000.wav").is_file()  # neither deletes the other's pieces
    assert (mine / "language.txt").read_text() == "en" and (theirs / "language.txt").read_text() == "el"


def test_bundle_from_an_older_version_imports(tools_here, talk, tmp_path):
    """Older versions named the talk's place after its folder alone."""
    folder = tmp_path / "meetaras-captions"
    new = Path(macbundle.write_bundle(talk, folder)["talk_folder"])
    old = folder / "talks" / "talk"
    new.rename(old)
    (talk.parent / ".work" / macbundle.STATE).unlink()
    (old / "out" / "part000.json").write_text(json.dumps(whisper_json([("Old", 1000)])))
    assert macbundle.import_results(talk, folder).is_file()


@pytest.mark.slow
@pytest.mark.skipif(not shutil.which("shasum"), reason="needs shasum")
def test_the_script_transcribes(real_whisper, tools_here, talk, tmp_path):
    """Runs transcribe-mac.sh with this machine standing in for the Mac (whisper.cpp already built)."""
    cli, model = real_whisper
    folder = tmp_path / "meetaras-captions"
    macbundle.write_bundle(talk, folder)
    (folder / tools.MODEL_NAME).unlink()
    (folder / tools.MODEL_NAME).symlink_to(model)
    (folder / "whisper.cpp" / "build" / "bin").mkdir(parents=True)
    (folder / "whisper.cpp" / "build" / "bin" / "whisper-cli").symlink_to(cli)
    run = subprocess.run(["bash", str(folder / "transcribe-mac.sh")], capture_output=True, text=True, timeout=600)
    assert run.returncode == 0, run.stdout + run.stderr
    out = json.loads((Path(macbundle.results(talk)["talk_folder"]) / "out" / "part000.json").read_text())
    assert any(tok.get("t_dtw", -1) >= 0 for seg in out["transcription"] for tok in seg["tokens"])
    assert macbundle.import_results(talk).is_file()
