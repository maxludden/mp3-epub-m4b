# Options and behavior

## Options worth knowing


- `--dry-run` — print the chapter table only (see [Convert one book](convert.md)).
- `--verify / --no-verify` — probe the finished M4B and report chapters, duration and
  streams (on by default; warns if the duration differs from the source).
- `--evict` — afterwards, evict the source audio from local disk. Only acts on files in
  iCloud Drive (macOS). Evicted sources are always downloaded first, automatically.
- `--detect-intro` / `--detect-outro` — split opening / end credits into their own
  chapters. Off by default: auto-detection can misfire, and a false positive shifts every
  later chapter, so check the result with `--dry-run`.
- `--min-gap`, `--bitrate`, `--overwrite`, `--fresh` — see `--help`.
- `--readaloud / --no-readaloud` — after delivery, align the M4B with the EPUB into an
  EPUB 3 with Media Overlays (`<epub name> (aligned).epub`, needs Node 24+). It is slow.
  If alignment fails the M4B is kept and re-running resumes at the alignment.
  `--align-device` and `--align-language` tune it; `mp3-epub-m4b stalign AUDIO EPUB`
  runs only this step.

## Good to know

- **The EPUB is required.** It provides chapter titles, word counts for sanity-checking
  the cuts, metadata, and the cover.
- **Interruptions are fine.** Progress is checkpointed; re-running the same command
  resumes. Saved progress is tied to the files and options it was started with —
  changing them (for example `--min-gap`) stops with a clear message; use `--fresh`.
- **Where things go.** Scratch work lives in `~/.mp3-epub-m4b-work` (override with
  `--work-dir` or `MP3_EPUB_M4B_WORK`). Each book gets a debug log at
  `<work folder>/<book>/pipeline.log`; set `MP3_EPUB_M4B_LOG_LEVEL=DEBUG` (or `TRACE`) for
  more detail on the console. Logging uses [loguru](https://github.com/Delgan/loguru) and is
  silent when the package is imported as a library until you call
  `logger.enable("mp3_epub_m4b")`.
- The audio is scanned for silences once (cached), chapters are placed in memory, and the
  M4B is encoded in a single pass, so chapter joins are gapless.

