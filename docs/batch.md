# Convert a whole series


```shell
mp3-epub-m4b batch "/path/to/series folder"
mp3-epub-m4b batch                          # the current directory
```

Files are paired in two ways, numbered pairs first:

1. **By number.** By default an audio file named `Volume N`, `Vol. N`, `VolN` or
   `N - Title` is paired with an EPUB ending in `_N` or containing `N - ` (e.g.
   `Volume 3.mp3` + `my_series_03.epub`). For other names use `--audio-pattern` (fully
   matched against the audio file's name) and `--epub-pattern` (searched in the EPUB's
   name). Each is a regex capturing the number; alternatives may each have their own group:

   ```shell
   mp3-epub-m4b batch ./series --audio-pattern 'Book (\d+)' --epub-pattern '_(\d+)$'
   ```

2. **By name.** Whatever is left is paired when the names match exactly (case-insensitive):
   `Some Book.mp3` (or `.m4a`) + `Some Book.epub`.

The pairs are then listed and you are asked to confirm. Pass `--yes` (`-y`) to skip the
question (needed when running unattended); `--dry-run` lists the pairs and the planned
chapters without asking or encoding.

Unmatched files are reported and skipped, and the batch stops if free disk space
drops below 8 GB. `./convert-batch.sh` is an optional wrapper.

