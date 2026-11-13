"""Tests for anchoring the start and end of the book from edge-clip transcripts."""

from __future__ import annotations

from typing import Literal

from mp3_epub_m4b.anchors import (
    Anchor,
    Clip,
    clips_from_gaps,
    closest_chapters,
    confirm_candidates,
    confirms,
    edge_clips,
    end_probe_clips,
    find_end,
    find_start,
    length_outliers,
    narrated_chapters,
    next_narrated,
    place_chapters,
    provisional_rate,
    section_score,
    speaking_rate,
)
from mp3_epub_m4b.chapters import SilenceIndex
from mp3_epub_m4b.epub_extract import Section
from mp3_epub_m4b.state import Segment
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


# --------------------------------------------------------------- placement


def seg(n: int, dur: float, words: int) -> Segment:
    return {"n": n, "title": f"c{n}", "start": 0.0, "end": dur, "duration": dur, "words": words}


def test_length_outliers_need_both_tolerances() -> None:
    # rate 0.1 s/word: expected 100 s, 100 s, 1000 s, 1000 s
    intro: Segment = {"n": 0, "title": "Opening Credits", "start": 0.0, "end": 9.0, "duration": 9.0}
    segs = [
        intro,
        seg(1, dur=100, words=1000),  # exact
        seg(2, dur=112, words=1000),  # 12% off but only 12 s -> within the slack
        seg(3, dur=1080, words=10000),  # 80 s off but only 8% -> within 10%
        seg(4, dur=1300, words=10000),  # 30% and 300 s -> flagged
    ]
    assert length_outliers(segs, rate=0.1) == [4]  # chapter numbers, the intro is not counted


def test_narrated_chapters_merges_part_pages_and_drops_unnarrated() -> None:
    part = sec(3, "Part Two", 20, "Part Two the long road")
    shifted = [
        sec(s["index"] + 1, s["title"], s["word_count"], s["opening"], s["kind"])
        for s in SECTIONS[3:]
    ]
    chapters = narrated_chapters([*SECTIONS[:3], part, *shifted], 2, 5)
    assert [(c["n"], c["title"], c["word_count"]) for c in chapters] == [
        (1, "First Light", 300),
        (2, "Tom & Jerry", 620),
        (3, "Fate = Doom; #3", 400),
    ]
    assert chapters[1]["opening"].startswith(OPEN[1])


def test_speaking_rate() -> None:
    chapters = narrated_chapters(SECTIONS, 2, 4)
    assert speaking_rate(chapters, 10.0, 140.0) == 0.1
    assert speaking_rate([], 0.0, 10.0) == 0.0


CHAPTERS3 = narrated_chapters(SECTIONS, 2, 4)  # 300, 600, 400 words


def test_place_chapters_chains_gaps_between_fixed_ends() -> None:
    silences = SilenceIndex([(30.0, 34.0), (47.0, 50.6), (103.6, 107.6)], 3.5)
    p = place_chapters(
        CHAPTERS3, silences, fixed={"1": 0.0, "3": 105.6}, rejected=[], total_dur=150.0, rate=0.11
    )
    assert p.boundaries == {"1": 0.0, "2": 32.0, "3": 105.6}
    assert p.fallback == [] and p.low_confidence == [2]


def test_place_chapters_skips_rejected_gaps() -> None:
    # the person said 32 s is not a chapter start, so chapter 2 moves to the next gap
    silences = SilenceIndex([(30.0, 34.0), (47.0, 50.6), (103.6, 107.6)], 3.5)
    p = place_chapters(
        CHAPTERS3,
        silences,
        fixed={"1": 0.0, "3": 105.6},
        rejected=[32.0],
        total_dur=150.0,
        rate=0.11,
    )
    assert p.boundaries["2"] == 48.8


def test_place_chapters_interpolates_when_no_gap_before_the_next_fixed_start() -> None:
    silences = SilenceIndex([(120.0, 124.0)], 3.5)
    p = place_chapters(
        CHAPTERS3, silences, fixed={"1": 0.0, "3": 90.0}, rejected=[], total_dur=150.0, rate=0.1
    )
    assert p.fallback == [2]
    assert p.boundaries["2"] == 30.0  # 90 s split 300:600 by the words of chapters 1 and 2


def test_closest_chapters_by_expected_start() -> None:
    chapters = narrated_chapters(SECTIONS, 2, 4)  # expected starts 0, 30, 90 at 0.1 s/word
    assert closest_chapters(85.0, chapters, 0.0, 0.1, k=2) == [2, 3]
    assert closest_chapters(0.0, chapters, 0.0, 0.1) == [1, 2, 3]


# ----------------------------------------------------------- review fixes


def test_section_score_matches_a_short_heard_window() -> None:
    # a 10 s window that starts in the silence and spends 3 s on the heading hears ~8 words
    heard = speak(" ".join(OPEN[1].split()[:8]), 0)
    assert section_score(heard, SECTIONS[3]) > 0.0


def test_find_start_looks_past_long_front_matter() -> None:
    stubs = [
        sec(i, f"Front {i}", 20, f"front matter page {i} words here", "front") for i in range(12)
    ]
    first = sec(12, "First Light", 300, OPEN[0] + FILLER)
    clips = clips_from_gaps([32.0], 50.0)
    a = find_start({0: speak(OPEN[0] + FILLER, 0)}, clips, [*stubs, first])
    assert a is not None and a.section == 12
