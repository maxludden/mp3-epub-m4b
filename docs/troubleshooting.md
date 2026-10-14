# Troubleshooting


**A chapter starts mid-word or mid-sentence.** Narrators often put a short "companion"
pause a few seconds after the real chapter gap, and a long pause inside a chapter (a scene
break) can look like a boundary. Run `--dry-run` and look for rows marked
`low-confidence`; try `--min-gap` (lower to catch shorter transitions, higher to ignore
scene breaks) with `--fresh`. If the credits at the start or end are the culprit, toggle
`--detect-intro` / `--detect-outro`.

**Chapters placed by guess, "ran out of audio", or many lengths far from the word counts.**
The recording may have no pause between chapters: some are joined by music, a spoken "Part 2
of 3", or nothing at all, so silence can't find them. With whisper.cpp installed,
`--align-by auto` (the default) notices and switches to transcript placement by itself; you
can also force it with `--align-by transcribe --fresh`. In the `--dry-run` table, rows noted
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

**"was made for different input/options".** Saved progress belongs to other files or
settings. Re-run with the original settings, or add `--fresh`.

