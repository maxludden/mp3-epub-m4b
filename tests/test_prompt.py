"""Tests for asking a waiting question in the terminal."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from mp3_epub_m4b.anchors import Clip
from mp3_epub_m4b.prompt import ask
from mp3_epub_m4b.questions import Question, start_question

from .test_anchors import SECTIONS


@pytest.fixture
def question() -> Question:
    return start_question(Clip(1, 32.0, 48.8), "heard words", SECTIONS, 3, Path("/x/q.wav"))


def scripted(*answers: str) -> Callable[[str], str]:
    it = iter(answers)
    return lambda _prompt: next(it)


def test_enter_takes_the_suggested_choice(question: Question) -> None:
    plays: list[Path] = []
    assert ask(question, input_fn=scripted(""), play=plays.append) == "section:3"
    assert plays == [Path("/x/q.wav")]


def test_replay_then_pick_by_number(question: Question) -> None:
    plays: list[Path] = []
    assert (
        ask(question, input_fn=scripted("r", "2"), play=plays.append)
        == question["choices"][1]["id"]
    )
    assert len(plays) == 2


def test_bad_input_asks_again(question: Question) -> None:
    assert ask(question, input_fn=scripted("9", "x", "1"), play=lambda _p: None) == "section:2"


def test_enter_without_a_suggestion_asks_again(question: Question) -> None:
    question["suggested"] = None
    assert ask(question, input_fn=scripted("", "5"), play=lambda _p: None) == "clip:next"
