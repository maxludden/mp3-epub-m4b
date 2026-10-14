# Installation

## Requirements


- macOS with [Homebrew](https://brew.sh) (the helper scripts and iCloud handling are macOS-only;
  the Python CLI itself only needs the tools below, so it should work anywhere they do)
- [ffmpeg](https://ffmpeg.org) (with `ffprobe`)
- [uv](https://docs.astral.sh/uv/) and Python 3.12+
- Node 24+, only for the readaloud EPUB step (`--no-readaloud` skips it)

## Setup

```shell
git clone https://github.com/maxludden/mp3-epub-m4b.git
cd mp3-epub-m4b
./setup.sh
```

`setup.sh` installs the requirements above via Homebrew if they're missing, the
readaloud aligner, and a Python venv at `~/.mp3-epub-m4b-venv` (override with
`MP3_EPUB_M4B_VENV`). It is safe to re-run. Without Homebrew, install the
requirements yourself and run `uv sync`.

To run `mp3-epub-m4b` from any directory, install it as a tool:

```shell
uv tool install .
```

(or use the `./convert.sh` / `./convert-batch.sh` wrappers, which call `uv run --project`).
The commands below assume it's on your `PATH`; inside the repo use `uv run mp3-epub-m4b`.

