"""Progress of long steps for `meetaras make`'s status file; console output stays as it is."""

from __future__ import annotations

from collections.abc import Callable

hook: Callable[[str, float], None] | None = None  # set by a make run: (what, fraction done)


def report(label: str, fraction: float) -> None:
    if hook is not None:
        hook(label, max(0.0, min(1.0, fraction)))
