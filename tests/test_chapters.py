"""Tests for chapter placement and chapter metadata on hand-made silence scans."""

from __future__ import annotations

import pytest

from mp3_epub_m4b.chapters import SilenceIndex, build_ffmetadata, build_segments, deviating_segments
from mp3_epub_m4b.epub_extract import Chapter, EpubMeta

CHAPTERS: list[Chapter] = [
    {"n": 1, "title": "One", "word_count": 100},
    {"n": 2, "title": "Two", "word_count": 100},
]


def test_find_gap_takes_midpoint_and_detects_companion() -> None:
    idx = SilenceIndex([(100.0, 104.0), (105.0, 107.0)], min_gap=3.5)
    pos, confidence, skipped = idx.find_gap(0.0, 200.0)
    assert (pos, confidence, skipped) == (102.0, "high", 0)


def test_find_gap_without_companion_is_low_confidence() -> None:
    pos, confidence, _ = SilenceIndex([(100.0, 104.0)], 3.5).find_gap(0.0, 200.0)
    assert pos == 102.0 and confidence == "low"


def test_find_gap_skips_gaps_closer_than_min_offset() -> None:
    idx = SilenceIndex([(20.0, 24.0), (100.0, 104.0)], 3.5)
    pos, _conf, skipped = idx.find_gap(0.0, 200.0, min_offset=50.0)
    assert pos == 102.0 and skipped == 1


def test_find_gap_at_end_of_file_reports_eof() -> None:
    assert SilenceIndex([(10.0, 14.0)], 3.5).find_gap(198.0, 200.0) == (None, "eof", 0)


def test_find_gap_clamps_a_silence_running_past_the_end() -> None:
    # The last silence runs to EOF; its midpoint is measured inside the file, never beyond it.
    pos, _conf, _ = SilenceIndex([(190.0, 260.0)], 3.5).find_gap(0.0, 200.0)
    assert pos is not None and pos <= 200.0


def test_find_gap_nothing_found_falls_back() -> None:
    assert SilenceIndex([(50.0, 51.0)], 3.5).find_gap(0.0, 200.0) == (None, "fallback", 0)


def test_build_segments_adds_intro_outro_notes_and_words() -> None:
    segs = build_segments(
        CHAPTERS,
        {"1": 12.0, "2": 100.0},
        total_dur=200.0,
        outro_start=180.0,
        intro_title="Intro",
        outro_title="Outro",
        low_confidence=[2],
    )
    assert [s["title"] for s in segs] == ["Intro", "One", "Two", "Outro"]
    assert [s["n"] for s in segs] == [0, 1, 2, 3]
    assert segs[1]["start"] == 12.0 and segs[1]["end"] == 100.0 and segs[1]["words"] == 100
    assert segs[2]["end"] == 180.0 and segs[2]["note"] == "low-confidence"
    assert "words" not in segs[0] and segs[-1]["end"] == 200.0


def test_build_segments_without_intro_or_outro() -> None:
    segs = build_segments(
        CHAPTERS,
        {"1": 0.0, "2": 90.0},
        total_dur=200.0,
        outro_start=None,
        intro_title="Intro",
        outro_title="Outro",
        fallback=[2],
    )
    assert [s["title"] for s in segs] == ["One", "Two"]
    assert segs[1]["end"] == 200.0 and segs[1]["note"] == "fallback"


def test_deviating_segments_flags_only_outliers() -> None:
    chapters: list[Chapter] = [{"n": i, "title": f"C{i}", "word_count": 100} for i in range(1, 5)]
    segs = build_segments(
        chapters,
        {"1": 0.0, "2": 100.0, "3": 200.0, "4": 300.0},
        total_dur=500.0,
        outro_start=None,
        intro_title="i",
        outro_title="o",
    )
    assert [n for n, *_ in deviating_segments(segs)] == [
        3
    ]  # (n=3, 4th chapter) 200 s vs ~125 s expected


def test_ffmetadata_golden_output_escapes_newlines_and_equals() -> None:
    meta: EpubMeta = {
        "title": "A=B\nC",
        "author": "X;Y",
        "publisher": "",
        "year": "2020",
        "series": "S#1",
        "series_index": "2",
    }
    segs = build_segments(
        [{"n": 1, "title": "Fate = Doom;\n#3\\", "word_count": 5}],
        {"1": 0.0},
        total_dur=1.5,
        outro_start=None,
        intro_title="i",
        outro_title="o",
    )
    assert build_ffmetadata(meta, segs) == (
        ";FFMETADATA1\n"
        "title=A\\=B\\\nC\n"
        "artist=X\\;Y\n"
        "album=A\\=B\\\nC\n"
        "album_artist=X\\;Y\n"
        "genre=Audiobook\n"
        "date=2020\n"
        "show=S\\#1\n"
        "episode_id=2\n"
        "[CHAPTER]\n"
        "TIMEBASE=1/1000\n"
        "START=0\n"
        "END=1500\n"
        "title=Fate \\= Doom\\;\\\n\\#3\\\\\n"
    )


@pytest.mark.parametrize("seconds", [0.0, 59.6, 3600.0])
def test_segment_times_are_rounded_to_milliseconds(seconds: float) -> None:
    seg = build_segments(
        CHAPTERS[:1],
        {"1": 0.0},
        total_dur=seconds + 0.12345,
        outro_start=None,
        intro_title="i",
        outro_title="o",
    )[0]
    assert seg["end"] == round(seconds + 0.12345, 3)


def test_find_gap_and_build_segments_log_their_decisions() -> None:
    from loguru import logger

    messages: list[str] = []
    logger.enable("mp3_epub_m4b")
    sink = logger.add(lambda m: messages.append(m.record["message"]), level="DEBUG")
    try:
        idx = SilenceIndex([(100.0, 104.0)], min_gap=3.5)
        idx.find_gap(0.0, 200.0)
        idx.find_gap(150.0, 200.0)
        build_segments(
            CHAPTERS,
            {"1": 10.0, "2": 100.0},
            total_dur=200.0,
            outro_start=None,
            intro_title="Intro",
            outro_title="Outro",
        )
    finally:
        logger.remove(sink)
        logger.disable("mp3_epub_m4b")
    assert any("gap at 102.0s" in m for m in messages)
    assert any("no gap found after 150.0s" in m for m in messages)
    assert any("built 3 segments" in m for m in messages)
