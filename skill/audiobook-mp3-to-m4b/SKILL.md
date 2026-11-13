---
name: audiobook-mp3-to-m4b
description: Converts a single-track MP3 or M4A audiobook into a properly chaptered M4B file, cross-referencing the book's EPUB for chapter titles and validation. Use this whenever the user has a giant single-file audiobook (commonly ripped/downloaded from Audible, Libro.fm, or similar) and wants it split into named chapters, converted to M4B, or "fixed" because chapter markers are missing, wrong, or landing mid-sentence/mid-word. Also trigger if the user mentions audiobook chapter timestamps, splitting an audiobook by chapter, making an audiobook work properly in players that expect M4B chapter markers (Apple Books, BookPlayer, Plex, Prologue), or complains that chapters in an audiobook start too early/too late/in the wrong place. Do NOT use for audiobooks that already ship as separate per-chapter files, or for non-audiobook audio editing.
---

<!-- Source of truth for the claude.ai skill. To install or update it, upload this file in
claude.ai → Settings → Skills (it replaces the synced copy). Keep it in step with the
mp3-epub-m4b tool in this repo. -->

# Audiobook MP3 -> Chaptered M4B

Use the **mp3-epub-m4b** tool (MCP server `mp3-epub-m4b`, or the `mp3-epub-m4b` CLI). Don't
hand-roll ffmpeg commands or patch checkpoint files: the tool already does the hard parts.

## How it places chapters

Seller chapter timestamps drift and word-count pacing alone is off by minutes, so the tool
works from the audio itself, and checks both ends of the book first:

1. It splits the audio into clips at chapter-length silences.
2. It transcribes the first 10 seconds of the first 5 and last 5 clips.
3. It reads every section of the EPUB: front matter, parts, chapters, back matter.
4. **Start:** it matches the opening clips to the first sections (for example, Chapter 1),
   then confirms that a following clip matches the next section (Chapter 2).
5. **End:** it matches the last clips to the final sections in reverse, and confirms the
   end against the section before.
6. It places chapters at silences between the anchors and checks each one's length against
   its word count. A chapter more than 10% *and* 15 seconds off is flagged.
7. It encodes the M4B, with metadata, cover and chapter markers, only when nothing is
   flagged or the person has settled every flag.

The most common failure, a wrong start or end of the book, therefore shows up in the first
minutes, as a question, instead of after a 30–40 minute encode.

## Running it with the MCP server (preferred)

1. `find_pairs(folder)` shows which audio files pair with which EPUBs. Confirm the pairs
   with the person if there's any doubt.
2. `start_conversion(audio, dry_run=true)` gives a cheap preview: it places chapters
   without encoding. It returns immediately.
3. Poll `conversion_status(book)` every minute or so. Report progress briefly; don't
   narrate every poll.
4. **When `waiting_for_input` is true:**
   - Call `pending_question(book)`.
   - Show the person the `prompt`, what was `heard` (Whisper's transcript of the clip),
     and the numbered `choices`, each with its `label` and `text`. Mark the `suggested`
     choice if there is one.
   - You can't play audio. Open `clip_path` in their audio player (`open "<clip_path>"` on
     macOS, `xdg-open` on Linux) so they can listen to the 10-second clip.
   - Ask them to pick, using `AskUserQuestion` when it's available. Put the choice labels
     in the options and the opening text in their descriptions. If there are more choices
     than fit, ask in plain text with the numbered list.
   - Call `answer_question(book, choice)` with the chosen `id`, then keep polling. More
     questions may follow, because each answer re-checks the chapters after it.
   - Never pick on the person's behalf, even when there is a suggested choice. The
     question exists because the tool couldn't be sure.
5. After a dry run looks right (check the first and last chapters and any
   `suspicious_chapters` with `conversion_status(book, detail=true)`), call
   `start_conversion(audio)` without `dry_run`. It reuses the scan and the answers.
6. When the stage is `done`, call `verify_m4b(final_output)`. Check that the chapter count
   matches the plan, that the duration matches the source, and that there is a cover
   stream.

What the choices mean:
- `section:<i>`: the narrator begins this section in the clip.
- `chapter:<n>`: chapter n starts at this point.
- `clip:next` / `clip:prev`: none of these; listen to the next or previous clip instead.
- `more`: show more sections (the right one isn't listed yet).
- `not-a-boundary`: this is a pause inside a chapter, not a chapter start.
- `accept`: keep the chapter where it is, because its length is just unusual. It stays
  flagged in the chapter table.

## Running it with the CLI (no MCP server)

Run it in a terminal the person can type in. The tool plays the clip and asks there:

```shell
mp3-epub-m4b convert "Book.mp3" "Book.epub" --dry-run   # preview, may ask questions
mp3-epub-m4b convert "Book.mp3" "Book.epub"             # the real run
mp3-epub-m4b batch "/path/to/series" -y                 # never asks; leaves books waiting
mp3-epub-m4b resolve "Book.mp3"                         # answer a waiting book, then carry on
mp3-epub-m4b status                                     # progress, including "waiting for input"
```

Exit status 3 means a book is waiting for input. Without a terminal, for example when you
run the command yourself, the question is saved in the book's checkpoint. Ask the person
to run `resolve` in their own terminal, or use the MCP tools above.

## Requirements

- ffmpeg/ffprobe, whisper.cpp (`brew install whisper-cpp`), and a GGML model such as
  `ggml-base.en.bin` in `~/.cache/whisper-models`. If whisper is missing, the tool stops at
  once with an install hint. `--align-by silence` (`align_by="silence"`) works without
  whisper but skips the start/end checks.
- The book's EPUB. It provides chapter titles, the text that is matched, word counts,
  metadata and the cover. Ask for it before proceeding without one.

## Intro / outro (opening or end credits)

These are off by default. Auto-detecting credits is ambiguous, and a false positive used to
shift every chapter label. In anchored mode, audio before the confirmed start becomes an
"Opening Credits" chapter only with `detect_intro`; otherwise it stays at the front of
chapter 1. Enable `detect_intro` / `detect_outro` only when the person confirms the book
has clear credits, and check the dry run.

## Other modes

- `align_by="transcribe"` finds every chapter's opening words in a transcript. Use it for
  recordings with no pause between chapters, which show up as many length questions in
  anchored mode. It is slower: roughly 15 minutes for a 20-hour book.
- `align_by="silence"` / `"auto"` are the older silence-first placement, with no questions.

## Cover art

The tool takes the full-resolution cover from the EPUB's manifest. It is usually far larger
than an app thumbnail, so prefer it over any image already on disk.
