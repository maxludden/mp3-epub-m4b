# Anchored alignment: verify the start and end of the book before encoding

Status: draft for review · 2026-11-13

## Why

Most books come out with either the **beginning or the end of the book wrong**.
The bad anchor usually only shows up near the end of a 30–40 minute run, and then
the whole book has to be processed again. The current default (`--align-by silence`)
puts chapter 1 at 0 s (or just after a short intro) and chains silence gaps forward.
It never checks that the first gap really is where the EPUB's first narrated section
begins. It also never checks that the last chapter lines up with the end of the audio.

The new default finds both ends of the book from what the narrator actually says,
confirms each one against its neighbouring section, and checks every chapter's length,
**before anything is encoded**. When a check fails, it asks the person instead of
guessing. Wrong anchors then cost a minute and one question instead of a full re-run.

## Goals

1. A new alignment mode, `--align-by anchored`, which becomes the default for
   `convert`, `batch`, and the MCP `start_conversion` tool.
2. The start and end of the book are found and verified by transcript before chapter
   placement finishes.
3. Every chapter is checked against its expected length (±10%). A chapter outside
   that range is low confidence.
4. The M4B is encoded only when no chapter is low confidence. That means everything
   passed, or a person resolved every flag.
5. When a person is needed, they're asked wherever the tool is running: in the
   terminal, or through Claude via the MCP server.

## Non-goals

- Removing the existing modes. `silence`, `transcribe` and `auto` stay available
  through `--align-by`.
- Transcribing the whole book. Only 10-second windows are transcribed.
- Changing encoding, delivery, readaloud, iCloud eviction or the status UI, except
  for the new "waiting for input" state.

## Anti-goals (a failure even if it technically works)

- Starting the encode while any chapter is still low confidence.
- Asking a question only after the encode has started.
- Hanging forever on a prompt in an unattended run (batch or MCP background job).
- Quietly falling back to the old unverified placement when verification fails.

## The pipeline (new `align` stage behaviour)

The existing stages (`extract → scan → align → assemble → deliver → readaloud`) and
the resumable checkpoint stay. The anchored mode replaces what happens inside
`align`, and widens what `extract` records.

### 1. Silence scan → clips
Reuse the existing scan (`stage_scan`, `SilenceIndex`). A **clip** is the stretch of
audio between two consecutive qualifying silences (≥ `min_gap`, default 3.5 s). The
first clip starts at 0 and the last ends at the end of the file. Clips are time ranges
and nothing gets written out for them. Only the windows that get transcribed are cut
to WAV. *(My call.)*

### 2. Transcribe the edges
Transcribe the **first 10 s of the first 5 clips** and the **first 10 s of the last
5 clips** with whisper.cpp (reuse `transcribe.transcribe_window`). Cache the
transcripts in the checkpoint so a resumed run doesn't redo them. If the book has
fewer than 10 clips, the two sets overlap and that's fine.

### 3. Table of contents
Record the EPUB's TOC (titles, order, nesting) in the checkpoint as it is.

### 4. Extract every section's text
Today `extract_epub` drops front/back matter through a blocklist. Anchored mode needs
**every** section: front matter, parts, chapters, sections, back matter. Add a
`sections` list to `EpubResult`. Each section has its title, a kind
(`front`/`body`/`back`, from the existing blocklist heuristics), its word count, its
`opening` (title plus the first ~40 words, since narrators usually read the heading)
and its spine position. Keep `chapters` for the existing modes.

### 5. Find the start of the book
Compare each of the first 5 clip transcripts, in order, with the openings of the
sections from the front of the book (reuse `score_match`). The **start** is the first
clip whose transcript matches a section above the match threshold (e.g. clip 2 ↔
"Chapter 1", or ↔ "Prologue" if the narrator reads it). Sections before the matched
one (title page, copyright, dedication…) are treated as not narrated.

### 6. Verify the start
The next clip's transcript must match the **next narrated section** (Chapter 1 →
Chapter 2). If it does, the start is confirmed.

My call: if the next clip doesn't match but one of the following two clips does,
it still counts as confirmed. Scene breaks inside Chapter 1 can put extra clips
in between. If none of them match, ask (see "Asking the person").

### 7. Find the end of the book
Compare the last clip's transcript with the openings of the final sections **in
reverse order** until one matches. That section is the last narrated section, and
anything after it in the EPUB (acknowledgements, about the author…) is not narrated.
If the last clip matches nothing, try the clips before it, up to the 5 that were
transcribed. Credits often take up the very last clip.

### 8. Verify the end
The clip before the end clip must match the **section before** the end section
(the same 3-clip tolerance as step 6). If not, ask.

### 9. Place the chapters in between
The book's chapters are the narrated sections from the start section to the end
section. Place each boundary with the existing silence chaining (`find_gap` with the
word-count `min_offset`), starting from the confirmed start. The anchor clips found in
steps 5–8 are fixed boundaries.

My call: sections under ~50 words between the anchors, such as a "Part Two" title
page, are merged into the section that follows rather than becoming chapters of a
few seconds.

### 10. Expected lengths and the ±10% check
Speaking rate = (audio from the start anchor to the end of the last section) ÷ (words
in the sections in between). A chapter's expected length = its words × rate. Any
chapter whose placed length is more than **10%** away from that is **low confidence**.

A chapter is also accepted if it is within ±15 s of its expected length, so very short
chapters aren't flagged for a single long pause (agreed 2026-11-13). Flagged means it
fails **both** the 10% and the 15 s tolerance.

### 11. Encode
Only when nothing is low confidence does the stage advance to `assemble`. Encoding,
metadata, cover and chapter markers work exactly as today.

## Asking the person

There are three kinds of question. Each one is stored in the checkpoint as a
`pending_question`, so it survives restarts and can be answered from anywhere.

| Question | Audio played / shown | Choices shown |
|---|---|---|
| **Where does the book start?** | First 10 s of the first clip, with Whisper's transcript | Openings of the first 5 sections (title + first lines) |
| **Where does the book end?** | First 10 s of the last clip, with transcript | Openings of the last 5 sections |
| **Chapter N looks wrong** | 10 s at the suspect boundary, with transcript | The 5 sections whose expected start is closest to that time, plus "this isn't a chapter start" |

The tool always shows its best automatic match as the suggested choice. The person's
pick becomes a fixed boundary. Every later chapter is re-placed and re-checked, which
may produce another question. *(My call: the start/end questions also let the person
step to the next or previous clip if the one played isn't where the book starts.)*

### In the terminal (interactive TTY)
A Rich prompt shows the transcript and the numbered choices, and plays the clip with
`ffplay -nodisp -autoexit` (from the ffmpeg already required), or `afplay` on macOS.
There's a "replay" choice too.

### Through Claude (MCP)
The background job can't prompt (`stdin` is `/dev/null`). It writes the
`pending_question`, records a "waiting for input" state, and exits cleanly. New MCP
tools:

- `pending_question(book)` returns the question, transcript, choices, suggested
  choice, and the clip's file path. Claude can't play audio, so it shows the
  transcript and opens the clip in the system player.
- `answer_question(book, choice)` saves the answer and restarts the job, which
  resumes from the checkpoint.

`conversion_status` reports `waiting_for_input: true` so Claude knows to ask.

### Unattended runs (`batch`, `convert-batch.sh`, non-TTY)
My call: never block. The book is left in the "waiting for input" state with nothing
encoded, the batch moves on to the next book, and the summary lists the books that
need an answer. A new `mp3-epub-m4b resolve BOOK` command asks the stored questions
interactively and then carries on with the conversion. `mp3-epub-m4b status` shows
"waiting for input" for those books.

## Constraints

- whisper.cpp and a GGML model become **required for the default mode** (they were
  optional). My call: if they're missing, fail at startup with an install hint and
  point to `--align-by silence`, rather than quietly falling back.
- The checkpoint stays resumable and fingerprinted. Adding the anchored mode changes
  the fingerprint, so old `silence` checkpoints need `--fresh`, as is already
  enforced by `StateMismatchError`.
- `--dry-run` runs the whole anchored check, including the questions, then prints
  the chapter table. This is the cheap preview.
- `--detect-intro` / `--detect-outro` keep working. In anchored mode the audio
  before the start anchor becomes the intro chapter when `--detect-intro` is set.
  Otherwise chapter 1 starts at 0 s in the M4B. *(My call.)*
- Tests: unit tests for start/end matching, verification, the rate/deviation check
  and question round-tripping, using the existing fake-whisper fixtures in
  `tests/conftest.py`. No real audio is needed in CI.
- Docs: update `README.md`, `docs/convert.md`, `docs/options.md`, `docs/batch.md`
  and `docs/troubleshooting.md`.

## Left to the builder

- The match threshold for start/end (start from `transcribe.MIN_SCORE`) and how a
  title-plus-opening is scored when the narrator skips the heading.
- How `sections` is derived from the TOC versus the spine when they disagree.
- The exact checkpoint keys for transcripts, anchors and `pending_question`.
- Prompt layout and wording.

## The Claude skill (`audiobook-mp3-to-m4b`) — in scope

The skill that drives this tool from Claude is `audiobook-mp3-to-m4b`. Its current
`SKILL.md` is out of date: it describes an older standalone `scripts/run.py` pipeline,
and its `scripts/` folder is empty. It's a claude.ai-synced skill
(`~/.claude/skills/synced/…/audiobook-mp3-to-m4b/`), so local edits would be
overwritten by the next sync.

My call: keep the skill's source in this repo at `skill/audiobook-mp3-to-m4b/SKILL.md`,
so it's versioned with the code it describes. The user then uploads it to claude.ai to
replace the synced copy. The rewritten skill:

- uses the MCP tools (`find_pairs`, `start_conversion`, `conversion_status`,
  `pending_question`, `answer_question`, `verify_m4b`), with the `mp3-epub-m4b` CLI
  as the fallback when the MCP server isn't connected;
- explains the anchored flow in plain terms: start and end are found and confirmed
  first, chapters are length-checked, and nothing is encoded until all checks pass;
- when `conversion_status` reports `waiting_for_input`, calls `pending_question`,
  shows the person the transcript and choices, opens the clip, asks them to pick
  (using `AskUserQuestion` where it's available), and passes the answer to
  `answer_question`;
- keeps the useful parts of today's guidance (verifying the M4B, intro/outro
  caveats, cover art) and drops the out-of-date manual `state.json` patching.
