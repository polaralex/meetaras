# Command line and timeline file

Everything the app does is also a command; the app and the commands share the same steps. Run them as
`uv run meetaras …` from the repository.

## Commands

```sh
meetaras                                              # the app at http://127.0.0.1:8765
meetaras new ~/talks/mytalk --video IMG_1234.MOV --slides deck.pdf [--screen rec.mov] [--audio mic.wav] \
    [--intro intro.mp4] [--outro outro.mp4] [--talk "…" --speaker "…" --event "…" --date "…"] \
    [--resolution auto|1080p|4k]                       # export size; auto: match the recordings
meetaras make ~/talks/mytalk [--review] [--detach]   # every step; finished ones are skipped
meetaras status ~/talks/mytalk
meetaras edit ~/talks/mytalk                          # the app at that talk
meetaras captions ~/talks/mytalk [--mac-bundle DIR | --import DIR]
meetaras metadata ~/talks/mytalk [--no-ai]
meetaras doctor [--fix]                               # check the tools; --fix sets up captions
```

Single steps: `meetaras draft | captions | render [--preview] [--resolution 1080p|4k] | check | align`
(`--help` on each).

## The timeline file

Each talk is a folder with a plain-text `timeline.txt`; the app only edits that file, and you can too.
Each line after the header means "from this time on, show this".

```
presenter  /Volumes/Share/IMG_1234.MOV
slides     deck.pdf
screen     screen.mov        # optional; screen_offset = talk time where it starts
intro      ../assets/intro.mp4
speaker    Ada Lovelace
talk       Engines and Ideas
resolution 4k                # optional: export in 4K (default 1080p)

0:00   start
0:12   presenter
0:45   slide 1               # slide large, speaker in the corner
2:10   slide 2 speaker       # speaker large, slide small   (also: side, full)
5:30   cut                   # left out until the next line
6:15   screen                # screen recording, speaker in the corner
41:20  end
```

Paths are relative to the talk folder unless absolute. Converted videos and other intermediate files
are cached in `<talk>/.work/` (safe to delete; they are made again). A talk folder can be moved.
