#!/bin/zsh
# Optional convenience wrapper around `mp3-epub-m4b batch`.
#
# Usage: convert-batch.sh <folder> [mp3-epub-m4b batch options...]
#   e.g. convert-batch.sh ~/Audiobooks/Series --evict --no-readaloud
#
# Pairs "Volume N.mp3|m4a" with "*_N.epub" by default; use --audio-pattern and
# --epub-pattern for other naming. Adds --detect-intro and --detect-outro.
# Run `mp3-epub-m4b batch --help` for all options.
set -eu
KIT_DIR="${0:A:h}"
export UV_PROJECT_ENVIRONMENT="${MP3_EPUB_M4B_VENV:-$HOME/.mp3-epub-m4b-venv}"
exec uv run --project "$KIT_DIR" --no-dev mp3-epub-m4b batch \
  --detect-intro --detect-outro "$@"
