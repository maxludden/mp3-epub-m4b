#!/bin/zsh
# Align an audiobook with its EPUB, producing an EPUB 3 with Media Overlays,
# using @storyteller-platform/align (https://www.npmjs.com/package/@storyteller-platform/align).
#
# Usage: align.sh <audiobook> <epub> [output.epub] [options]
#   <audiobook>   an .m4b/.mp3/.m4a file, or a directory of audio files
#   output.epub   defaults to "<epub name> (aligned).epub" next to the EPUB
#
# Options:
#   -g, --granularity word|sentence   alignment granularity (default: sentence)
#   -l, --language LOCALE             BCP 47 audio language (default: en-US)
#   -d, --device DEVICE               auto|cpu|webgpu|dml|cuda|mlx (default: auto)
#       --engine whisper.cpp          transcribe with whisper.cpp instead of CTC
#   -m, --model MODEL                 CTC model (default mms) or whisper model
#       --codec copy|aac|libopus|libmp3lame   audio codec (default: copy)
#       --progress                    show progress bars (hidden by default)
#   -h, --help
# Any other arguments are passed straight through to `align`.
#
# Notes:
#   - Needs Node >= 24 (the tool uses DisposableStack); a suitable Node is
#     looked for on PATH and in Homebrew.
#   - The tool is installed once into ~/.audiobook-kit-align (with the MLX
#     backend on Apple Silicon), so nothing depends on the npx cache.
#   - `align --audiobook` requires a directory, so a single audio file is
#     staged into a temporary directory via symlink.
set -u

# ---------------------------------------------------------------------------
# Step 1: Setup. (Everything above `set -u` is the help text: usage() prints
# it, so keep new comments below this line.)
# ---------------------------------------------------------------------------

# Absolute path of this script (zsh `:A` modifier), so usage() can read it.
SELF="${0:A}"
# Print the header comment block (line 2 up to `set -u`), dropping the
# trailing `set -u` line and the leading "# " of each line.
usage() { sed -n '2,/^set -u/p' "$SELF" | sed '$d' | sed 's/^# \{0,1\}//'; }

# Where the aligner is installed once, and where per-run logs are written.
TOOL_DIR="$HOME/.audiobook-kit-align"
LOG_DIR="$HOME/.audiobook-kit-work/align-logs"

# Option defaults. Empty strings mean "not given, let `align` choose".
GRAN="sentence"; LANGUAGE="en-US"; DEVICE=""; ENGINE=""; MODEL=""; CODEC=""
PROGRESS=0
POSITIONAL=(); EXTRA=()  # positional args / unrecognised flags to pass through

# ---------------------------------------------------------------------------
# Step 2: Parse command-line arguments.
# ---------------------------------------------------------------------------
# Known options consume their value (`${2:?...}` aborts with a message if it
# is missing); unknown -flags are collected in EXTRA for `align`; everything
# else is positional (audiobook, epub, optional output path).
while (( $# )); do
  case "$1" in
    -h|--help) usage; exit 0 ;;
    -g|--granularity) GRAN="${2:?missing value for $1}"; shift 2 ;;
    -l|--language) LANGUAGE="${2:?missing value for $1}"; shift 2 ;;
    -d|--device) DEVICE="${2:?missing value for $1}"; shift 2 ;;
    --engine|-e) ENGINE="${2:?missing value for $1}"; shift 2 ;;
    -m|--model) MODEL="${2:?missing value for $1}"; shift 2 ;;
    --codec) CODEC="${2:?missing value for $1}"; shift 2 ;;
    --progress) PROGRESS=1; shift ;;
    -*) EXTRA+=("$1"); shift ;;
    *) POSITIONAL+=("$1"); shift ;;
  esac
done

# ---------------------------------------------------------------------------
# Step 3: Resolve paths and validate inputs.
# ---------------------------------------------------------------------------
# Need at least the audiobook and the EPUB.
(( ${#POSITIONAL[@]} >= 2 )) || { usage; exit 1; }
# `:A` makes each path absolute. The default output is "<epub> (aligned).epub"
# next to the EPUB (`:r` strips the extension).
AUDIO="${POSITIONAL[1]:A}"
EPUB="${POSITIONAL[2]:A}"
OUT="${POSITIONAL[3]:-${EPUB:r} (aligned).epub}"
OUT="${OUT:A}"

# Fail early: inputs must exist, output must be a new .epub (never overwrite),
# and ffmpeg must be installed.
[[ -e "$AUDIO" ]] || { echo "audiobook not found: $AUDIO"; exit 1; }
[[ -f "$EPUB" ]] || { echo "epub not found: $EPUB"; exit 1; }
[[ "$OUT" == *.epub ]] || { echo "output must end in .epub: $OUT"; exit 1; }
[[ -e "$OUT" ]] && { echo "output already exists, refusing to overwrite: $OUT"; exit 1; }
command -v ffmpeg >/dev/null || { echo "ffmpeg missing (brew install ffmpeg)"; exit 1; }

# ---------------------------------------------------------------------------
# Step 4: Find a Node >= 24 (the aligner needs it).
# ---------------------------------------------------------------------------
# Print the major version of the given node binary, or 0 if it won't run.
node_major() { "$1" -p 'process.versions.node.split(".")[0]' 2>/dev/null || echo 0; }
NODE=""
# Try the node on PATH first, then the usual Homebrew locations; take the
# first one that is new enough.
for cand in "$(command -v node 2>/dev/null)" /opt/homebrew/bin/node /usr/local/bin/node; do
  [[ -n "$cand" && -x "$cand" ]] || continue
  (( $(node_major "$cand") >= 24 )) && { NODE="$cand"; break; }
done
[[ -n "$NODE" ]] || { echo "Node >= 24 required (brew install node)"; exit 1; }
# Put the chosen node first on PATH so the aligner's scripts use it, and use
# the npm that ships beside it (falling back to whatever `npm` is on PATH).
export PATH="${NODE:h}:$PATH"
NPM="${NODE:h}/npm"; [[ -x "$NPM" ]] || NPM="npm"

# ---------------------------------------------------------------------------
# Step 5: One-time install of the aligner (+ MLX backend on Apple Silicon).
# ---------------------------------------------------------------------------
ALIGN_BIN="$TOOL_DIR/node_modules/.bin/align"
# Skipped on every run after the first, once the `align` binary exists.
if [[ ! -x "$ALIGN_BIN" ]]; then
  echo "installing @storyteller-platform/align into $TOOL_DIR ..."
  mkdir -p "$TOOL_DIR"
  pkgs=(@storyteller-platform/align)
  # Add the MLX audio backend only on Apple Silicon Macs.
  [[ "$(uname -s)-$(uname -m)" == "Darwin-arm64" ]] && pkgs+=(@storyteller-platform/mlx-audio)
  # Create a package.json if needed, then install into TOOL_DIR.
  (cd "$TOOL_DIR" && { [[ -f package.json ]] || "$NPM" init -y >/dev/null; } \
    && "$NPM" install "${pkgs[@]}") || { echo "install failed"; exit 1; }
fi

# ---------------------------------------------------------------------------
# Step 6: Stage the audiobook. `align --audiobook` requires a directory.
# ---------------------------------------------------------------------------
STAGE=""
# Remove the temporary staging directory (if one was made) on any exit.
cleanup() { [[ -n "$STAGE" ]] && rm -rf "$STAGE"; }
trap cleanup EXIT
if [[ -d "$AUDIO" ]]; then
  # Already a directory of audio files: use it as is.
  AUDIO_DIR="$AUDIO"
else
  # A single file: symlink it into a fresh temp directory (no copying).
  STAGE="$(mktemp -d)"
  ln -s "$AUDIO" "$STAGE/${AUDIO:t}"
  AUDIO_DIR="$STAGE"
fi

# ---------------------------------------------------------------------------
# Step 7: Build the `align` command line.
# ---------------------------------------------------------------------------
# Base arguments: inputs, output, granularity, language; `-u` upgrades EPUB 2
# to EPUB 3 automatically, `--time` prints timings, `--log-level info` logs.
cmd=("$ALIGN_BIN" --audiobook "$AUDIO_DIR" --epub "$EPUB" --output "$OUT"
     -g "$GRAN" --language "$LANGUAGE" -u --time --log-level info)
# An explicit --engine (e.g. whisper.cpp) selects transcription-based
# alignment; otherwise use the default CTC aligner, which also takes --device.
if [[ -n "$ENGINE" ]]; then
  cmd+=(--engine "$ENGINE"); [[ -n "$MODEL" ]] && cmd+=(--model "$MODEL")
else
  cmd+=(--ctc)
  [[ -n "$DEVICE" ]] && cmd+=(--device "$DEVICE")
  [[ -n "$MODEL" ]] && cmd+=(--model "$MODEL")
fi
# Optional audio codec; hide progress bars unless --progress was given (they
# garble the log); finally append any pass-through arguments.
[[ -n "$CODEC" ]] && cmd+=(--codec "$CODEC")
(( PROGRESS )) || cmd+=(--no-progress)
cmd+=("${EXTRA[@]}")

# ---------------------------------------------------------------------------
# Step 8: Run it, logging to a timestamped file.
# ---------------------------------------------------------------------------
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/${EPUB:t:r}-$(date +%Y%m%d-%H%M%S).log"
echo "node:   $NODE ($("$NODE" -v))"
echo "output: $OUT"
echo "log:    $LOG"

# Show output live and save it. `pipestatus[1]` is align's own exit code
# (a plain $? would report tee's).
"${cmd[@]}" 2>&1 | tee "$LOG"
rc=${pipestatus[1]}
# ---------------------------------------------------------------------------
# Step 9: Report the result.
# ---------------------------------------------------------------------------
if (( rc != 0 )); then
  echo "align failed (exit $rc) — see $LOG"
  exit $rc
fi
# On success, print the output path and its size.
[[ -f "$OUT" ]] && echo "DONE: $OUT ($(du -h "$OUT" | cut -f1))"
