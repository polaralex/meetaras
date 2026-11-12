"""The tools Meetaras needs, `meetaras doctor` and the app's Setup page: what is there, what is missing,
what it is for and how to install it on this OS.

Commands that need admin rights are only shown, never run. What Meetaras can do itself (`--fix`):
download the captions model (SHA-256 checked) and build whisper.cpp from a pinned commit into its
data folder.
"""

from __future__ import annotations

import hashlib
import os
import platform
import shutil
import subprocess
import sys
import time
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import jobs, progress

MODEL_NAME = "ggml-large-v3-turbo-q5_0.bin"  # part of the transcript cache key: keep the name
MODEL_URL = f"https://huggingface.co/ggerganov/whisper.cpp/resolve/main/{MODEL_NAME}"
MODEL_SHA256 = "394221709cd5ad1f40c46e6031ca61bce88931e6e088c188294c6d5a55ffa7e2"
MODEL_SIZE = 574_041_195
# Measured with meetaras's flags (-nfa for DTW word timings, no VAD) on the VM and on an M3.
WHISPER_REPO = "https://github.com/ggml-org/whisper.cpp"
WHISPER_COMMIT = "6e4ab854f67f743900934a703d5603419384c961"
CMAKE_VERSION = "4.4.4"  # from PyPI through uv when cmake isn't installed; the same as the Mac bundle's
MIN_FREE_GB = 10  # a 1-hour talk needs ~6 GB of intermediate files (4K talks: ~25 GB)


def data_dir() -> Path:
    """meetaras's own folder for tools (whisper.cpp build, model)."""
    if os.environ.get("MEETARAS_DATA"):
        return Path(os.environ["MEETARAS_DATA"]).expanduser()
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "meetaras"
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "meetaras"


def own_whisper() -> Path:
    return data_dir() / "whisper.cpp"


def _cli(whisper_dir: Path) -> Path:
    return whisper_dir / "build" / "bin" / "whisper-cli"


def find_whisper() -> tuple[Path | None, Path | None]:
    """(whisper-cli, model), each None if not found.

    MEETARAS_WHISPER_DIR / MEETARAS_WHISPER_MODEL win when set; otherwise meetaras's own build, then
    whisper-cli on PATH (e.g. Homebrew), with the model from the data folder or next to the build.
    """
    env_dir = os.environ.get("MEETARAS_WHISPER_DIR")
    if env_dir:
        dirs, on_path = [Path(env_dir).expanduser()], None
    else:
        dirs, on_path = [own_whisper()], shutil.which("whisper-cli")
    cli = next((_cli(d) for d in dirs if _cli(d).is_file()), None) or (Path(on_path) if on_path else None)
    if os.environ.get("MEETARAS_WHISPER_MODEL"):
        models = [Path(os.environ["MEETARAS_WHISPER_MODEL"]).expanduser()]
    else:
        models = [data_dir() / "models" / MODEL_NAME] + [d / "models" / MODEL_NAME for d in dirs]
    model = next((m for m in models if m.is_file()), None)
    return cli, model


# ---------- checks ----------


@dataclass
class Check:
    name: str
    ok: bool
    required: bool
    purpose: str
    detail: str = ""  # version, path, or what is wrong
    fix: list[str] = field(default_factory=list)  # commands to copy, or a sentence
    action: str = ""  # something Meetaras can do itself: "captions"


def _run(*cmd: str) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=20).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


def system() -> str:
    """"macos", "debian" (also Ubuntu…), "fedora", "arch" or "linux"."""
    if sys.platform == "darwin":
        return "macos"
    try:
        info = dict(line.split("=", 1) for line in Path("/etc/os-release").read_text().splitlines() if "=" in line)
    except OSError:
        return "linux"
    ids = (info.get("ID", "") + " " + info.get("ID_LIKE", "")).replace('"', "").split()
    return next((x for x in ("debian", "fedora", "arch") if x in ids or (x == "debian" and "ubuntu" in ids)), "linux")


INSTALL = {  # package installs per system; shown, never run
    "ffmpeg": {"debian": "sudo apt install ffmpeg", "fedora": "sudo dnf install ffmpeg  # from RPM Fusion",
               "arch": "sudo pacman -S ffmpeg",
               # Homebrew's plain ffmpeg has no zscale; ffmpeg-full is keg-only, hence the PATH line
               "macos": "brew install ffmpeg-full && echo 'export PATH=\"$(brew --prefix ffmpeg-full)/bin:$PATH\"' >> ~/.zprofile"},
    "build": {"debian": "sudo apt install build-essential git", "fedora": "sudo dnf install gcc-c++ make git",
              "arch": "sudo pacman -S base-devel git", "macos": "xcode-select --install"},
    "uv": {"macos": "brew install uv", "debian": "curl -LsSf https://astral.sh/uv/install.sh | sh",
           "fedora": "curl -LsSf https://astral.sh/uv/install.sh | sh", "arch": "sudo pacman -S uv",
           "linux": "curl -LsSf https://astral.sh/uv/install.sh | sh"},
    "vulkan": {"debian": "sudo apt install libvulkan-dev glslc", "fedora": "sudo dnf install vulkan-loader-devel glslc",
               "arch": "sudo pacman -S vulkan-headers shaderc"},
}  # fmt: skip


def install_hint(what: str) -> str:
    """How to install `what` here: the command for this system, or a sentence."""
    cmd = INSTALL[what].get(system())
    return f"Install it with `{cmd}`." if cmd else f"Install {what} with your system's package manager."


def _install(what: str) -> list[str]:
    cmd = INSTALL[what].get(system())
    return [cmd] if cmd else [install_hint(what)]


def gpu() -> str:
    """The whisper.cpp backend this machine can build: metal, cuda, vulkan or cpu."""
    if sys.platform == "darwin":
        return "metal" if platform.machine() == "arm64" else "cpu"
    if shutil.which("nvcc") or Path("/usr/local/cuda/bin/nvcc").is_file():
        return "cuda"
    if shutil.which("glslc") and Path("/usr/include/vulkan/vulkan.h").is_file():
        return "vulkan"
    return "cpu"


def cmake() -> list[str] | None:
    """How to run CMake: installed, or from PyPI through uv (no admin rights needed)."""
    if shutil.which("cmake"):
        return ["cmake"]
    uv = shutil.which("uv") or next((str(p) for p in (Path.home() / ".local/bin/uv", Path.home() / ".cargo/bin/uv") if p.is_file()), None)
    return [uv, "tool", "run", "--from", f"cmake=={CMAKE_VERSION}", "cmake"] if uv else None


def compiler_ok() -> bool:
    if sys.platform == "darwin":
        return subprocess.run(["xcode-select", "-p"], capture_output=True).returncode == 0
    return bool(shutil.which("c++") or shutil.which("g++") or shutil.which("clang++"))


def checks(library: Path | None = None) -> list[Check]:
    out = []
    ff = shutil.which("ffmpeg")
    if not ff or not shutil.which("ffprobe"):
        out.append(Check("ffmpeg", False, True, "converts, renders and exports every video", "not found", _install("ffmpeg")))
    else:
        version = _run(ff, "-hide_banner", "-version").split("\n")[0].removeprefix("ffmpeg version ").split(" ")[0]
        missing = [name for name, args, word in (
            ("libx264", ("-encoders",), " libx264 "), ("zscale", ("-filters",), " zscale "),
        ) if word not in _run(ff, "-hide_banner", *args)]  # fmt: skip
        detail = f"{version}" + (f"; missing {' and '.join(missing)}" if missing else ", libx264 and zscale")
        fix = ["This ffmpeg lacks " + " and ".join(missing) + " (H.264 export, HDR tone mapping); install a full build:"] + _install("ffmpeg") if missing else []
        out.append(Check("ffmpeg", not missing, True, "converts, renders and exports every video", detail, fix))

    cli, model = find_whisper()
    whisper_fix: list[str] = []
    if not cli:
        build_missing = []
        if not compiler_ok() or not shutil.which("git"):
            build_missing += _install("build")
        if cmake() is None:
            build_missing.append(f"CMake: {_install('uv')[0]} (or install cmake)")
        whisper_fix = (["First install the build tools:"] + build_missing) if build_missing else []
    out.append(Check(
        "whisper.cpp", cli is not None, False, "captions (speech to text, on this machine)",
        str(cli) if cli else f"not found; Set up captions builds it in {own_whisper()} ({gpu()})",
        whisper_fix, "" if cli or whisper_fix else "captions",
    ))  # fmt: skip
    out.append(Check(
        "captions model", model is not None, False, f"{MODEL_NAME} (574 MB), what whisper.cpp recognises speech with",
        str(model) if model else "not downloaded; Set up captions downloads it", [], "" if model else "captions",
    ))  # fmt: skip
    if sys.platform != "darwin" and shutil.which("nvidia-smi") and gpu() != "cuda":
        out.append(Check("CUDA toolkit", False, False, "faster captions on the NVIDIA GPU", "nvcc not found", [
            "Install the CUDA toolkit (12.x for GPUs older than Turing: CUDA 13 dropped Maxwell, Pascal and Volta),",
            "then rebuild whisper.cpp with Set up captions.",
        ]))  # fmt: skip

    claude = shutil.which("claude")
    api = bool(os.environ.get("ANTHROPIC_API_KEY"))
    detail = (_run(claude, "--version").strip() or claude) if claude else ("ANTHROPIC_API_KEY set" if api else "not found")
    out.append(Check(
        "Claude", bool(claude or api), False, "YouTube title, description and tags (chapters work without it)",
        detail, [] if claude or api else ["Install Claude Code (https://claude.com/claude-code) and log in, or set ANTHROPIC_API_KEY."],
    ))  # fmt: skip

    where = library if library and library.exists() else Path.home()
    free = shutil.disk_usage(where).free / 1e9
    out.append(Check(
        "disk space", free >= MIN_FREE_GB, False,
        f"converted videos and clips (~6 GB per hour of talk, ~25 GB for a 4K export) in {where}",
        f"{free:.0f} GB free", [] if free >= MIN_FREE_GB else ["Free some space, or choose a library on a bigger disk."],
    ))  # fmt: skip
    return out


def describe(results: list[Check]) -> str:
    lines = []
    for c in results:
        mark = "ok  " if c.ok else ("MISSING" if c.required else "--  ")
        lines.append(f"{mark:<8}{c.name:<16}{c.detail}")
        if not c.ok:
            lines.append(f"{'':<24}for: {c.purpose}")
            lines += [f"{'':<24}{line}" for line in c.fix]
            if c.action == "captions":
                lines.append(f"{'':<24}Meetaras can do this itself: meetaras doctor --fix")
    return "\n".join(lines)


# ---------- setting up captions ----------


class SetupError(RuntimeError):
    pass


def download_model(url: str = MODEL_URL, sha256: str = MODEL_SHA256, dest: Path | None = None) -> Path:
    """Download the model into the data folder, checking its SHA-256 before it is used."""
    dest = dest or data_dir() / "models" / MODEL_NAME
    if dest.is_file():
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(".part")
    digest = hashlib.sha256()
    print(f"  downloading {url}", flush=True)
    with urllib.request.urlopen(url, timeout=60) as r, part.open("wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        done = 0
        while chunk := r.read(1 << 20):
            f.write(chunk)
            digest.update(chunk)
            done += len(chunk)
            if total:
                progress.report("download", done / total)
    if digest.hexdigest() != sha256:
        part.unlink()
        raise SetupError(f"the download of {dest.name} is damaged (SHA-256 {digest.hexdigest()}, expected {sha256}); try again")
    part.replace(dest)
    return dest


def cmake_args(backend: str) -> list[str]:
    args = ["-DCMAKE_BUILD_TYPE=Release", "-DBUILD_SHARED_LIBS=OFF", "-DWHISPER_BUILD_TESTS=OFF",
            "-DWHISPER_BUILD_SERVER=OFF", "-DWHISPER_BUILD_EXAMPLES=ON"]  # fmt: skip
    return args + {
        "metal": ["-DGGML_METAL=ON", "-DGGML_METAL_EMBED_LIBRARY=ON"],
        "cuda": ["-DGGML_CUDA=ON", "-DCMAKE_CUDA_ARCHITECTURES=native"],
        "vulkan": ["-DGGML_VULKAN=ON"],
        "cpu": [],
    }[backend]


def _step(cmd: list[str], cwd: Path | None = None) -> None:
    print("  $ " + " ".join(cmd), flush=True)
    proc = subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    tail: list[str] = []
    assert proc.stdout is not None
    for line in proc.stdout:
        tail = (tail + [line])[-30:]
        if line.startswith("[") and "%]" in line[:7]:  # cmake --build: "[ 42%] Building …"
            try:
                progress.report("build", int(line[1:4]) / 100)
            except ValueError:
                pass
    if proc.wait() != 0:
        raise SetupError(f"`{' '.join(cmd[:3])} …` failed:\n" + "".join(tail))


def fetch_whisper(dest: Path, commit: str = WHISPER_COMMIT, repo: str = WHISPER_REPO) -> None:
    """A checkout of whisper.cpp at `commit` in dest (git checks the content by its hash)."""
    if not shutil.which("git"):
        raise SetupError("git is needed to get whisper.cpp; see `meetaras doctor`")
    if not (dest / ".git").is_dir():
        dest.mkdir(parents=True, exist_ok=True)
        _step(["git", "init", "-q"], dest)
    if _run("git", "-C", str(dest), "rev-parse", "HEAD").strip() != commit:
        _step(["git", "fetch", "-q", "--depth", "1", repo, commit], dest)
        _step(["git", "checkout", "-q", "--force", "FETCH_HEAD"], dest)


def whisper_source() -> Path:
    """meetaras's own whisper.cpp repository, with the pinned commit fetched if it lacks it."""
    if not shutil.which("git"):
        raise SetupError(f"git is needed to get whisper.cpp's source. {install_hint('build')}")
    repo = own_whisper()
    found = subprocess.run(["git", "-C", str(repo), "cat-file", "-e", f"{WHISPER_COMMIT}^{{commit}}"], capture_output=True)
    if not ((repo / ".git").exists() and found.returncode == 0):
        fetch_whisper(repo)
    return repo


def build_whisper(dest: Path | None = None, commit: str = WHISPER_COMMIT, repo: str = WHISPER_REPO) -> Path:
    """Build whisper-cli at the pinned commit into the data folder."""
    dest = dest or own_whisper()
    cli = _cli(dest)
    if cli.is_file():
        return cli
    make = cmake()
    if make is None or not compiler_ok() or not shutil.which("git"):
        raise SetupError("building whisper.cpp needs a C++ compiler, git and CMake (or uv); see `meetaras doctor`")
    fetch_whisper(dest, commit, repo)
    backend = gpu()
    print(f"  building whisper.cpp {commit[:7]} for {backend} (a few minutes)", flush=True)
    _step([*make, "-S", ".", "-B", "build", *cmake_args(backend)], dest)
    _step([*make, "--build", "build", "--config", "Release", "--target", "whisper-cli", "-j", str(os.cpu_count() or 4)], dest)
    if not cli.is_file():
        raise SetupError(f"the build finished but {cli} is missing")
    return cli


# The app runs `meetaras doctor --fix` as its own process (like Make video) and polls this file.
def _setup_files() -> tuple[Path, Path, Path]:
    d = data_dir()
    return d / "setup.json", d / "setup.log", d / "setup.lock"


def setup_status() -> dict | None:
    status, _, lock = _setup_files()
    data = jobs.read_json(status)
    if data and jobs.gone(data, lock):
        data["state"] = "interrupted"
    return data


def _write_status(**data) -> None:
    status, _, _ = _setup_files()
    status.parent.mkdir(parents=True, exist_ok=True)
    jobs.write_json(status, {**(setup_status() or {}), **data})


def set_up_captions() -> None:
    """Download the model and build whisper.cpp, whichever is missing (`meetaras doctor --fix`)."""
    _, _, lock = _setup_files()
    try:
        with jobs.lock(lock):
            _set_up_captions()
    except jobs.Busy:
        raise SetupError("captions are being set up already") from None


def _set_up_captions() -> None:
    _write_status(state="running", started=time.time(), step="", progress=None, error=None)
    last = [0.0]

    def report(label: str, fraction: float) -> None:
        if time.monotonic() - last[0] >= 1 or fraction >= 1:
            last[0] = time.monotonic()
            _write_status(step=label, progress=round(fraction, 3))

    progress.hook = report
    try:
        cli, model = find_whisper()
        if model is None:
            _write_status(step="download", progress=0)
            download_model()
        if cli is None:
            _write_status(step="build", progress=0)
            build_whisper()
        _write_status(state="done", step="", progress=None)
    except BaseException as exc:
        _write_status(state="failed", error=str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__)
        raise
    finally:
        progress.hook = None


def start_setup() -> None:
    """Run `meetaras doctor --fix` in the background (for the app); its log goes to setup.log."""
    status = setup_status()
    if status and status["state"] == "running":
        raise SetupError("captions are being set up already")
    _write_status(state="running", started=time.time(), step="", progress=None, error=None)
    _, log, _ = _setup_files()
    jobs.start([sys.executable, "-m", "meetaras.cli", "doctor", "--fix"], log)


def setup_log() -> list[str]:
    _, log, _ = _setup_files()
    return log.read_text(errors="replace").splitlines()[-40:] if log.exists() else []


def checks_json(library: Path | None = None) -> list[dict]:
    return [asdict(c) for c in checks(library)]
