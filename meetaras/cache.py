"""Content-addressed keys for intermediate files in .work/."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

# Bump when the output of a pipeline step changes, to invalidate old intermediates.
CACHE_VERSION = 2


def file_fingerprint(path: Path) -> str:
    """Cheap identity for large inputs: name, size and mtime (hashing a 10 GB video is too slow).

    The name rather than the path, so moving a talk folder (or a video referenced from elsewhere)
    to another place or machine keeps its caches.
    """
    st = Path(path).stat()
    return f"{Path(path).name}|{st.st_size}|{st.st_mtime_ns}"


def key(*parts: object) -> str:
    blob = json.dumps([CACHE_VERSION, *parts], sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


@contextmanager
def atomic_output(path: Path) -> Iterator[Path]:
    """A temporary file next to `path` to write to; it becomes `path` when the block succeeds and is
    removed when it fails. Unique per writer, so two runs making the same file don't collide."""
    path = Path(path)
    tmp = path.with_name(f"{path.stem}.tmp-{os.getpid()}-{secrets.token_hex(3)}{path.suffix}")
    try:
        yield tmp
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)
