#!/bin/zsh
# Convert one audiobook (mp3/m4a) + its EPUB into a chaptered M4B.
#
# Usage: convert.sh <audio-file> <epub-file> [output-dir] [--no-evict]
#   output-dir defaults to "<audio's folder>/m4b"
#   --no-evict: keep the source's local copy even when it lives in iCloud Drive
#
# Resumable: re-running after an interruption continues where it left off.
set -u

AUDIO="${1:?usage: convert.sh <audio> <epub> [out-dir] [--no-evict]}"
EPUB="${2:?usage: convert.sh <audio> <epub> [out-dir] [--no-evict]}"
OUTDIR="${3:-}"
[[ "$OUTDIR" == "--no-evict" ]] && OUTDIR=""
KIT_DIR="${0:A:h}"
VENV="${AUDIOBOOK_KIT_VENV:-$HOME/.audiobook-kit-venv}"
WORK="${AUDIOBOOK_KIT_WORK:-$HOME/.audiobook-kit-work}"
[[ -z "$OUTDIR" ]] && OUTDIR="${AUDIO:h}/m4b"

EVICT=1
[[ "$*" == *--no-evict* ]] && EVICT=0
[[ "$AUDIO" != *"Mobile Documents"* ]] && EVICT=0

[[ -f "$AUDIO" ]] || { echo "audio not found: $AUDIO"; exit 1; }
[[ -f "$EPUB" ]] || { echo "epub not found: $EPUB"; exit 1; }
command -v uv >/dev/null || { echo "uv missing — run ./setup.sh first"; exit 1; }
command -v ffmpeg >/dev/null || { echo "ffmpeg missing — run ./setup.sh first"; exit 1; }
mkdir -p "$WORK" "$OUTDIR"

# --- iCloud guard: make sure the source is fully materialized on local disk ---
wait_local() {
  local f="$1" size blocks
  size=$(stat -f %z "$f" 2>/dev/null) || return 1
  blocks=$(stat -f %b "$f" 2>/dev/null || echo 0)
  (( blocks * 512 >= size )) && return 0
  echo "source is evicted from local disk — downloading from iCloud..."
  brctl download "$f" 2>/dev/null
  for i in {1..240}; do
    blocks=$(stat -f %b "$f" 2>/dev/null || echo 0)
    (( blocks * 512 >= size )) && return 0
    sleep 15
  done
  return 1
}
wait_local "$AUDIO" || { echo "TIMED OUT waiting for iCloud download of $AUDIO"; exit 1; }
wait_local "$EPUB" || { echo "TIMED OUT waiting for iCloud download of $EPUB"; exit 1; }

# --- run the resumable pipeline (re-running resumes from the saved state) ---
UV_PROJECT_ENVIRONMENT="$VENV" uv run --project "$KIT_DIR" --no-dev \
  audiobook-kit convert "$AUDIO" "$EPUB" --out "$OUTDIR" --work-dir "$WORK" \
  --detect-intro --intro-max-len 120 --detect-outro --outro-max-tail 180 \
  || { echo "PIPELINE FAILED — progress is kept in $WORK; re-run to resume (or add --fresh via audiobook-kit directly)"; exit 1; }
OUT_M4B=$(ls -t "$OUTDIR"/*.m4b(N) 2>/dev/null | head -1)
[[ -n "$OUT_M4B" && -f "$OUT_M4B" ]] || { echo "no .m4b found in $OUTDIR after the run"; exit 1; }

# --- verification summary ---
src_dur=$(ffprobe -v error -show_entries format=duration -of default=noprint_wrappers=1:nokey=1 "$AUDIO")
out_dur=$(ffprobe -v error -show_entries format=duration -of default=noprint_wrappers=1:nokey=1 "$OUT_M4B")
chapters=$(ffprobe -v error -show_chapters -of csv "$OUT_M4B" 2>/dev/null | grep -c '^chapter')
streams=$(ffprobe -v error -show_entries format=nb_streams -of default=noprint_wrappers=1:nokey=1 "$OUT_M4B")
echo "== DONE: $OUT_M4B =="
echo "   chapters: $chapters | duration: ${out_dur%s*}s (source ${src_dur}s) | streams: $streams (3 = audio + cover + chapter track)"

if (( EVICT )); then
  brctl evict "$AUDIO" 2>/dev/null && echo "   source evicted from local disk (still in iCloud)"
fi
