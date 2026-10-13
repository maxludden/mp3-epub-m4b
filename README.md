# Audiobook Conversion Kit

Turns a single-file audiobook (`.mp3` / `.m4a`) plus its EPUB into a chaptered
`.m4b` with accurate chapter boundaries (detected from silence gaps in the audio,
anchored chapter-by-chapter so they can't drift), embedded cover art, and the
opening/end credits split into their own chapters. Works on any Mac — this folder
syncs via iCloud, so it's already on all of them.

## One-time setup (per machine)

```shell
cd "/Users/maxludden/dev/py/audiobook-kit"
./setup.sh
```

Installs ffmpeg and Node >= 24 (via Homebrew), the readaloud aligner, and a small Python venv at `/Users/maxludden/dev/py/audiobook-kit/.venv`.
*(Safe to re-run; does nothing if already set up...)*

## Convert one book

```shell
./convert.sh "/path/to/Volume 1.mp3" "/path/to/book_01.epub"
```

The M4B lands in an `m4b/` folder next to the audio (or pass a third argument for
a different output dir). Takes roughly 30–40 minutes per ~24 h book.

## Convert a whole series

```shell
./convert-batch.sh "/path/to/series folder"
```

Expects the naming convention `Volume N.mp3` (or `.m4a`) paired with `..._NN.epub`
(zero-padded), e.g. `Volume 3.mp3` + `defiance_of_the_fall_03.epub`. Unmatched
files are reported and skipped, and the batch stops if disk space drops below 8 GB.

## Good to know

- **The EPUB is required.** It provides chapter titles, word counts for
  sanity-checking the cuts, metadata, and the full-resolution cover.
- **Readaloud EPUB.** Once the M4B is delivered, `convert` and `batch` automatically
  align it with the EPUB (the `stalign` step, needs Node >= 24) and write
  `<epub name> (aligned).epub` next to the `.m4b`. This is slow; pass
  `--no-readaloud` to skip it, or `--align-device` / `--align-language` to tune it.
  If alignment fails the M4B is kept, and re-running resumes at the alignment.
  Re-running on an already-converted book adds the readaloud EPUB.
- **Interruptions are fine.** The pipeline checkpoints constantly; re-running the
  same command resumes instead of starting over.
- **iCloud-aware.** If a source file has been evicted from local disk ("cloud"
  icon in Finder), the script downloads it first, and by default evicts it again
  afterward to give the space back (pass `--no-evict` to keep local copies).
- **Verify line.** Each finished book prints chapter count, duration vs source,
  and stream count (3 = audio + cover + chapter track). If duration doesn't match the source,
  something went wrong — re-run or investigate before trusting the file.
- All heavy temp work happens in `~/.audiobook-kit-work` on the local disk, never
  in iCloud.

## Using the CLI directly

`convert.sh` is a thin wrapper around the `audiobook-kit` command (run it with
`uv run audiobook-kit ...` from this folder):

```shell
audiobook-kit convert "Volume 1.mp3" book_01.epub -o ./m4b
audiobook-kit batch "/path/to/series" --audio-pattern 'Book (\d+)' --epub-pattern '_(\d+)$'
```

- Each book also gets a full debug log at `<work folder>/<book>/pipeline.log`. Set
  `AUDIOBOOK_KIT_LOG_LEVEL=DEBUG` to see debug messages on the console too.
- `--fresh` discards saved progress for the book; `--overwrite` replaces an existing `.m4b`.
- Saved progress is tied to the audio/EPUB files and options it was started with;
  changing them (for example `--min-gap`) stops with a clear message instead of
  silently reusing stale state. Use `--fresh` to start over.
- The pipeline scans the audio for silences once (cached in the work folder), places chapters in memory, then encodes the M4B in  single pass, so chapter joins are gapless.

## Development

```shell
uv sync            # includes pytest, mypy, ruff
uv run pytest      # needs ffmpeg for the end-to-end tests
uv run mypy src && uv run ruff check src tests
```
