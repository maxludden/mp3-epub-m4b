#!/bin/zsh
# One-time (idempotent) machine setup: ffmpeg, uv, Node >= 24, the readaloud aligner,
# and a local venv for the kit.
# The venv lives on local disk (not iCloud): ~/.audiobook-kit-venv
set -u

KIT_DIR="${0:A:h}"
VENV="${AUDIOBOOK_KIT_VENV:-$HOME/.audiobook-kit-venv}"

command -v brew >/dev/null || { echo "Homebrew is required: https://brew.sh"; exit 1; }
command -v ffmpeg >/dev/null || brew install ffmpeg || exit 1
command -v uv >/dev/null || brew install uv || exit 1

# Readaloud (stalign) needs Node >= 24; install/upgrade via Homebrew only if missing or too old.
node_ok() { command -v node >/dev/null && (( $(node -p 'process.versions.node.split(".")[0]') >= 24 )); }
if ! node_ok && ! [[ -x /opt/homebrew/bin/node && $(/opt/homebrew/bin/node -p 'process.versions.node.split(".")[0]') -ge 24 ]]; then
  brew install node || brew upgrade node || exit 1
fi

UV_PROJECT_ENVIRONMENT="$VENV" uv sync --project "$KIT_DIR" --no-dev || exit 1
"$VENV/bin/audiobook-kit" --help >/dev/null || { echo "install check failed"; exit 1; }
# Install the aligner into ~/.audiobook-kit-align now (no-op if already there).
"$VENV/bin/python" -c "from audiobook_kit.stalign import ensure_installed, find_node; ensure_installed(find_node())" \
  || { echo "aligner install failed"; exit 1; }
echo "ready — try: ./convert.sh <audio> <epub>"
