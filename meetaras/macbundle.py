"""Transcribe on a Mac: a folder with everything a Mac needs to transcribe talks with whisper.cpp on its
GPU (Metal, many times faster than real time on Apple Silicon), written from here; the results are
picked up when they appear.

The folder (e.g. one shared with the Mac, such as a virtual machine's shared folder) serves all talks:
  transcribe-mac.sh                    the script to run on the Mac
  ggml-…bin, model.sha256              the model (copied the first time)
  whisper.cpp-<commit>.tar.gz          whisper.cpp at the pinned commit (the first time)
  cmake-…-macos-universal.tar.gz       Kitware's CMake, for a Mac without one (the first time; SHA-256 pinned)
  talks/<talk>-<id>/pieces/partNNN.wav the talk audio in 10-minute pieces, prompt.txt, language.txt
  talks/<talk>-<id>/out/partNNN.json   written by the Mac
(<talk> is the talk folder's name, <id> a short hash of where it is: talks with the same folder name
don't share a place.)
"""

from __future__ import annotations

import hashlib
import json
import shlex
import shutil
import subprocess
import urllib.request
from pathlib import Path

from . import captions, timeline, tools
from .normalize import normalize

CMAKE_TARBALL = "cmake-4.4.4-macos-universal.tar.gz"
CMAKE_URL = f"https://github.com/Kitware/CMake/releases/download/v4.4.4/{CMAKE_TARBALL}"
CMAKE_SHA256 = "4b7b73704b1db9b374e5c9ab8e17ac6148b817b6396ee75cd94a852cbac9d305"
STATE = "mac-bundle.json"  # in .work/: where this talk's pieces went
INSTRUCTIONS = """On the Mac: open Terminal, type `bash ` (with a space), drag transcribe-mac.sh from the
folder {folder} into the window and press Return. On an Apple Silicon Mac it takes roughly 10 to 20
minutes per hour of talk (the first time also a few minutes to build whisper.cpp). Meetaras picks the
results up by itself."""
# Where a folder shared with the Mac usually is when Meetaras runs in a virtual machine:
# VMware (Fusion), VirtualBox, Parallels.
SHARED_FOLDERS = ("/mnt/hgfs/*", "/media/sf_*", "/media/psf/*")


class MacBundleError(RuntimeError):
    pass


def default_folder() -> Path:
    """meetaras-captions in a folder shared with the Mac if there is one (SHARED_FOLDERS), else in the home folder."""
    for pattern in SHARED_FOLDERS:
        try:
            shared = sorted(d for d in Path("/").glob(pattern.lstrip("/")) if d.is_dir())
        except OSError:
            shared = []
        if shared:
            return shared[0] / "meetaras-captions"
    return Path.home() / "meetaras-captions"


def _copy(src: Path, dest: Path) -> None:
    tmp = dest.with_name(dest.name + ".tmp")
    shutil.copyfile(src, tmp)  # only the bytes: shared folders refuse to copy permissions
    tmp.replace(dest)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def _shared_files(folder: Path) -> None:
    """The model, whisper.cpp source and CMake, each only if the folder doesn't have it yet; the script always."""
    model = folder / tools.MODEL_NAME
    if not model.is_file() or model.stat().st_size != tools.MODEL_SIZE:
        _, have = tools.find_whisper()
        print(f"  copying the model ({tools.MODEL_SIZE / 1e6:.0f} MB, the first time only)", flush=True)
        _copy(have if have else tools.download_model(), model)
    (folder / "model.sha256").write_text(f"{tools.MODEL_SHA256}  {tools.MODEL_NAME}\n")

    source = folder / f"whisper.cpp-{tools.WHISPER_COMMIT[:7]}.tar.gz"
    if not source.is_file():
        repo = tools.whisper_source()
        tmp = source.with_name(source.name + ".tmp")
        subprocess.run(["git", "-C", str(repo), "archive", "--format=tar.gz", "--prefix=whisper.cpp/", "-o", str(tmp),
                        tools.WHISPER_COMMIT], check=True)  # fmt: skip
        tmp.replace(source)

    cmake = folder / CMAKE_TARBALL
    if not cmake.is_file() or _sha256(cmake) != CMAKE_SHA256:
        print("  downloading CMake for the Mac (90 MB, the first time only)", flush=True)
        tmp = cmake.with_name(cmake.name + ".tmp")
        with urllib.request.urlopen(CMAKE_URL, timeout=60) as r, tmp.open("wb") as f:
            shutil.copyfileobj(r, f, 1 << 20)
        if _sha256(tmp) != CMAKE_SHA256:
            tmp.unlink()
            raise MacBundleError("the CMake download is damaged (SHA-256 mismatch); try again")
        tmp.replace(cmake)
    (folder / "cmake.sha256").write_text(f"{CMAKE_SHA256}  {CMAKE_TARBALL}\n")

    script = (
        SCRIPT.replace("@MODEL@", tools.MODEL_NAME).replace("@COMMIT@", tools.WHISPER_COMMIT)
        .replace("@CMAKE@", CMAKE_TARBALL).replace("@FLAGS@", shlex.join(captions.WHISPER_FLAGS))
    )  # fmt: skip
    (folder / "transcribe-mac.sh").write_text(script)


def _transcription(tl_file: Path) -> tuple[captions.Transcription, str]:
    tl = timeline.load(tl_file)
    work = tl_file.parent / ".work"
    return captions.transcription(tl, normalize(tl.presenter, work, "presenter"), work)


def _state(tl_file: Path) -> dict | None:
    try:
        return json.loads((tl_file.parent / ".work" / STATE).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def _talk_dir(folder: Path, tl_file: Path) -> Path:
    """The talk's place in `folder`: the one written last time (it moves with the talk folder, and older
    versions named it after the folder alone), else <folder name>-<hash of where the talk is>."""
    state = _state(tl_file)
    if state and state.get("folder") == str(folder):
        return Path(state["talk_folder"])
    where = hashlib.sha256(str(tl_file.parent.resolve()).encode()).hexdigest()[:6]
    return folder / "talks" / f"{tl_file.parent.name}-{where}"


def write_bundle(tl_file: Path, folder: Path) -> dict:
    """Put this talk's audio pieces (and, the first time, the tools) into `folder` for the Mac."""
    folder = timeline.absolute(folder)
    t, prompt = _transcription(tl_file)
    if t.out.exists():
        raise MacBundleError("this talk has a transcript already")
    talk = _talk_dir(folder, tl_file)
    info_file = talk / "talk.json"
    if info_file.is_file() and json.loads(info_file.read_text()).get("transcript") != t.name:
        shutil.rmtree(talk)  # pieces of other audio (e.g. an earlier audio offset): their results don't fit
    (talk / "pieces").mkdir(parents=True, exist_ok=True)
    (talk / "out").mkdir(exist_ok=True)
    _shared_files(folder)
    print(f"  writing the audio in {len(t.starts)} piece(s) of 10 minutes", flush=True)
    for i in range(len(t.starts)):
        piece = talk / "pieces" / f"part{i:03d}.wav"
        if not piece.is_file():
            tmp = t.work / f"{t.name}-mac{i:03d}.wav"
            t.cut(i, tmp)
            _copy(tmp, piece)
            tmp.unlink()
    (talk / "prompt.txt").write_text(prompt)
    (talk / "language.txt").write_text(t.language)
    info = {"transcript": t.name, "pieces": len(t.starts), "folder": str(folder), "talk_folder": str(talk)}
    info_file.write_text(json.dumps(info))
    (t.work / STATE).write_text(json.dumps(info))
    return info


def results(tl_file: Path) -> dict | None:
    """The talk's bundle and how many of its pieces the Mac has done, or None if there is none."""
    info = _state(tl_file)
    if info is None:
        return None
    out = Path(info["talk_folder"]) / "out"
    info["done"] = sum((out / f"part{i:03d}.json").is_file() for i in range(info["pieces"]))
    return info


def import_results(tl_file: Path, folder: Path | None = None) -> Path:
    """Take the Mac's results into the talk; returns the transcript (exports include captions from now on)."""
    t, _ = _transcription(tl_file)
    if t.out.exists():
        return t.out
    if folder is not None:  # this talk's place there, or where older versions put it
        folder = timeline.absolute(folder)
        places = [_talk_dir(folder, tl_file), folder / "talks" / tl_file.parent.name]
        talk = next((p for p in places if (p / "talk.json").is_file()), None)
    else:
        state = _state(tl_file)
        talk = Path(state["talk_folder"]) if state else None
    if talk is None or not (talk / "talk.json").is_file():
        raise MacBundleError("no folder for the Mac was written for this talk (`meetaras captions --mac-bundle`)")
    if json.loads((talk / "talk.json").read_text())["transcript"] != t.name:
        raise MacBundleError("the pieces on the Mac were made from other audio settings; write the folder again")
    todo = [i for i, part in enumerate(t.parts) if not part.exists()]
    missing = [i for i in todo if not (talk / "out" / f"part{i:03d}.json").is_file()]
    if missing:
        raise MacBundleError(f"the Mac hasn't transcribed {len(missing)} of {len(t.parts)} pieces yet")
    for i in todo:
        data = json.loads((talk / "out" / f"part{i:03d}.json").read_text())
        if "transcription" not in data:
            raise MacBundleError(f"part{i:03d}.json from the Mac is not a whisper.cpp transcript")
        t.parts[i].write_text(json.dumps(data))
    return t.finish()


SCRIPT = r"""#!/bin/bash
# Transcribe talks for Meetaras on this Mac, with whisper.cpp on the GPU (Metal).
# Written by Meetaras (`meetaras captions --mac-bundle`). Run it from Terminal:   bash transcribe-mac.sh
#
# Everything stays in this folder (no sudo, no downloads, nothing installed elsewhere):
#   1. checks the SHA-256 of the model (and of CMake, if it has to be unpacked),
#   2. builds whisper-cli the first time, with Apple's compiler (Xcode Command Line Tools),
#   3. transcribes talks/*/pieces/partNNN.wav into talks/*/out/partNNN.json (finished pieces are skipped).
# Meetaras picks the results up by itself. To undo everything: delete this folder.
set -euo pipefail
cd "$(dirname "$0")"

MODEL=@MODEL@
COMMIT=@COMMIT@
CMAKE_TARBALL=@CMAKE@
CLI="$PWD/whisper.cpp/build/bin/whisper-cli"

echo "== Checking the model"
shasum -a 256 -c model.sha256

built_commit=$(cat whisper.cpp/.meetaras-commit 2>/dev/null || echo "$COMMIT")
if [ ! -x "$CLI" ] || [ "$built_commit" != "$COMMIT" ]; then
  if ! xcode-select -p >/dev/null 2>&1; then
    echo "Apple's command line tools are needed to build whisper.cpp."
    echo "Run:  xcode-select --install   (Apple's installer opens), then run this script again."
    exit 1
  fi
  if command -v cmake >/dev/null 2>&1; then
    CMAKE=cmake
  else
    CMAKE="$PWD/${CMAKE_TARBALL%.tar.gz}/CMake.app/Contents/bin/cmake"
    if [ ! -x "$CMAKE" ]; then
      echo "== Unpacking CMake"
      shasum -a 256 -c cmake.sha256
      tar xzf "$CMAKE_TARBALL"
    fi
  fi
  echo "== Building whisper.cpp ${COMMIT:0:7} (a few minutes)"
  rm -rf whisper.cpp
  tar xzf "whisper.cpp-${COMMIT:0:7}.tar.gz"
  "$CMAKE" -S whisper.cpp -B whisper.cpp/build -DCMAKE_BUILD_TYPE=Release \
    -DGGML_METAL=ON -DGGML_METAL_EMBED_LIBRARY=ON -DBUILD_SHARED_LIBS=OFF \
    -DWHISPER_BUILD_TESTS=OFF -DWHISPER_BUILD_SERVER=OFF
  "$CMAKE" --build whisper.cpp/build --config Release --target whisper-cli -j
  echo "$COMMIT" > whisper.cpp/.meetaras-commit
fi

# The same settings as Meetaras (captions.WHISPER_FLAGS): -nfa keeps the DTW word timings on (flash
# attention disables them); no VAD (it shifts timestamps by seconds).
run_piece() {  # wav, output base, language, prompt file, extra flags
  "$CLI" -m "$MODEL" -f "$1" -l "$3" -t 4 -of "$2" --prompt "$(cat "$4")" @FLAGS@ "${@:5}" 2> "$2.log"
}

has_word_times() {  # at least one word with a DTW time
  grep -Eq '"t_dtw": *[0-9]' "$1"
}

EXTRA=()
start=$(date +%s)
for talk in talks/*/; do
  talk=${talk%/}
  [ -d "$talk/pieces" ] || continue
  mkdir -p "$talk/out"
  language=$(cat "$talk/language.txt")
  total=$(ls "$talk"/pieces/part*.wav | wc -l | tr -d ' ')
  i=0
  for wav in "$talk"/pieces/part*.wav; do
    name=$(basename "$wav" .wav)
    i=$((i + 1))
    [ -f "$talk/out/$name.json" ] && continue
    echo "== $(basename "$talk"): $name ($i of $total, 10 minutes of talk)"
    t0=$(date +%s)
    run_piece "$wav" "$talk/out/$name.tmp" "$language" "$talk/prompt.txt" ${EXTRA[@]+"${EXTRA[@]}"}
    if ! has_word_times "$talk/out/$name.tmp.json"; then
      if [ ${#EXTRA[@]} -eq 0 ]; then
        echo "   no word timings with the GPU: retrying this and later pieces on the CPU"
        EXTRA=(-ng)
        run_piece "$wav" "$talk/out/$name.tmp" "$language" "$talk/prompt.txt" "${EXTRA[@]}"
      fi
      has_word_times "$talk/out/$name.tmp.json" || { echo "No word timings even on the CPU; see $talk/out/$name.tmp.log"; exit 1; }
    fi
    mv "$talk/out/$name.tmp.json" "$talk/out/$name.json"
    echo "   done in $(( $(date +%s) - t0 )) s"
  done
done

echo
echo "All done in $(( $(date +%s) - start )) s. Meetaras picks the results up by itself."
"""
