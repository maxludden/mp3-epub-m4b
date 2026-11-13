"""Tests for the questions asked when anchoring needs a person's decision."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mp3_epub_m4b.anchors import Clip, narrated_chapters
from mp3_epub_m4b.questions import (
    AnchoredProgress,
    NeedsInput,
    Question,
    apply_answer,
    chapter_question,
    end_question,
    start_question,
)

from .test_anchors import SECTIONS

CLIP = Clip(1, 32.0, 48.8)
WAV = Path("/w/questions/start-1.wav")


def ids(q: Question) -> list[str]:
    return [c["id"] for c in q["choices"]]


def test_start_question_offers_the_first_five_matchable_sections_and_next_clip() -> None:
    many = [*SECTIONS[:5], *[{**SECTIONS[4], "index": i, "title": f"C{i}"} for i in range(5, 9)]]
    q = start_question(CLIP, "this is audible", many, suggested=2, clip_path=WAV)  # type: ignore[arg-type]
    assert q["kind"] == "start" and q["clip"] == 1 and q["clip_start"] == 32.0
    assert q["clip_seconds"] == 10.0 and q["clip_path"] == str(WAV)
    assert ids(q) == ["section:2", "section:3", "section:4", "section:5", "section:6", "clip:next"]
    assert q["suggested"] == "section:2"
    assert q["choices"][0]["label"] == "First Light"
    assert q["choices"][0]["text"].startswith("Morning fog rolled")
    assert "Where does the book start?" in q["prompt"]


def test_end_question_offers_the_last_five_and_previous_clip() -> None:
    q = end_question(CLIP, "tone", SECTIONS, suggested=None, clip_path=WAV)
    assert ids(q) == ["section:2", "section:3", "section:4", "section:5", "clip:prev"]
    assert q["suggested"] is None and q["kind"] == "end"


def test_chapter_question_lists_candidates_and_can_reject() -> None:
    chapters = narrated_chapters(SECTIONS, 2, 4)
    q = chapter_question(2, CLIP, "tone", chapters, [2, 3], WAV, actual=120.0, expected=60.0)
    assert ids(q) == ["chapter:2", "chapter:3", "not-a-boundary"]
    assert q["chapter"] == 2 and q["suggested"] is None
    assert "Chapter 2 (Tom & Jerry) is 0:02:00 long" in q["prompt"]
    q2 = chapter_question(
        2, CLIP, "tone", chapters, [2], WAV, actual=1, expected=60, allow_reject=False
    )
    assert ids(q2) == ["chapter:2"]


def test_apply_answer_start_section_confirms() -> None:
    prog: AnchoredProgress = {}
    apply_answer(prog, start_question(CLIP, "", SECTIONS, None, WAV), "section:2")
    assert prog["start"] == {"clip": 1, "section": 2, "confirmed": True}


def test_apply_answer_next_and_previous_clip() -> None:
    prog: AnchoredProgress = {"start": {"clip": 1, "section": 2, "confirmed": False}}
    apply_answer(prog, start_question(CLIP, "", SECTIONS, 2, WAV), "clip:next")
    assert prog["ask_clip"]["start"] == 2 and prog.get("start") is None
    apply_answer(prog, end_question(CLIP, "", SECTIONS, None, WAV), "clip:prev")
    assert prog["ask_clip"]["end"] == 0


def test_apply_answer_chapter_and_reject() -> None:
    chapters = narrated_chapters(SECTIONS, 2, 4)
    q = chapter_question(2, Clip(3, 105.6, 150.0), "", chapters, [2, 3], WAV, actual=1, expected=2)
    prog: AnchoredProgress = {}
    apply_answer(prog, q, "chapter:3")
    assert prog["fixed"] == {"3": 105.6}
    apply_answer(prog, q, "not-a-boundary")
    assert prog["rejected"] == [105.6]


def test_apply_answer_rejects_unknown_choice() -> None:
    with pytest.raises(ValueError, match="section:99"):
        apply_answer({}, start_question(CLIP, "", SECTIONS, None, WAV), "section:99")


def test_question_round_trips_json_and_needs_input_carries_it() -> None:
    q = start_question(CLIP, "heard", SECTIONS, 2, WAV)
    assert json.loads(json.dumps(q)) == q
    assert NeedsInput(q).question is q
    assert "Where does the book start?" in str(NeedsInput(q))
