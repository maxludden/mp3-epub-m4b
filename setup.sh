#!/bin/zsh
# One-time (idempotent) machine setup: ffmpeg, uv, Node >= 24, the readaloud aligner,
# and a local venv for the kit.
# The venv lives in ~/.mp3-epub-m4b-venv by default (override with MP3_EPUB_M4B_VENV);
# keep it off synced folders such as iCloud Drive.
set -u

KIT_DIR="${0:A:h}"
VENV="${MP3_EPUB_M4B_VENV:-$HOME/.mp3-epub-m4b-venv}"

command -v brew >/dev/null || { echo "Homebrew is required: https://brew.sh"; exit 1; }
command -v ffmpeg >/dev/null || brew install ffmpeg || exit 1
command -v uv >/dev/null || brew install uv || exit 1

# Readaloud (stalign) needs Node >= 24; install/upgrade via Homebrew only if missing or too old.
node_ok() { command -v node >/dev/null && (( $(node -p 'process.versions.node.split(".")[0]') >= 24 )); }
if ! node_ok && ! [[ -x /opt/homebrew/bin/node && $(/opt/homebrew/bin/node -p 'process.versions.node.split(".")[0]') -ge 24 ]]; then
  brew install node || brew upgrade node || exit 1
fi

UV_PROJECT_ENVIRONMENT="$VENV" uv sync --project "$KIT_DIR" --no-dev || exit 1
"$VENV/bin/mp3-epub-m4b" --help >/dev/null || { echo "install check failed"; exit 1; }
# Install the aligner into ~/.mp3-epub-m4b-align now (no-op if already there).
"$VENV/bin/python" -c "from mp3_epub_m4b.align import ensure_installed, find_node; ensure_installed(find_node())" \
  || { echo "aligner install failed"; exit 1; }
echo "ready — try: ./convert.sh <audio> [epub]"
