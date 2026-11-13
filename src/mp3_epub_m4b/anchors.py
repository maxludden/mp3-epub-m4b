"""Anchor the start and end of the book: match short transcripts of the edge clips to EPUB sections.

A *clip* is the audio between two chapter-length silences. The default
(``anchored``) alignment listens to the first ``LISTEN_SECONDS`` of the first
and last few clips, finds which EPUB section each one begins, and confirms the
match against the neighbouring section before any chapter is placed. Everything
here is pure, so it is tested on fake transcripts.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from mp3_epub_m4b.epub_extract import Section
from mp3_epub_m4b.transcribe import MIN_SCORE, Word, find_opening

LISTEN_SECONDS = 10.0  # how much of each clip's start is transcribed
EDGE_CLIPS = 5  # clips listened to at each end of the book
EDGE_SECTIONS = 10  # sections compared at each end of the book
CONFIRM_CLIPS = 3  # clips after/before an anchor that may confirm it
MIN_SECTION_WORDS = 50  # shorter sections (title pages, part pages) are not matched or kept


@dataclass(frozen=True)
class Clip:
    """Audio between two chapter-length silences."""

    index: int
    start: float
    end: float


@dataclass(frozen=True)
class Anchor:
    """A clip matched to the section the narrator begins in it."""

    clip: int
    section: int
    score: float


def clips_from_gaps(gaps: Sequence[float], total_dur: float) -> list[Clip]:
    """Split the audio at the midpoints of chapter-length silences."""
    edges = [0.0, *gaps, total_dur]
    return [Clip(i, edges[i], edges[i + 1]) for i in range(len(edges) - 1)]


def edge_clips(clips: Sequence[Clip]) -> list[int]:
    """Indexes of the first and last ``EDGE_CLIPS`` clips, in order, without duplicates."""
    idx = [c.index for c in clips]
    return sorted(set(idx[:EDGE_CLIPS]) | set(idx[-EDGE_CLIPS:]))


def section_score(heard: Sequence[Word], section: Section) -> float:
    """How well ``heard`` matches the start of ``section`` (0.0 if it does not match).

    Tries the opening alone and with the title in front, since narrators
    usually read the heading first.
    """
    best = 0.0
    for text in (section["opening"], f"{section['title']} {section['opening']}"):
        hit = find_opening(heard, text, MIN_SCORE)
        if hit is not None:
            best = max(best, hit.score)
    return best


def _best(heard: Sequence[Word], sections: Sequence[Section]) -> tuple[int, float] | None:
    scored = [
        (s["index"], section_score(heard, s))
        for s in sections
        if s["word_count"] >= MIN_SECTION_WORDS
    ]
    scored = [x for x in scored if x[1] > 0.0]
    return max(scored, key=lambda x: x[1]) if scored else None


def find_start(
    heard: Mapping[int, Sequence[Word]], clips: Sequence[Clip], sections: Sequence[Section]
) -> Anchor | None:
    """The first of the first ``EDGE_CLIPS`` clips that begins one of the first ``EDGE_SECTIONS`` sections."""
    for clip in clips[:EDGE_CLIPS]:
        if clip.index in heard and (hit := _best(heard[clip.index], sections[:EDGE_SECTIONS])):
            return Anchor(clip.index, hit[0], hit[1])
    return None


def _nearest(clips: Sequence[Clip], t: float) -> int:
    return min(clips, key=lambda c: abs(c.start - t)).index


def end_probe_clips(clips: Sequence[Clip], sections: Sequence[Section], rate: float) -> list[int]:
    """Clips nearest where each of the last ``EDGE_SECTIONS`` sections should start.

    Used when none of the last clips begins a section (a final chapter with many
    scene breaks): the expected start is the end of the audio minus the words
    from that section to the last body section, at ``rate`` seconds per word.
    """
    if not clips or rate <= 0:
        return []
    body = [s["index"] for s in sections if s["kind"] == "body"]
    last_body = body[-1] if body else len(sections) - 1
    total = clips[-1].end
    probes: list[int] = []
    for s in reversed(sections[-EDGE_SECTIONS:]):
        if s["index"] > last_body or s["word_count"] < MIN_SECTION_WORDS:
            continue
        words = sum(x["word_count"] for x in sections[s["index"] : last_body + 1])
        k = _nearest(clips, total - words * rate)
        if k not in probes:
            probes.append(k)
    return probes


def find_end(
    heard: Mapping[int, Sequence[Word]],
    clips: Sequence[Clip],
    sections: Sequence[Section],
    rate: float,
) -> Anchor | None:
    """The last narrated section and the clip it starts in.

    Walks the last ``EDGE_CLIPS`` clips from the end, comparing each with the
    last ``EDGE_SECTIONS`` sections; failing that, tries :func:`end_probe_clips`.
    """
    tail = list(reversed(sections[-EDGE_SECTIONS:]))
    order = [c.index for c in reversed(clips[-EDGE_CLIPS:])]
    order += [k for k in end_probe_clips(clips, sections, rate) if k not in order]
    for k in order:
        if k in heard and (hit := _best(heard[k], tail)):
            return Anchor(k, hit[0], hit[1])
    return None


def confirm_candidates(
    anchor: Anchor, direction: Literal[1, -1], clips: Sequence[Clip], expected_time: float
) -> list[int]:
    """Clips that may begin the neighbouring section: the next ``CONFIRM_CLIPS`` in ``direction``,
    then the clip nearest ``expected_time``."""
    n = len(clips)
    out = [k for s in range(1, CONFIRM_CLIPS + 1) if 0 <= (k := anchor.clip + s * direction) < n]
    near = _nearest(clips, expected_time) if clips else anchor.clip
    if near != anchor.clip and near not in out:
        out.append(near)
    return out


def confirms(
    heard: Mapping[int, Sequence[Word]], candidates: Sequence[int], section: Section
) -> int | None:
    """The first candidate clip whose transcript begins ``section``, or ``None``."""
    for k in candidates:
        if k in heard and section_score(heard[k], section) > 0.0:
            return k
    return None


def next_narrated(sections: Sequence[Section], i: int, direction: Literal[1, -1]) -> int | None:
    """The nearest section after (or before) ``i`` long enough to be matched."""
    j = i + direction
    while 0 <= j < len(sections):
        if sections[j]["word_count"] >= MIN_SECTION_WORDS:
            return j
        j += direction
    return None


def provisional_rate(sections: Sequence[Section], total_dur: float) -> float:
    """Seconds per word over the whole recording, counting body sections only."""
    words = sum(s["word_count"] for s in sections if s["kind"] == "body")
    return total_dur / words if words else 0.0
