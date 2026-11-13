"""Tests for anchoring the start and end of the book from edge-clip transcripts."""

from __future__ import annotations

from typing import Literal

from mp3_epub_m4b.anchors import (
    Anchor,
    Clip,
    clips_from_gaps,
    confirm_candidates,
    confirms,
    edge_clips,
    end_probe_clips,
    find_end,
    find_start,
    next_narrated,
    provisional_rate,
    section_score,
)
from mp3_epub_m4b.epub_extract import Section
from mp3_epub_m4b.transcribe import Word

from .conftest import ANCHORED_OPENINGS as OPEN


def speak(text: str, start: float, per_word: float = 0.4) -> list[Word]:
    """Fake word-level transcript of ``text`` beginning at ``start`` seconds."""
    return [
        Word(start + i * per_word, start + (i + 1) * per_word, w)
        for i, w in enumerate(text.split())
    ]


def sec(
    i: int, title: str, words: int, opening: str, kind: Literal["front", "body", "back"] = "body"
) -> Section:
    return {"index": i, "title": title, "kind": kind, "word_count": words, "opening": opening}


FILLER = " word" * 30
SECTIONS = [
    sec(0, "Title Page", 8, "Fish and Chips a test by Ann Author", "front"),
    sec(
        1, "Copyright", 30, "Copyright 2021 Ann Author all rights reserved no part of this", "front"
    ),
    sec(2, "First Light", 300, OPEN[0] + FILLER),
    sec(3, "Tom & Jerry", 600, OPEN[1] + FILLER),
    sec(4, "Fate = Doom; #3", 400, OPEN[2] + FILLER),
    sec(
        5, "About the Author", 60, "Ann Author lives by the sea with two cats and a parrot", "back"
    ),
]


def test_clips_from_gaps() -> None:
    assert clips_from_gaps([32.0, 48.8], 150.0) == [
        Clip(0, 0.0, 32.0),
        Clip(1, 32.0, 48.8),
        Clip(2, 48.8, 150.0),
    ]
    assert clips_from_gaps([], 10.0) == [Clip(0, 0.0, 10.0)]


def test_edge_clips_first_and_last_five_without_duplicates() -> None:
    assert edge_clips(clips_from_gaps([float(t) for t in range(1, 12)], 20.0)) == [
        0, 1, 2, 3, 4, 7, 8, 9, 10, 11,
    ]  # fmt: skip
    assert edge_clips(clips_from_gaps([1.0, 2.0], 3.0)) == [0, 1, 2]


def test_section_score_with_and_without_the_spoken_heading() -> None:
    assert section_score(speak(OPEN[1] + FILLER, 0), SECTIONS[3]) > 0.9
    assert (
        section_score(speak("Chapter two Tom and Jerry " + OPEN[1] + FILLER, 0), SECTIONS[3]) > 0.8
    )
    assert section_score(speak(OPEN[1], 0), SECTIONS[2]) == 0.0


def test_find_start_skips_opening_credits() -> None:
    clips = clips_from_gaps([32.0, 48.8, 105.6], 150.0)
    heard = {
        0: speak("this is audible ann author fish and chips narrated by someone famous", 0),
        1: speak(OPEN[0], 32),
        2: speak("tone tone tone", 48.8),
    }
    a = find_start(heard, clips, SECTIONS)
    assert a is not None and (a.clip, a.section) == (1, 2)


def test_find_start_none_when_nothing_matches() -> None:
    clips = clips_from_gaps([32.0], 50.0)
    assert find_start({0: speak("music", 0), 1: speak("more music", 32)}, clips, SECTIONS) is None


def test_find_end_matches_final_sections_in_reverse() -> None:
    clips = clips_from_gaps([32.0, 48.8, 105.6], 150.0)
    heard = {3: speak(OPEN[2], 105.6), 2: speak("tone", 48.8)}
    a = find_end(heard, clips, SECTIONS, rate=0.1)
    assert a is not None and (a.clip, a.section) == (3, 4)


def test_find_end_uses_expected_position_when_last_clips_are_mid_chapter() -> None:
    # chapter 3 (400 words at 0.1 s/word = 40 s) starts at clip 2 (110 s); clips 3-7 are scene breaks
    clips = clips_from_gaps([32.0, 110.0, 115.0, 120.0, 125.0, 130.0, 135.0], 150.0)
    assert 2 in end_probe_clips(clips, SECTIONS, rate=0.1)
    heard = {i: speak("still in the middle of it", clips[i].start) for i in range(3, 8)}
    heard[2] = speak(OPEN[2], 110.0)
    a = find_end(heard, clips, SECTIONS, rate=0.1)
    assert a is not None and (a.clip, a.section) == (2, 4)


def test_confirm_candidates_adds_the_expected_clip() -> None:
    clips = clips_from_gaps([float(t) for t in range(10, 100, 10)], 100.0)
    assert confirm_candidates(Anchor(1, 2, 0.9), 1, clips, expected_time=71.0) == [2, 3, 4, 7]
    assert confirm_candidates(Anchor(8, 4, 0.9), -1, clips, expected_time=9.0) == [7, 6, 5, 1]
    assert confirm_candidates(Anchor(9, 4, 0.9), 1, clips, expected_time=200.0) == []


def test_confirms_and_next_narrated() -> None:
    assert confirms({3: speak(OPEN[1], 60)}, [2, 3], SECTIONS[3]) == 3
    assert confirms({3: speak(OPEN[1], 60)}, [2], SECTIONS[3]) is None
    part = sec(3, "Part Two", 20, "Part Two the long road")
    shifted = [
        sec(s["index"] + 1, s["title"], s["word_count"], s["opening"], s["kind"])
        for s in SECTIONS[3:]
    ]
    with_part = [*SECTIONS[:3], part, *shifted]
    assert next_narrated(with_part, 2, 1) == 4
    assert next_narrated(with_part, 4, -1) == 2
    assert next_narrated(SECTIONS, 2, -1) is None  # copyright and title page are too short


def test_provisional_rate_counts_body_words_only() -> None:
    assert provisional_rate(SECTIONS, 130.0) == 0.1
    assert provisional_rate(SECTIONS[:2], 130.0) == 0.0
