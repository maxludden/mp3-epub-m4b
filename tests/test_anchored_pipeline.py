"""Anchored alignment through the Pipeline, with a fake scan and a fake whisper (no ffmpeg needed)."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from mp3_epub_m4b import run as run_mod
from mp3_epub_m4b.questions import NeedsInput
from mp3_epub_m4b.run import Pipeline
from mp3_epub_m4b.transcribe import Word

from .conftest import ANCHORED_OPENINGS, expected_boundaries, make_epub_with_matter

TOTAL = 147.6
# the synthetic book's layout: chapter gaps, companions after the stings, a scene break in chapter 2
SILENCES = [(30.0, 34.0), (35.0, 37.0), (47.0, 50.6), (100.6, 104.6), (105.6, 107.6)]
TONE = "tone hum tone hum tone hum"


def speak(text: str, start: float, per_word: float = 0.4) -> list[Word]:
    return [
        Word(start + i * per_word, start + (i + 1) * per_word, w)
        for i, w in enumerate(text.split())
    ]


def narrator(starts: list[float]) -> Callable[[float], str]:
    """What is heard at ``t``: a chapter's opening near its start, a tone elsewhere."""

    def heard(t: float) -> str:
        for i, b in enumerate(starts):
            if abs(t - b) < 3.0:
                return ANCHORED_OPENINGS[i] + " word" * 12
        return TONE

    return heard


Book = tuple[Path, Path]


@pytest.fixture
def book(tmp_path: Path) -> Book:
    mp3, epub = tmp_path / "Book.mp3", tmp_path / "Book.epub"
    mp3.write_bytes(b"x")
    make_epub_with_matter(epub)
    return mp3, epub


@pytest.fixture
def calls() -> list[float]:
    return []


def patch_audio(
    monkeypatch: pytest.MonkeyPatch,
    calls: list[float],
    heard: Callable[[float], str],
    silences: list[tuple[float, float]] = SILENCES,
) -> None:
    monkeypatch.setattr(run_mod, "ffprobe_duration", lambda _p: TOTAL)
    monkeypatch.setattr(run_mod, "scan_silences", lambda *a, **k: [list(s) for s in silences])
    monkeypatch.setattr(run_mod, "find_whisper", lambda _m=None: ("whisper", Path("model")))

    def fake_transcribe(_audio: Path, start: float, _dur: float, **_kw: object) -> list[Word]:
        calls.append(start)
        return speak(heard(start), start)

    monkeypatch.setattr(run_mod, "transcribe_window", fake_transcribe)
    monkeypatch.setattr(
        Pipeline,
        "_cut_clip",
        lambda self, start, path: (
            path.parent.mkdir(parents=True, exist_ok=True),
            path.write_bytes(b"RIFF"),
        ),
    )


def make(book: Book, tmp_path: Path) -> Pipeline:
    mp3, epub = book
    return Pipeline(
        mp3, epub, tmp_path / "out", tmp_path / "work", 3.5, "Opening Credits", "End Credits",
        1800.0, "64k", align_by="anchored",
    )  # fmt: skip


def run_until_assemble(p: Pipeline, budget: float = 5.0) -> None:
    for _ in range(100):
        if p.state["stage"] in ("assemble", "deliver", "done"):
            return
        p.run(budget)
    raise AssertionError("pipeline did not reach assemble")


def answer_all(p: Pipeline, answers: list[str]) -> list[str]:
    """Run to assemble, answering each question in turn; returns the question kinds asked."""
    asked: list[str] = []
    for _ in range(len(answers) + 1):
        try:
            run_until_assemble(p)
            return asked
        except NeedsInput as e:
            asked.append(e.question["kind"])
            p.answer(answers[len(asked) - 1])
    raise AssertionError(f"still asking after {answers}: {asked}")


def test_anchored_places_three_chapters_without_asking(book, tmp_path, monkeypatch, calls) -> None:
    patch_audio(monkeypatch, calls, narrator(expected_boundaries()))
    p = make(book, tmp_path)
    run_until_assemble(p)
    assert [c["title"] for c in p.state["epub"]["chapters"]] == [
        "First Light",
        "Tom & Jerry",
        "Fate = Doom; #3",
    ]
    b = p.state["align"]["boundaries"]
    assert [b[k] for k in "123"] == pytest.approx(expected_boundaries())
    assert "pending_question" not in p.state
    assert p.state["align"]["method"] == "anchored"
    assert [s["title"] for s in p.state["segments"]] == [
        "First Light",
        "Tom & Jerry",
        "Fate = Doom; #3",
    ]


def test_anchored_asks_for_the_start_when_nothing_matches(
    book, tmp_path, monkeypatch, calls
) -> None:
    patch_audio(monkeypatch, calls, lambda _t: TONE)
    p = make(book, tmp_path)
    with pytest.raises(NeedsInput) as e:
        run_until_assemble(p)
    q = e.value.question
    assert q["kind"] == "start" and q["clip"] == 0 and q["heard"] == TONE
    assert [c["id"] for c in q["choices"]] == [
        "section:2",
        "section:3",
        "section:4",
        "section:5",
        "clip:next",
    ]
    assert Path(q["clip_path"]).is_file()
    assert p.state["stage"] == "align" and p.state["pending_question"] == q
    with pytest.raises(NeedsInput):  # never advances while the question is pending
        p.run(5.0)


def test_answers_resume_to_assemble(book, tmp_path, monkeypatch, calls) -> None:
    patch_audio(monkeypatch, calls, lambda _t: TONE)
    p = make(book, tmp_path)
    assert answer_all(p, ["section:2", "section:4"]) == ["start", "end"]
    assert p.state["align"]["anchored"]["start"] == {"clip": 0, "section": 2, "confirmed": True}
    assert p.state["align"]["anchored"]["end"] == {"clip": 3, "section": 4, "confirmed": True}
    assert p.state["stage"] == "assemble"


def test_next_clip_answer_asks_about_the_following_clip(book, tmp_path, monkeypatch, calls) -> None:
    patch_audio(monkeypatch, calls, lambda _t: TONE)
    p = make(book, tmp_path)
    with pytest.raises(NeedsInput):
        run_until_assemble(p)
    p.answer("clip:next")
    with pytest.raises(NeedsInput) as e:
        run_until_assemble(p)
    assert e.value.question["kind"] == "start" and e.value.question["clip"] == 1


# chapter 2's real gap is missing; the only pause before chapter 3 is a scene break at 61.8 s
SPARSE = [(60.0, 63.6), (100.6, 104.6), (105.6, 107.6)]


def test_length_outlier_asks_and_rejecting_the_pause_settles_it(
    book, tmp_path, monkeypatch, calls
) -> None:
    patch_audio(monkeypatch, calls, narrator(expected_boundaries()), SPARSE)
    p = make(book, tmp_path)
    asked = answer_all(p, ["section:2", "section:4", "not-a-boundary"])
    assert asked[-1] == "chapter"
    assert p.state["align"]["anchored"]["rejected"] == [61.8]
    assert p.state["align"]["fallback"] == [2]  # placed by word count between the anchors


def test_chapter_question_offers_only_unanchored_chapters(
    book, tmp_path, monkeypatch, calls
) -> None:
    patch_audio(monkeypatch, calls, narrator(expected_boundaries()), SPARSE)
    p = make(book, tmp_path)
    answer_all(p, ["section:2", "section:4", "chapter:2"])
    # Review Focus 3: the person insists chapter 2 starts at the pause; that settles it, no loop
    assert p.state["align"]["anchored"]["fixed"] == {"2": 61.8}
    assert p.state["stage"] == "assemble"


def test_chapter_question_choices(book, tmp_path, monkeypatch, calls) -> None:
    patch_audio(monkeypatch, calls, narrator(expected_boundaries()), SPARSE)
    p = make(book, tmp_path)
    for choice in ("section:2", "section:4"):
        with pytest.raises(NeedsInput):
            run_until_assemble(p)
        p.answer(choice)
    with pytest.raises(NeedsInput) as e:
        run_until_assemble(p)
    q = e.value.question
    assert q["kind"] == "chapter" and q["chapter"] == 1 and q["clip_start"] == 61.8
    assert [c["id"] for c in q["choices"]] == ["chapter:2", "not-a-boundary"]


def test_resume_reuses_cached_transcripts(book, tmp_path, monkeypatch, calls) -> None:
    patch_audio(monkeypatch, calls, narrator(expected_boundaries()))
    p = make(book, tmp_path)
    for _ in range(3):
        p.run(0.0)  # extract, scan, then one transcription per call
    first = len(calls)
    assert 0 < first < 4
    p2 = make(book, tmp_path)  # a new process resuming from state.json
    run_until_assemble(p2)
    assert len(calls) == len(set(calls))  # nothing transcribed twice
