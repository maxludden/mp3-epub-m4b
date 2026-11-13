# Troubleshooting


**A chapter starts mid-word or mid-sentence.** Narrators often put a short "companion"
pause a few seconds after the real chapter gap, and a long pause inside a chapter (a scene
break) can look like a boundary. Run `--dry-run` and look for rows marked
`low-confidence`; try `--min-gap` (lower to catch shorter transitions, higher to ignore
scene breaks) with `--fresh`. If the credits at the start or end are the culprit, toggle
`--detect-intro` / `--detect-outro`.

**Chapters placed by guess, "ran out of audio", or many lengths far from the word counts.**
The recording may have no pause between chapters: some are joined by music, a spoken "Part 2
of 3", or nothing at all, so silence can't find them. The default `anchored` mode asks you
about each chapter whose length doesn't fit; when there are many, `--align-by transcribe
--fresh` places every chapter by its opening words instead. In the `--dry-run` table, rows noted
`unmatched` are chapters whose opening words were never heard: they are placed by
interpolation, so check them against the audio.

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
error).** The EPUB has more chapters than the audio, or the two are different editions
(if the audio has no pause between chapters, see the entry above).
For a readaloud failure the M4B is already delivered; fix the cause (Node 24+, enough
disk) and re-run to resume at the alignment, or pass `--no-readaloud`.

**"waiting for input" / exit status 3.** The start or end of the book couldn't be
confirmed, or a chapter's length doesn't fit its text, and there was no terminal to ask in.
Run `mp3-epub-m4b resolve AUDIO` in a terminal to listen to the clip and pick; the
conversion then carries on. If the same book keeps asking about the start, its audio may
open with long credits or a preview: pick the section the narrator begins in, or use "None
of these" to hear the next clip.

**"the table of contents has N entries but they point into so few files…".** The EPUB
keeps several chapters in one file (often the whole book in one), and its table of
contents links to places inside that file. Sections are cut at file boundaries, so those
chapters merge into one and the default mode can't find the start and end of the book or
check chapter lengths reliably. Re-export the EPUB with one file per chapter (for example
with Calibre), or check the chapter table with `--dry-run` before converting.

**"no whisper model found" / "whisper.cpp not found".** The default mode transcribes a few
10-second clips. Install whisper.cpp (`brew install whisper-cpp`) and put a model such as
`ggml-base.en.bin` in `~/.cache/whisper-models` (or pass `--whisper-model`), or use
`--align-by silence`.

**"was made for different input/options".** Saved progress belongs to other files or
settings. Re-run with the original settings, or add `--fresh`.

