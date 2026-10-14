"""Chapter placement: find boundaries in a silence scan and build output segments.

Everything here is pure (no I/O, no ffmpeg), so it can be tested on fake scans.
See :mod:`mp3_epub_m4b.run` for why boundaries are chained forward from the
previous detected boundary rather than predicted.
"""

from __future__ import annotations

import bisect
from collections.abc import Sequence

from loguru import logger

from mp3_epub_m4b.epub_extract import Chapter, EpubMeta
from mp3_epub_m4b.state import Segment

COMPANION_MIN_SILENCE = 1.5  # length (s) of the short gap that follows a chapter gap
COMPANION_WINDOW = 10.0  # how soon (s) after a chapter gap the companion appears
MIN_EARLY_FRACTION = 0.4  # a gap earlier than this share of the word-count estimate is rejected
FALLBACK_SPAN = 900.0  # where (s after the previous chapter) to place a chapter with no gap found
MAX_DEVIATION = 0.30  # relative duration deviation from the word-count estimate that is flagged


class SilenceIndex:
    """Searchable view of a silence scan.

    Attributes:
        silences: ``(start, end)`` pairs in absolute seconds, chronological.
        min_gap: Minimum silence (s) counted as a chapter transition.
    """

    def __init__(self, silences: Sequence[tuple[float, float]], min_gap: float) -> None:
        self.silences = list(silences)
        self.min_gap = min_gap
        self._ends = [e for _s, e in self.silences]

    def find_gap(
        self,
        after: float,
        total_dur: float,
        max_span: float = 4200.0,
        min_offset: float = 0.0,
    ) -> tuple[float | None, str, int]:
        """Find the first chapter-transition silence after ``after``.

        Looks at silences starting 3 s after ``after`` and within ``max_span``
        seconds of that. The midpoint of the first silence of at least
        ``min_gap`` seconds (measured inside that range) whose midpoint is at
        least ``min_offset`` after ``after`` is returned. Confidence is
        ``"high"`` if a companion gap follows within 10 s, else ``"low"``.

        Args:
            after: Position (s) of the previous boundary to search from.
            total_dur: Total audio duration (s).
            max_span: Longest stretch (s) to search.
            min_offset: Skip gaps whose midpoint is closer than this to ``after``
                (used to reject pauses inside a chapter).

        Returns:
            ``(position, confidence, skipped)``: confidence is ``"high"`` or
            ``"low"``; ``skipped`` counts qualifying gaps rejected as too early.
            ``(None, "eof", 0)`` if the search started at end of file, or
            ``(None, "fallback", skipped)`` if no gap was found.
        """
        clip_start = after + 3.0
        if clip_start >= total_dur:
            logger.debug(f"no gap after {after:.1f}s: reached the end of the audio")
            return None, "eof", 0
        clip_end = min(clip_start + max_span, total_dur)
        skipped = 0
        k = bisect.bisect_right(self._ends, clip_start)
        while k < len(self.silences):
            s, e = self.silences[k]
            if s >= clip_end:
                break
            s, e_clamped = max(s, clip_start), min(e, clip_end)
            if e_clamped - s >= self.min_gap:
                mid = (s + e_clamped) / 2
                if mid - after < min_offset:
                    skipped += 1
                    logger.trace(f"gap at {mid:.1f}s is too close to {after:.1f}s; skipping")
                else:
                    confidence = "high" if self.has_companion(k, e_clamped) else "low"
                    logger.debug(
                        f"gap at {mid:.1f}s after {after:.1f}s ({confidence} confidence, "
                        f"{skipped} skipped)"
                    )
                    return mid, confidence, skipped
            k += 1
        logger.debug(f"no gap found after {after:.1f}s ({skipped} too early)")
        return None, "fallback", skipped

    def has_companion(self, k: int, gap_end: float) -> bool:
        """Whether a short companion silence follows silence ``k`` within 10 s of ``gap_end``."""
        window_end = gap_end + COMPANION_WINDOW
        for s2, e2 in self.silences[k + 1 :]:
            if s2 >= window_end:
                break
            if min(e2, window_end) - max(s2, gap_end) >= COMPANION_MIN_SILENCE:
                return True
        return False


def make_segment(
    n: int, title: str, start: float, end: float, *, words: int | None = None, note: str = ""
) -> Segment:
    """Build a :class:`Segment`, rounding times to milliseconds and computing its duration."""
    start, end = round(start, 3), round(end, 3)
    seg: Segment = {
        "n": n,
        "title": title,
        "start": start,
        "end": end,
        "duration": round(end - start, 3),
    }
    if words is not None:
        seg["words"] = words
    if note:
        seg["note"] = note
    return seg


def build_segments(
    chapters: Sequence[Chapter],
    boundaries: dict[str, float],
    *,
    total_dur: float,
    outro_start: float | None,
    intro_title: str,
    outro_title: str,
    low_confidence: Sequence[int] = (),
    fallback: Sequence[int] = (),
) -> list[Segment]:
    """Turn detected chapter starts into contiguous output segments.

    Adds an intro segment when chapter 1 starts more than 5 s in, and an
    outro segment when ``outro_start`` leaves more than 2 s at the end.

    Args:
        chapters: EPUB chapters in order.
        boundaries: Chapter number (as ``str``) -> start in seconds.
        total_dur: Audio duration (s).
        outro_start: Start of the end credits, or ``None`` for no outro.
        intro_title: Title for the intro segment.
        outro_title: Title for the outro segment.
        low_confidence: Chapters whose boundary lacked a companion gap.
        fallback: Chapters placed by a fixed offset because no gap was found.

    Returns:
        The segments, numbered from 0, with word counts and boundary notes
        attached to chapter segments.
    """
    segments: list[Segment] = []
    if boundaries["1"] > 5.0:
        segments.append(make_segment(0, intro_title, 0.0, boundaries["1"]))
    for i, c in enumerate(chapters, start=1):
        end = boundaries[str(i + 1)] if i < len(chapters) else (outro_start or total_dur)
        note = "fallback" if i in fallback else "low-confidence" if i in low_confidence else ""
        segments.append(
            make_segment(
                len(segments), c["title"], boundaries[str(i)], end, words=c["word_count"], note=note
            )
        )
    if outro_start and (total_dur - outro_start) > 2.0:
        segments.append(make_segment(len(segments), outro_title, outro_start, total_dur))
    logger.debug(
        f"built {len(segments)} segments from {len(chapters)} chapters "
        f"({len(low_confidence)} low-confidence, {len(fallback)} fallback)"
    )
    return segments


def deviating_segments(segments: Sequence[Segment]) -> list[tuple[int, str, float, float]]:
    """Chapters whose length strays from what their word count predicts.

    The expected duration comes from the book-wide speaking rate (seconds per
    word over all segments that have a word count).

    Returns:
        ``(n, title, actual_seconds, expected_seconds)`` for each segment that
        deviates by more than 30%.
    """
    counted = [s for s in segments if s.get("words")]
    total_words = sum(s["words"] for s in counted if "words" in s)
    rate = sum(s["duration"] for s in counted) / total_words if total_words else 0.0
    flagged = []
    for s in counted:
        exp = s.get("words", 0) * rate
        if exp > 0 and abs(s["duration"] - exp) / exp > MAX_DEVIATION:
            flagged.append((s["n"], s["title"], round(s["duration"], 1), round(exp, 1)))
    return flagged


def ffmeta_escape(s: str) -> str:
    """Escape a value for an ``FFMETADATA1`` file (``\\``, ``=``, ``;``, ``#`` and newlines)."""
    s = s.replace("\r", "").replace("\\", "\\\\")
    for char in "=;#":
        s = s.replace(char, "\\" + char)
    return s.replace("\n", "\\\n")


def build_ffmetadata(meta: EpubMeta, segments: Sequence[Segment]) -> str:
    """Build the ``FFMETADATA1`` text: book tags plus one ``[CHAPTER]`` per segment.

    Args:
        meta: Book metadata from the EPUB.
        segments: Contiguous segments in order; times are seconds from the start of the audio.

    Returns:
        The file contents (newline-terminated).
    """
    lines = [
        ";FFMETADATA1",
        f"title={ffmeta_escape(meta['title'])}",
        f"artist={ffmeta_escape(meta['author'])}",
        f"album={ffmeta_escape(meta['title'])}",
        f"album_artist={ffmeta_escape(meta['author'])}",
        "genre=Audiobook",
    ]
    if meta.get("year"):
        lines.append(f"date={ffmeta_escape(meta['year'])}")
    if meta.get("series"):
        lines.append(f"show={ffmeta_escape(meta['series'])}")
        lines.append(f"episode_id={ffmeta_escape(meta['series_index'])}")
    logger.debug(f"writing ffmetadata for {meta['title']!r}: {len(segments)} chapters")
    for s in segments:
        lines += [
            "[CHAPTER]",
            "TIMEBASE=1/1000",
            f"START={round(s['start'] * 1000)}",
            f"END={round(s['end'] * 1000)}",
            f"title={ffmeta_escape(s['title'])}",
        ]
    return "\n".join(lines) + "\n"
