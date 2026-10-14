#!/bin/zsh
# Optional convenience wrapper around `mp3-epub-m4b convert`; the CLI does all the work.
#
# Usage: convert.sh <audio-file> <epub-file> [mp3-epub-m4b convert options...]
#   e.g. convert.sh a.mp3 a.epub -o ./out --no-readaloud --evict
#
# Adds --detect-intro and --detect-outro (split opening/end credits into their own
# chapters); everything else, including iCloud download/evict and the verification
# summary, is built into the CLI. Run `mp3-epub-m4b convert --help` for all options.
# Environment: MP3_EPUB_M4B_VENV (venv location), MP3_EPUB_M4B_WORK (scratch folder).
set -eu
KIT_DIR="${0:A:h}"
export UV_PROJECT_ENVIRONMENT="${MP3_EPUB_M4B_VENV:-$HOME/.mp3-epub-m4b-venv}"
exec uv run --project "$KIT_DIR" --no-dev mp3-epub-m4b convert \
  --detect-intro --detect-outro "$@"
