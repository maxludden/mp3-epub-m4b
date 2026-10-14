"""Tests for the chapter table and output verification summary."""

from __future__ import annotations

import io

from rich.console import Console

from mp3_epub_m4b.report import Verification, chapter_table, fmt_time
from mp3_epub_m4b.state import Segment


def test_fmt_time() -> None:
    assert fmt_time(0) == "0:00:00"
    assert fmt_time(3725.4) == "1:02:05"


def _render(table) -> str:
    console = Console(width=120, record=True, file=io.StringIO())
    console.print(table)
    return console.export_text()


def test_chapter_table_lists_titles_times_words_and_notes() -> None:
    segs: list[Segment] = [
        {"n": 0, "title": "Opening Credits", "start": 0.0, "end": 12.0, "duration": 12.0},
        {
            "n": 1,
            "title": "Chapter One",
            "start": 12.0,
            "end": 3612.0,
            "duration": 3600.0,
            "words": 9000,
            "note": "low-confidence",
        },
    ]
    text = _render(chapter_table(segs))
    for expected in (
        "Opening Credits",
        "Chapter One",
        "1:00:12",
        "1:00:00",
        "9000",
        "low-confidence",
    ):
        assert expected in text
    assert "2 chapters" in text


def test_verification_reports_problems() -> None:
    ok = Verification(
        chapters=3, duration=100.0, source_duration=101.0, streams=3, expected_chapters=3
    )
    assert ok.problems == []
    assert "chapters: 3" in ok.summary()
    bad = Verification(
        chapters=2, duration=100.0, source_duration=200.0, streams=2, expected_chapters=3
    )
    assert len(bad.problems) == 2
    assert Verification(0, 1.0, 1.0, 1).problems == ["the M4B has no chapters"]
