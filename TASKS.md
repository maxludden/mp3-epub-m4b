# Review fixes — progress

Legend: `[x]` done · `[~]` in progress · `[ ]` todo

## Bugs
- [x] 1. Boundary fallback overshoots end of file (clamp + fail loudly)
- [x] 2. ffmpeg errors swallowed (`detect_silences`) / stderr hidden (`encode_one`)
- [x] 3. Scene-break false boundaries (reject candidates far earlier than word-count estimate)
- [x] 4. `find_boundary` window clamp mismatch
- [x] 5. EPUB parsing: lxml instead of regex, URL-decoded hrefs, entities, EPUB 3 nav/cover/series, path-based TOC matching
- [x] 6. ffmpeg metadata escaping (title/author/series, newlines)
- [x] 7. `deliver` overwrite/corruption (write `.part`, atomic rename, refuse overwrite unless asked)
- [x] 8. Zero chapters rejected; chapter-count plausibility warning; word-count check by index not title

## Design / quality
- [x] 9. Stale resume state (fingerprint inputs/options, `--fresh`)
- [x] 10. `print()` in pipeline fights progress bars (log callback)
- [x] 11. `/tmp` staging → work dir, cleanup, disk check on the right volume
- [x] 12. Sandbox leftovers removed (docstrings, argparse `run.py` main)
- [x] 13. Gapless single-pass encode with chapter markers (replaces per-segment encode + concat)
- [x] 14. One-pass silence scan cached to disk (replaces per-chapter re-decoding), `-vn`
- [x] 15. Naming scheme configurable (`--audio-pattern`, `--epub-pattern`), case-insensitive extensions
- [x] 16. Front/back-matter blocklist false positives
- [x] 17. Legacy entry points marked; `align_chapters` saves state after every boundary
- [x] 18. Scoped warning filter (no global side effect)

## Housekeeping
- [x] 19. Tests (pytest)
- [x] 20. `.gitignore`, drop `hello()`, ruff/mypy config in pyproject
- [x] 21. Stale shell scripts / README (`convert.sh` points at missing `pipeline/run.py`)
- [x] 22. Final verification: mypy, ruff, pytest, real-EPUB run, synthetic end-to-end audio run
