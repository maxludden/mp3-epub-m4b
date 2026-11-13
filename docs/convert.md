# Convert one book


```shell
mp3-epub-m4b convert "Volume 1.mp3" book_01.epub
mp3-epub-m4b convert "Some Book.mp3"      # uses "Some Book.epub" beside it
```

The EPUB argument is optional: without it, the `.epub` with the same name as the audio
file (in the same folder) is used.

The M4B lands in an `m4b/` folder next to the audio (or pass `-o DIR`). Expect roughly
30–40 minutes for a ~24 h book. The optional `./convert.sh` wrapper does the same and
turns on `--detect-intro --detect-outro`.

## How chapters are found

By default (`--align-by anchored`) the start and end of the book are pinned down before
any chapter is placed, and nothing is encoded until every chapter checks out:

1. The audio is split into clips at chapter-length silences.
2. The first 10 seconds of the first 5 and last 5 clips are transcribed with whisper.cpp.
3. Every part of the EPUB is read in order — front matter, parts, chapters, back matter.
4. **Start:** the opening clips are matched against the first sections until one lines up
   (say, Chapter 1). It is confirmed only if a following clip matches the *next* section
   (Chapter 2). Sections before it (title page, copyright…) count as not narrated.
5. **End:** the last clips are matched against the final sections in reverse order and
   confirmed against the section before. Back matter after it counts as not narrated.
6. Chapters in between are placed at silences, and each one's length is checked against
   its word count at the book's speaking rate. One more than 10% *and* 15 seconds off is
   flagged.

When a check fails, you're asked. You hear the 10-second clip, see what whisper heard and
the opening lines of the candidate sections, and pick where the narrator is. A flagged
chapter gets the same treatment at its suspect boundary, with a "no chapter starts here"
option for a pause inside a chapter. Your answer fixes that boundary and everything after
it is re-checked.

Without a terminal (a batch run, Claude, a pipe) the question is saved instead, nothing is
encoded, and `convert` exits with status 3. Answer it later, and the conversion carries on
with its original settings:

```shell
mp3-epub-m4b resolve "Volume 1.mp3"
```

**Preview the chapters first.** `--dry-run` scans the audio, places the chapters and
prints the table without encoding anything, so a bad boundary shows up in minutes
instead of after the encode. A real run afterwards reuses the saved scan:

```text
$ mp3-epub-m4b convert "Volume 1.mp3" book_01.epub --dry-run
planning chapters for Volume 1.mp3 with book_01.epub (no encoding)
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

