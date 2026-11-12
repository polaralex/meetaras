#!/bin/bash
# Pre-convert iPhone recordings on the Mac before copying them to the machine that runs meetaras.
#
# Uses only Apple's built-in /usr/bin/avconvert (hardware video engine). Installs nothing,
# downloads nothing, changes nothing except writing the converted files.
#
# Converts HEVC/HDR recordings to 1080p H.264, which meetaras decodes much faster.
# The originals are not touched; output goes next to each file as <name>-1080p.mp4.
#
# Usage:
#   bash mac-preconvert.sh IMG_1234.MOV [more files...]
#   bash mac-preconvert.sh --start 600 --duration 180 IMG_1234.MOV   # just a 3-minute test piece
#
# Afterwards, use the -1080p.mp4 file as the talk's video (`meetaras new --video`, or `presenter` in timeline.txt).

set -euo pipefail

PRESET="Preset1920x1080"   # H.264, 1920x1080
start=""
duration=""

usage() { sed -n '2,16p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }

files=()
while [ $# -gt 0 ]; do
  case "$1" in
    --start) start="$2"; shift 2 ;;
    --duration) duration="$2"; shift 2 ;;
    -h|--help) usage ;;
    -*) echo "unknown option: $1" >&2; usage 1 ;;
    *) files+=("$1"); shift ;;
  esac
done
[ ${#files[@]} -gt 0 ] || usage 1

if [ ! -x /usr/bin/avconvert ]; then
  echo "avconvert not found; this script needs macOS (it ships with the system)." >&2
  exit 1
fi

for src in "${files[@]}"; do
  if [ ! -f "$src" ]; then
    echo "skipping $src: not a file" >&2
    continue
  fi
  base="${src%.*}"
  suffix="-1080p"
  [ -n "$start$duration" ] && suffix="-1080p-from${start:-0}s"
  out="${base}${suffix}.mp4"
  if [ -e "$out" ]; then
    echo "skipping $src: $out already exists (delete it to convert again)"
    continue
  fi

  args=(--source "$src" --output "$out" --preset "$PRESET" --progress)
  [ -n "$start" ] && args+=(--start "$start")
  [ -n "$duration" ] && args+=(--duration "$duration")

  echo "Converting $src → $out"
  t0=$(date +%s)
  if ! /usr/bin/avconvert "${args[@]}"; then
    echo >&2
    echo "avconvert failed for $src. Its own help, which lists the presets this macOS offers:" >&2
    /usr/bin/avconvert --help 2>&1 | sed 's/^/  /' >&2 || true
    rm -f "$out"
    exit 1
  fi
  t1=$(date +%s)
  echo "Done in $((t1 - t0)) s: $out ($(du -h "$out" | cut -f1))"
done
