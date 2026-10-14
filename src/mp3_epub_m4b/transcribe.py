"""Transcript-based chapter placement: find where each chapter's opening words are spoken.

Silence detection fails on recordings whose chapters are separated by a
sound, or by nothing at all, rather than by quiet. This module instead
transcribes a short window of audio around where a chapter should start
(estimated from word counts), then locates the chapter's opening words in the
word-level transcript.

Everything except :func:`transcribe_window` is pure, so the matching and
window arithmetic can be tested on fake transcripts.
"""

from __future__ import annotations

import difflib
import os
import re
import shutil
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

from loguru import logger

MATCH_WORDS = 18  # how many opening words are compared against the transcript
MIN_MATCH_WORDS = 4  # openings with fewer words are too ambiguous to match
MIN_SCORE = 0.62  # similarity (0-1) below which a window position is not a match
CONFIDENT_SCORE = 0.8  # matches scoring below this are reported as low confidence
MIN_WINDOW_HALF = 90.0  # smallest half-width (s) of a search window
WINDOW_FRACTION = 0.15  # half-width as a share of the distance from the anchor
WIDEN_STEPS = (1.0, 2.5, 6.0)  # window multipliers tried in turn; narration pace can drift minutes
OPENING_LEAD = 0.3  # how far (s) before a chapter's first word its marker is placed
MODEL_ENV = "MP3_EPUB_M4B_WHISPER_MODEL"
MODEL_ENV_FALLBACKS = ("AUDIOBOOK_MCP_WHISPER_MODEL",)
BIN_ENV = "AUDIOBOOK_MCP_WHISPER_BIN"
WHISPER_BINARIES = ("whisper-cli", "whisper-cpp")
MODEL_DIRS = (Path("~/.cache/whisper-models"), Path("~/.cache/whisper.cpp"))
MODEL_NAMES = ("ggml-base.en.bin", "ggml-small.en.bin", "ggml-medium.en.bin")

_LINE = re.compile(r"\[(\d+):(\d+):([\d.]+)\s*-->\s*(\d+):(\d+):([\d.]+)\]\s*(.*)")
_TOKEN = re.compile(r"[a-z0-9]+")


class WhisperError(RuntimeError):
    """whisper.cpp or its model is missing, or a transcription run failed."""


class Word(NamedTuple):
    """One transcribed word with absolute start and end times (s)."""

    start: float
    end: float
    text: str


@dataclass(frozen=True)
class Match:
    """Where a chapter's opening words were found in a transcript.

    Attributes:
        index: Position of the first matched word in the transcript.
        score: Similarity of the match, 0 to 1.
        start: Start time (s) of the first matched word.
        prev_end: End time (s) of the word before it, or ``None`` if the match
            is the first word of the window.
    """

    index: int
    score: float
    start: float
    prev_end: float | None


def normalize_words(text: str) -> list[str]:
    """Lowercase ``text`` and split it into bare alphanumeric tokens (apostrophes dropped)."""
    return _TOKEN.findall(text.lower().replace("'", "").replace("’", ""))


def parse_whisper_words(output: str, offset: float = 0.0) -> list[Word]:
    """Parse ``whisper-cli -ml 1 -sow`` output (one word per line) into :class:`Word` s.

    Args:
        output: Standard output of whisper-cli.
        offset: Seconds to add, turning clip-relative times into file times.

    Returns:
        The words in order; blank lines and bracketed markers such as
        ``[BLANK_AUDIO]`` are dropped.
    """
    words: list[Word] = []
    for line in output.splitlines():
        m = _LINE.match(line)
        if not m:
            continue
        text = m.group(7).strip()
        if not text or (text.startswith(("[", "(")) and text.endswith(("]", ")"))):
            continue
        start = int(m[1]) * 3600 + int(m[2]) * 60 + float(m[3])
        end = int(m[4]) * 3600 + int(m[5]) * 60 + float(m[6])
        words.append(Word(offset + start, offset + end, text))
    return words


def score_match(expected: Sequence[str], heard: Sequence[str]) -> float:
    """Similarity (0-1) between the expected opening tokens and the tokens heard.

    Compares the joined strings character by character rather than word by
    word, so a misheard name ("Kamen" for "Camon") costs a few characters
    instead of a whole word.
    """
    if not expected or not heard:
        return 0.0
    return difflib.SequenceMatcher(
        None, " ".join(expected), " ".join(heard), autojunk=False
    ).ratio()


def find_opening(words: Sequence[Word], opening: str, min_score: float = MIN_SCORE) -> Match | None:
    """Locate a chapter's opening words in a transcript.

    Slides the first ``MATCH_WORDS`` expected words along the transcript and
    keeps the best-scoring position (the earliest on ties).

    Args:
        words: Word-level transcript of the search window.
        opening: The chapter's opening text from the EPUB.
        min_score: Lowest similarity accepted as a match.

    Returns:
        The best :class:`Match`, or ``None`` if nothing scores ``min_score``
        or the opening is too short to match reliably.
    """
    expected = normalize_words(opening)[:MATCH_WORDS]
    if len(expected) < MIN_MATCH_WORDS:
        return None
    tokens = [normalize_words(w.text) for w in words]
    # whisper may split or merge words, so flatten to one token per entry but
    # remember which transcript word each token came from
    flat: list[str] = []
    origin: list[int] = []
    for i, toks in enumerate(tokens):
        for t in toks:
            flat.append(t)
            origin.append(i)
    # a position near the window's end can only see part of the opening
    need = min(len(expected), 8)
    best_score, best_at = 0.0, -1
    for j in range(len(flat) - need + 1):
        # exactly as many tokens as expected: a longer window lets a start one
        # word early (the previous chapter's last word) outscore the true start
        heard = flat[j : j + len(expected)]
        s = score_match(expected, heard)
        if s > best_score:
            best_score, best_at = s, j
    if best_at < 0 or best_score < min_score:
        logger.debug(f"no match for {' '.join(expected[:6])!r} (best score {best_score:.2f})")
        return None
    w_idx = origin[best_at]
    prev_end = words[w_idx - 1].end if w_idx > 0 else None
    logger.debug(
        f"matched {' '.join(expected[:6])!r} at {words[w_idx].start:.1f}s ({best_score:.2f})"
    )
    return Match(w_idx, best_score, words[w_idx].start, prev_end)


def boundary_in_gap(prev_end: float | None, next_start: float) -> float:
    """Choose the chapter boundary between the last word of one chapter and the first of the next.

    The marker goes just before the opening's first word, so jumping to a
    chapter lands on its narration and skips any music, credits or "Part 2 of
    3" announcement before it. Those stay at the tail of the previous chapter.
    The lead is small because whisper's word starts can run slightly late.

    Args:
        prev_end: End time (s) of the last word before the opening, if any.
        next_start: Start time (s) of the opening's first word.

    Returns:
        The boundary time in seconds: ``OPENING_LEAD`` before ``next_start``,
        but never inside the previous word and never after ``next_start``.
    """
    boundary = max(0.0, next_start - OPENING_LEAD)
    if prev_end is not None:
        boundary = max(boundary, prev_end)
    return min(boundary, next_start)


def estimate_rate(boundaries: Mapping[int, float], words: Sequence[int], default: float) -> float:
    """Seconds per word, from the confirmed boundaries so far.

    Args:
        boundaries: Confirmed chapter number (1-based) -> start (s).
        words: Word count of each chapter, index 0 being chapter 1.
        default: Rate (s/word) to use until three boundaries are known.

    Returns:
        ``default`` with fewer than three boundaries; otherwise the rate over
        the span between the first and last confirmed chapters.
    """
    if len(boundaries) < 3:
        return default
    first, last = min(boundaries), max(boundaries)
    span_words = sum(words[first - 1 : last - 1])
    if span_words <= 0 or boundaries[last] <= boundaries[first]:
        return default
    return (boundaries[last] - boundaries[first]) / span_words


def expected_start(
    anchor: int, anchor_start: float, chapter: int, words: Sequence[int], rate: float
) -> float:
    """Predicted start (s) of ``chapter``, counted from a confirmed ``anchor`` chapter."""
    return anchor_start + sum(words[anchor - 1 : chapter - 1]) * rate


def search_window(
    expected: float, anchor_start: float, total_dur: float, widen: float = 1.0
) -> tuple[float, float]:
    """The ``(start, end)`` span (s) to transcribe when looking for a chapter.

    The window is centred on ``expected``. Its half-width grows with the
    distance from the confirmed anchor, because estimates drift over distance,
    and never reaches back into the anchor chapter's own opening.

    Args:
        expected: Predicted chapter start (s).
        anchor_start: Start (s) of the last confirmed chapter.
        total_dur: Audio duration (s).
        widen: Multiplier for retries after a miss.
    """
    half = max(MIN_WINDOW_HALF, WINDOW_FRACTION * max(0.0, expected - anchor_start)) * widen
    lo = max(anchor_start + 5.0, expected - half)
    hi = min(total_dur, expected + half)
    return lo, max(hi, lo)


def find_whisper(model: Path | None = None) -> tuple[str, Path]:
    """Locate the whisper.cpp binary and a GGML model.

    The model is, in order: the ``model`` argument, ``MP3_EPUB_M4B_WHISPER_MODEL``
    (or ``AUDIOBOOK_MCP_WHISPER_MODEL``), then the first English model found in
    the usual cache folders.

    Raises:
        WhisperError: If either cannot be found.
    """
    binary = os.environ.get(BIN_ENV) or next(
        (b for name in WHISPER_BINARIES if (b := shutil.which(name))), None
    )
    if not binary:
        raise WhisperError("whisper.cpp not found on PATH (brew install whisper-cpp)")
    candidates: list[Path] = []
    if model:
        candidates.append(model)
    for var in (MODEL_ENV, *MODEL_ENV_FALLBACKS):
        if os.environ.get(var):
            candidates.append(Path(os.environ[var]))
    candidates += [d.expanduser() / n for d in MODEL_DIRS for n in MODEL_NAMES]
    found = next((c for c in candidates if c.is_file()), None)
    if found is None:
        raise WhisperError(
            f"no whisper model found; pass --whisper-model or set {MODEL_ENV} to a ggml-*.bin file"
        )
    return binary, found


def transcribe_window(
    audio: Path, start: float, duration: float, *, binary: str, model: Path, scratch: Path
) -> list[Word]:
    """Transcribe ``duration`` seconds of ``audio`` from ``start`` into timed words.

    Args:
        audio: Source audio file.
        start: Window start (s).
        duration: Window length (s).
        binary: whisper.cpp command.
        model: GGML model file.
        scratch: Folder for the temporary WAV clip.

    Returns:
        Words with times in absolute file seconds.

    Raises:
        WhisperError: If ffmpeg or whisper fails.
    """
    scratch.mkdir(parents=True, exist_ok=True)
    clip = scratch / "window.wav"
    cut = [
        "ffmpeg", "-nostdin", "-loglevel", "error", "-y",
        "-ss", f"{start:.3f}", "-t", f"{duration:.3f}",
        "-i", str(audio), "-vn", "-ar", "16000", "-ac", "1", str(clip),
    ]  # fmt: skip
    asr = [binary, "-m", str(model), "-f", str(clip), "-ml", "1", "-sow", "-np", "-l", "en"]
    logger.debug(f"transcribing {duration:.0f}s from {start:.0f}s")
    try:
        subprocess.run(cut, check=True, capture_output=True, text=True)
        out = subprocess.run(asr, check=True, capture_output=True, text=True).stdout
    except (OSError, subprocess.CalledProcessError) as e:
        detail = getattr(e, "stderr", "") or str(e)
        raise WhisperError(f"transcription failed: {detail.strip()[-300:]}") from e
    finally:
        clip.unlink(missing_ok=True)
    return parse_whisper_words(out, offset=start)
