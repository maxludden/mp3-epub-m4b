"""Questions for a person when anchored alignment can't decide alone, and applying their answers.

A question is plain JSON kept in ``state.json`` under ``pending_question``, so it
can be answered later from anywhere: the terminal prompt, ``mp3-epub-m4b
resolve``, or Claude through the MCP server. Answering only changes the saved
anchoring progress; the pipeline then re-runs and works out what follows.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any, Literal, NotRequired, TypedDict

from mp3_epub_m4b.anchors import LISTEN_SECONDS, MIN_SECTION_WORDS, Clip
from mp3_epub_m4b.epub_extract import Chapter, Section
from mp3_epub_m4b.report import fmt_time

QuestionKind = Literal["start", "end", "chapter"]
CHOICES_SHOWN = 5  # sections or chapters offered per question
TEXT_WORDS = 25  # words of each section's opening shown with a choice


class Choice(TypedDict):
    """One answer.

    ``id`` is ``section:<i>``, ``chapter:<n>``, ``clip:next``, ``clip:prev``,
    ``more`` (show the next sections), ``not-a-boundary`` or ``accept`` (keep the
    chapter as placed).
    """

    id: str
    label: str
    text: str


class Question(TypedDict):
    """A decision only a person can make, with the clip to listen to."""

    kind: QuestionKind
    prompt: str
    clip: int  # clip index, or -1 when the position is not at a clip
    clip_start: float
    clip_seconds: float
    clip_path: str
    heard: str
    choices: list[Choice]
    suggested: str | None
    chapter: NotRequired[int]
    offset: NotRequired[int]  # start/end questions: how far into the section list they begin


class AnchoredProgress(TypedDict, total=False):
    """Checkpointed progress of anchored alignment."""

    heard: dict[str, list[list[Any]]]  # clip index -> [[start, end, text], ...]
    start: dict[str, Any] | None  # {"clip", "section", "confirmed"[, "person"]}
    end: dict[str, Any] | None
    ask_clip: dict[str, int]  # "start"/"end" -> clip the person is being asked about
    offset: dict[str, int]  # "start"/"end" -> first section shown (paging with "more")
    accepted: list[int]  # chapters the person chose to keep as placed
    fixed: dict[str, float]  # chapter number -> start the person chose
    rejected: list[float]  # positions the person said are not chapter starts


class NeedsInput(RuntimeError):
    """Raised when the pipeline is waiting for a person to answer ``question``."""

    def __init__(self, question: Question) -> None:
        super().__init__(f"waiting for input: {question['prompt']}")
        self.question = question


def _snippet(text: str) -> str:
    words = text.split()
    return " ".join(words[:TEXT_WORDS]) + (" …" if len(words) > TEXT_WORDS else "")


def _section_choice(s: Section) -> Choice:
    return {
        "id": f"section:{s['index']}",
        "label": s["title"] or f"(untitled section {s['index'] + 1})",
        "text": _snippet(s["opening"]),
    }


def _base(kind: QuestionKind, prompt: str, clip: Clip, heard: str, clip_path: Path) -> Question:
    return {
        "kind": kind,
        "prompt": prompt,
        "clip": clip.index,
        "clip_start": clip.start,
        "clip_seconds": LISTEN_SECONDS,
        "clip_path": str(clip_path),
        "heard": heard,
        "choices": [],
        "suggested": None,
    }


def _edge_question(
    kind: Literal["start", "end"],
    prompt: str,
    clip: Clip,
    heard: str,
    sections: Sequence[Section],
    suggested: int | None,
    clip_path: Path,
    *,
    offset: int,
    can_move: bool,
) -> Question:
    matchable = [s for s in sections if s["word_count"] >= MIN_SECTION_WORDS]
    ordered = matchable if kind == "start" else matchable[::-1]
    page = ordered[offset : offset + CHOICES_SHOWN]
    shown = sorted(page, key=lambda s: s["index"])
    if suggested is not None and all(s["index"] != suggested for s in shown):
        shown = sorted([*shown, sections[suggested]], key=lambda s: s["index"])
    q = _base(kind, prompt, clip, heard, clip_path)
    q["offset"] = offset
    q["choices"] = [_section_choice(s) for s in shown]
    if offset + CHOICES_SHOWN < len(ordered):
        where = "later" if kind == "start" else "earlier"
        q["choices"].append(
            {"id": "more", "label": "More sections", "text": f"Show {where} sections."}
        )
    if can_move:
        step, word = ("clip:next", "next") if kind == "start" else ("clip:prev", "previous")
        q["choices"].append(
            {"id": step, "label": "None of these", "text": f"Play the {word} clip instead."}
        )
    q["suggested"] = f"section:{suggested}" if suggested is not None else None
    return q


def start_question(
    clip: Clip,
    heard: str,
    sections: Sequence[Section],
    suggested: int | None,
    clip_path: Path,
    *,
    offset: int = 0,
    last_clip: bool = False,
) -> Question:
    """Ask which section the narrator begins in ``clip`` (``CHOICES_SHOWN`` sections from the front)."""
    prompt = "Where does the book start? Listen to the clip and pick the section the narrator is reading."
    return _edge_question(
        "start", prompt, clip, heard, sections, suggested, clip_path,
        offset=offset, can_move=not last_clip,
    )  # fmt: skip


def end_question(
    clip: Clip,
    heard: str,
    sections: Sequence[Section],
    suggested: int | None,
    clip_path: Path,
    *,
    offset: int = 0,
) -> Question:
    """Ask which section the narrator begins in ``clip`` (``CHOICES_SHOWN`` sections from the back)."""
    prompt = (
        "Where does the last narrated section start? Listen to the clip and pick the section "
        "the narrator is reading."
    )
    return _edge_question(
        "end", prompt, clip, heard, sections, suggested, clip_path,
        offset=offset, can_move=clip.index > 0,
    )  # fmt: skip


def chapter_question(
    n: int,
    clip: Clip,
    heard: str,
    chapters: Sequence[Chapter],
    candidates: Sequence[int],
    clip_path: Path,
    *,
    actual: float,
    expected: float,
    allow_reject: bool = True,
) -> Question:
    """Ask which chapter starts at ``clip`` because chapter ``n`` is far from its expected length."""
    title = chapters[n - 1]["title"]
    prompt = (
        f"Chapter {n} ({title}) is {fmt_time(actual)} long but its text suggests about "
        f"{fmt_time(expected)}. Which chapter starts at {fmt_time(clip.start)}?"
    )
    q = _base("chapter", prompt, clip, heard, clip_path)
    q["chapter"] = n
    q["choices"] = [
        {
            "id": f"chapter:{c}",
            "label": f"{c}. {chapters[c - 1]['title']}",
            "text": _snippet(chapters[c - 1].get("opening", "")),
        }
        for c in candidates
    ]
    if allow_reject:
        q["choices"].append(
            {
                "id": "not-a-boundary",
                "label": "No chapter starts here",
                "text": "This is a pause inside a chapter.",
            }
        )
    q["choices"].append(
        {
            "id": "accept",
            "label": "Keep it as it is",
            "text": "The chapter is placed right; its length is just unusual.",
        }
    )
    return q


def apply_answer(progress: AnchoredProgress, question: Question, choice_id: str) -> None:
    """Record the person's answer in ``progress``.

    Raises:
        ValueError: If ``choice_id`` is not one of the question's choices.
    """
    if choice_id not in {c["id"] for c in question["choices"]}:
        raise ValueError(f"{choice_id!r} is not a choice for this question")
    kind, _, value = choice_id.partition(":")
    q = question["kind"]
    if q in ("start", "end"):
        if kind == "section":
            progress[q] = {
                "clip": question["clip"],
                "section": int(value),
                "confirmed": True,
                "person": True,
            }
            progress.setdefault("offset", {})[q] = 0
        elif kind == "more":
            progress.setdefault("offset", {})[q] = question.get("offset", 0) + CHOICES_SHOWN
            progress.setdefault("ask_clip", {})[q] = question["clip"]
            progress[q] = None
        else:
            step = 1 if value == "next" else -1
            progress.setdefault("ask_clip", {})[q] = question["clip"] + step
            progress[q] = None
    elif kind == "chapter":
        progress.setdefault("fixed", {})[value] = question["clip_start"]
    elif kind == "accept":
        progress.setdefault("accepted", []).append(question.get("chapter", 0))
    else:  # not-a-boundary
        progress.setdefault("rejected", []).append(question["clip_start"])
