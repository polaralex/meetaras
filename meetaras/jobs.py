"""Long jobs that the app polls (`meetaras make`, setting up captions): one at a time per lock file,
their state in a JSON status file, and started as their own process so they outlive the caller."""

from __future__ import annotations

import fcntl
import json
import os
import subprocess
import threading
import time
from contextlib import contextmanager
from pathlib import Path

GRACE = 30.0  # seconds a started job may take to come up and take its lock


class Busy(RuntimeError):
    """Another process holds the job's lock."""


def write_json(path: Path, data: dict) -> None:
    """Replace the file at once: a reader never sees half of it."""
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1))
    os.replace(tmp, path)


def read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return None


@contextmanager
def lock(path: Path):
    """Hold the lock (released by the OS even if the process is killed); Busy if another has it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Busy(str(path)) from None
        f.truncate(0)
        f.write(f"{os.getpid()}\n")
        f.flush()
        yield


def held(path: Path) -> bool:
    """Whether a process holds the lock now."""
    if not path.exists():
        return False
    with open(path, "a+") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)  # released when the file closes
        except BlockingIOError:
            return True
    return False


def gone(status: dict, lock_path: Path) -> bool:
    """A status that says running, left behind by a job that was killed or crashed.

    A status without a pid was written by `start` before the job's process had come up: it gets
    GRACE seconds to take the lock.
    """
    starting = status.get("pid") is None and time.time() - status["started"] < GRACE
    return status["state"] == "running" and not starting and not held(lock_path)


def start(cmd: list[str], log: Path) -> int:
    """Run cmd as its own process (a new session: it outlives the caller), output to log; its pid."""
    with open(log, "w") as out:
        proc = subprocess.Popen(
            cmd, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT, start_new_session=True,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
        )  # fmt: skip
    threading.Thread(target=proc.wait, daemon=True).start()  # reaps it when started from the long-running app
    return proc.pid
