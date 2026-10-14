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
- `--align-by auto|silence|transcribe` — how chapters are placed. `auto` (the default) tries
  silence first and switches to `transcribe` when silence fails or looks unreliable (chapters
  placed by guess, or many far from the length their word count predicts). `transcribe`
  listens instead: it transcribes a window around where each chapter should start and finds
  the chapter's opening words (epigraph included) in it, so it works when nothing separates
  chapters. It needs whisper.cpp and takes roughly 15 minutes for a 20-hour book. `silence`
  never transcribes. Without whisper.cpp, `auto` behaves exactly like `silence`.
- `--whisper-model` — GGML model for `transcribe` (default: the first `ggml-*.en.bin` in
  `~/.cache/whisper-models` or `~/.cache/whisper.cpp`; also `MP3_EPUB_M4B_WHISPER_MODEL`).
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
- **Checking on a long run.** While a book converts, the progress bars show a line under
  them saying what is happening (for example, which stretch of audio is being listened to).
  From another terminal, `mp3-epub-m4b status` reports every book in the work folder:
  stage checklist, chapters placed, speed, time left and whether the process is still alive.
  Give it an audio file or a work folder for one book, and add `--watch` to keep it
  refreshing until nothing is running.

