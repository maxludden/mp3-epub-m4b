"""Checkpoint types and persistence for the resumable pipeline.

The pipeline writes its progress to ``<work_dir>/state.json``. This module owns
the shape of that file, the fingerprint that ties it to particular inputs and
options, and atomic reads and writes.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, NotRequired, TypedDict

from loguru import logger

from mp3_epub_m4b.epub_extract import EpubResult

STAGES = ("extract", "scan", "align", "assemble", "deliver", "readaloud")


class Segment(TypedDict):
    """One output chapter: a time range of the source audio.

    ``words`` (EPUB word count) and ``note`` (how the boundary was found) are
    absent for intro/outro segments and for checkpoints made by older versions.
    """

    n: int
    title: str
    start: float
    end: float
    duration: float
    words: NotRequired[int]
    note: NotRequired[str]


class AlignProgress(TypedDict):
    """Checkpointed progress of the align stage."""

    boundaries: dict[str, float]  # chapter number (as str, JSON keys) -> start in seconds
    next: int  # next chapter number to locate
    low_confidence: list[int]  # chapters whose boundary lacked a companion gap
    fallback: list[int]  # chapters placed by a fixed offset (no gap found)
    outro_start: NotRequired[float | None]  # set once the outro search has run
    unmatched: NotRequired[list[int]]  # transcribe mode: chapters whose opening was never heard
    method: NotRequired[str]  # how chapters are being placed: "silence" or "transcribe"


class PipelineState(TypedDict):
    """Checkpoint persisted to ``state.json``; keys appear as stages complete."""

    stage: str  # one of STAGES, or "done"
    inputs: NotRequired[dict[str, Any]]  # fingerprint of the files/options this state belongs to
    epub: NotRequired[EpubResult]
    mp3_duration: NotRequired[float]
    align: NotRequired[AlignProgress]
    segments: NotRequired[list[Segment]]
    assembled_path: NotRequired[str]
    deliver_bytes_done: NotRequired[int]
    final_output: NotRequired[str]
    readaloud_output: NotRequired[str]  # the aligned EPUB 3, once the readaloud stage is done


class StateMismatchError(RuntimeError):
    """A saved checkpoint was made for different input files or options."""


def check_fingerprint(path: Path, saved: dict[str, Any], current: dict[str, Any]) -> None:
    """Refuse to reuse a checkpoint made for different inputs or options.

    Args:
        path: The checkpoint file (named in the error message).
        saved: Fingerprint stored in the checkpoint.
        current: Fingerprint of this run.

    Raises:
        StateMismatchError: If any fingerprint key differs.
    """
    changed = sorted(k for k in current if saved.get(k) != current[k])
    if changed:
        logger.error(f"{path.name}: checkpoint does not match this run ({', '.join(changed)})")
        raise StateMismatchError(
            f"{path} was made for different input/options ({', '.join(changed)}). "
            "Re-run with the original settings, or start over "
            "(delete the work directory / use --fresh)."
        )


def read_state(path: Path) -> PipelineState:
    """Read a checkpoint file.

    Raises:
        OSError: If the file can't be read.
        ValueError: If it isn't valid JSON.
    """
    state: PipelineState = json.loads(path.read_text())
    logger.debug(f"resumed checkpoint {path} at stage {state.get('stage')!r}")
    return state


def write_state(path: Path, state: PipelineState) -> None:
    """Atomically write ``state`` to ``path`` (temp file + rename)."""
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False))
    tmp.replace(path)
    logger.trace(f"checkpoint saved: stage {state.get('stage')!r}")
