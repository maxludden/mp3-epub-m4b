# Development


- `cli.py` — Typer CLI (`convert`, `batch`, `stalign`); `options.py` — shared option definitions;
  `converter.py` — runs one book with progress bars; `pairing.py` — matches audio to EPUBs
- `run.py` — the resumable pipeline (stages: extract, scan, align, assemble, deliver, readaloud)
- `state.py` — checkpoint types, fingerprint and persistence
- `chapters.py` — chapter placement from the silence scan, segments, chapter metadata
- `epub_extract.py` — EPUB metadata, chapters, word counts and cover
- `ffmpeg_tools.py` — ffmpeg/ffprobe helpers (silence scan, probing)
- `report.py` — chapter table and output verification
- `icloud.py` — iCloud Drive download/evict (macOS)
- `align.py` — readaloud alignment wrapper; `log.py` — logging setup

## Checks

```shell
uv sync            # includes pytest, mypy, ruff
uv run pytest      # tests marked `ffmpeg` are skipped if ffmpeg isn't installed
uv run mypy src tests && uv run ruff check . && uv run ruff format --check .
```

CI runs the same checks (`.github/workflows/ci.yml`); `.pre-commit-config.yaml` runs them
locally. MIT licensed — see `LICENSE`.
