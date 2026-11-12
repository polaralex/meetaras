# Contributing

Install as in the [README](README.md); `uv sync` also installs the development tools.

## Tests

```sh
uv run pytest -m "not slow and not ui and not whisper"   # quick check, ~40 s (what CI runs on every push)
uv run pytest -n auto --dist loadfile                    # everything, in parallel (~3.5 min)
```

Fixtures are generated with ffmpeg, so the tests need: ffmpeg 6+ with libx264, libx265 and zscale
(on macOS Homebrew's `ffmpeg-full`; on Debian/Ubuntu `ffmpeg`), git and bash. The `ui` tests drive Chromium
(`chromium` on PATH, or `uv run playwright install chromium`) and are skipped without it; the `whisper`
test needs a whisper.cpp build and model (`meetaras doctor --fix`). No network needed.

## Working with Claude Code

[CLAUDE.md](CLAUDE.md) explains the project to [Claude Code](https://claude.com/claude-code): how it
works, the code map and the conventions. Facts about your own setup that Claude should know (where your
recordings arrive, talks it must not touch) go in a `CLAUDE.local.md` next to it, which Claude Code also
reads and git ignores.
