# mp3+epub=m4b

Turns a single-file audiobook (`.mp3` / `.m4a`) plus its EPUB into a chaptered
`.m4b` with accurate chapter boundaries (detected from silence gaps in the audio,
anchored chapter-by-chapter so they can't drift), embedded cover art, and
optionally the opening/end credits split into their own chapters.

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

## Convert one book

```shell
uv run mp3-epub-m4b convert "Volume 1.mp3" book_01.epub
```

The M4B lands in an `m4b/` folder next to the audio (or pass `-o DIR`). Expect roughly
30–40 minutes for a ~24 h book. The optional `./convert.sh` wrapper does the same and
turns on `--detect-intro --detect-outro`.

**Preview the chapters first.** `--dry-run` scans the audio, places the chapters and
prints the table without encoding anything, so a bad boundary shows up in minutes
instead of after the encode. A real run afterwards reuses the saved scan:

```text
$ mp3-epub-m4b convert "Volume 1.mp3" book_01.epub --dry-run
extracted: Fish & Chips: A Test by Ann Author, 3 chapters, cover=yes
scanning Volume 1.mp3 for silences
scan: found 5 silences
chapter 3: ignored 1 gap(s) earlier than 27s after the previous chapter (scene break?)
Chapters
┏━━━┳━━━━━━━━━━━━━━━━━┳━━━━━━━━━┳━━━━━━━━━┳━━━━━━━┳━━━━━━┓
┃ # ┃ Title           ┃   Start ┃  Length ┃ Words ┃ Note ┃
┡━━━╇━━━━━━━━━━━━━━━━━╇━━━━━━━━━╇━━━━━━━━━╇━━━━━━━╇━━━━━━┩
│ 1 │ First Light     │ 0:00:00 │ 0:00:32 │   302 │      │
│ 2 │ Tom & Jerry     │ 0:00:32 │ 0:01:11 │   603 │      │
│ 3 │ Fate = Doom; #3 │ 0:01:43 │ 0:00:45 │   404 │      │
└───┴─────────────────┴─────────┴─────────┴───────┴──────┘
                3 chapters, 0:02:28 total
```

Rows are highlighted when a chapter was placed with low confidence (no companion
gap), by fallback (no gap found), or when its length is far from what its word
count predicts.

After a real run the output is verified:

```text
Fish & Chips A Test.m4b: chapters: 3 | duration: 0:02:28 (source 0:02:28) | streams: 3
```

## Convert a whole series

```shell
uv run mp3-epub-m4b batch "/path/to/series folder"
```

By default it pairs `Volume N.mp3` (or `.m4a`) with `..._N.epub` (e.g. `Volume 3.mp3` +
`my_series_03.epub`). For other names use `--audio-pattern` and `--epub-pattern`
(regexes with one number group):

```shell
uv run mp3-epub-m4b batch ./series --audio-pattern 'Book (\d+)' --epub-pattern '_(\d+)$'
```

Unmatched files are reported and skipped, and the batch stops if free disk space
drops below 8 GB. `./convert-batch.sh` is an optional wrapper.

## Options worth knowing

- `--dry-run` — print the chapter table only (see above).
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
  `<work folder>/<book>/pipeline.log`; set `MP3_EPUB_M4B_LOG_LEVEL=DEBUG` for debug
  output on the console.
- The audio is scanned for silences once (cached), chapters are placed in memory, and the
  M4B is encoded in a single pass, so chapter joins are gapless.

## Troubleshooting

**A chapter starts mid-word or mid-sentence.** Narrators often put a short "companion"
pause a few seconds after the real chapter gap, and a long pause inside a chapter (a scene
break) can look like a boundary. Run `--dry-run` and look for rows marked
`low-confidence`; try `--min-gap` (lower to catch shorter transitions, higher to ignore
scene breaks) with `--fresh`. If the credits at the start or end are the culprit, toggle
`--detect-intro` / `--detect-outro`.

**"output is Ns but the source is Ms" / `verify: duration is ...`.** The encode and the
source should match within about 3 seconds. A larger difference usually means the encode
was cut short (disk full, interrupted copy). Re-run with `--fresh --overwrite`; if it
persists, check the source plays to the end.

**"N segment(s) deviate >30% from the word-count estimate."** A chapter is much longer or
shorter than its word count predicts, which suggests two chapters merged or one was
skipped. Check those rows in the `--dry-run` table against the audio before trusting the
output. A warning that the average chapter length is implausible means the EPUB probably
isn't the right one for this audio.

**Alignment fails ("ran out of audio", "would start at the very end", or a readaloud
error).** The EPUB has more chapters than the audio, or the two are different editions.
For a readaloud failure the M4B is already delivered; fix the cause (Node 24+, enough
disk) and re-run to resume at the alignment, or pass `--no-readaloud`.

**"was made for different input/options".** Saved progress belongs to other files or
settings. Re-run with the original settings, or add `--fresh`.

## Layout

- `cli.py` — Typer CLI (`convert`, `batch`, `stalign`); `options.py` — shared option definitions;
  `converter.py` — runs one book with progress bars; `pairing.py` — matches audio to EPUBs
- `run.py` — the resumable pipeline (stages: extract, scan, align, assemble, deliver, readaloud)
- `state.py` — checkpoint types, fingerprint and persistence
- `chapters.py` — chapter placement from the silence scan, segments, chapter metadata
- `epub_extract.py` — EPUB metadata, chapters, word counts and cover
- `ffmpeg_tools.py` — ffmpeg/ffprobe helpers (silence scan, probing)
- `report.py` — chapter table and output verification
- `icloud.py` — iCloud Drive download/evict (macOS)
- `stalign.py` — readaloud alignment wrapper; `log.py` — logging setup

## Development

```shell
uv sync            # includes pytest, mypy, ruff
uv run pytest      # tests marked `ffmpeg` are skipped if ffmpeg isn't installed
uv run mypy src tests && uv run ruff check . && uv run ruff format --check .
```

CI runs the same checks (`.github/workflows/ci.yml`); `.pre-commit-config.yaml` runs them
locally. MIT licensed — see `LICENSE`.
