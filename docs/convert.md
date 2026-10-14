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

