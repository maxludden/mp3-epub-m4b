"""Tests for transcript-based chapter placement (matching, windows, whisper plumbing)."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from mp3_epub_m4b import transcribe
from mp3_epub_m4b.transcribe import (
    WhisperError,
    Word,
    boundary_in_gap,
    estimate_rate,
    expected_start,
    find_opening,
    find_whisper,
    normalize_words,
    parse_whisper_words,
    score_match,
    search_window,
    transcribe_window,
)


def speak(text: str, start: float, per_word: float = 0.4) -> list[Word]:
    """Fake word-level transcript of ``text`` beginning at ``start`` seconds."""
    return [
        Word(start + i * per_word, start + (i + 1) * per_word, w)
        for i, w in enumerate(text.split())
    ]


def test_normalize_words_drops_case_punctuation_and_apostrophes() -> None:
    assert normalize_words('Kelsier’s "Opinion", 3!') == ["kelsiers", "opinion", "3"]


def test_parse_whisper_words_skips_markers_and_applies_offset() -> None:
    out = (
        "[00:00:00.000 --> 00:00:00.010]  \n"
        "[00:00:01.500 --> 00:00:02.000]   Hello\n"
        "[00:00:02.000 --> 00:00:02.500]   [BLANK_AUDIO]\n"
        "[01:00:00.250 --> 01:00:00.750]   world.\n"
        "not a timing line\n"
    )
    assert parse_whisper_words(out, offset=10.0) == [
        Word(11.5, 12.0, "Hello"),
        Word(3610.25, 3610.75, "world."),
    ]


def test_score_match_tolerates_a_misheard_name() -> None:
    expected = normalize_words("Camon counted his coins dropping the golden boxings")
    heard = normalize_words("Kamen counted his coins dropping the golden boxings")
    assert score_match(expected, heard) > 0.9
    assert score_match(expected, normalize_words("something else entirely")) < 0.5
    assert score_match([], heard) == 0.0


OPENING = "CAMON COUNTED HIS COINS, DROPPING the golden boxings one by one into the small chest"


def test_find_opening_locates_misheard_opening_and_previous_word() -> None:
    before = speak("it ends right here you and I", 100.0)  # last word ends at 102.8
    chapter = speak("Kamen counted his coins dropping the golden boxings one by one into", 111.0)
    after = speak("the small chest on his table", chapter[-1].end)
    hit = find_opening(before + chapter + after, OPENING)
    assert hit is not None
    assert hit.start == pytest.approx(111.0)
    assert hit.prev_end == pytest.approx(102.8)
    assert hit.score > 0.8


def test_find_opening_prefers_the_true_start_over_one_word_early() -> None:
    # the previous chapter's last word ("and") must not be swallowed into the match
    words = speak("narration ends and", 10.0) + speak(OPENING, 20.0)
    hit = find_opening(words, OPENING)
    assert hit is not None and hit.start == pytest.approx(20.0)


def test_find_opening_returns_none_when_not_spoken_or_too_short() -> None:
    words = speak("the narrator talks about something completely unrelated to the book " * 4, 0)
    assert find_opening(words, OPENING) is None
    assert find_opening(words, "Too short") is None
    assert find_opening([], OPENING) is None


def test_find_opening_at_window_start_has_no_previous_word() -> None:
    hit = find_opening(speak(OPENING, 5.0), OPENING)
    assert hit is not None and hit.prev_end is None


def test_boundary_in_gap_cuts_just_before_the_first_word() -> None:
    assert boundary_in_gap(100.0, 118.0) == pytest.approx(117.7)  # long gap: music stays behind
    assert boundary_in_gap(100.0, 101.0) == pytest.approx(100.7)
    assert boundary_in_gap(100.0, 100.2) == 100.0  # gap < lead: cut at the previous word's end
    assert boundary_in_gap(100.0, 99.0) == 99.0  # overlapping stamps: never after the opening
    assert boundary_in_gap(None, 50.0) == pytest.approx(49.7)
    assert boundary_in_gap(None, 0.1) == 0.0
    for prev, nxt in [(10.0, 10.2), (10.0, 30.0), (0.0, 0.0), (5.0, 4.0)]:
        assert boundary_in_gap(prev, nxt) <= nxt


def test_estimate_rate_needs_three_boundaries() -> None:
    words = [100, 100, 100, 100]
    assert estimate_rate({1: 0.0, 2: 50.0}, words, default=9.9) == 9.9
    assert estimate_rate({1: 0.0, 2: 50.0, 3: 100.0}, words, default=9.9) == pytest.approx(0.5)
    assert estimate_rate({1: 0.0, 3: 200.0, 4: 300.0}, words, default=9.9) == pytest.approx(1.0)


def test_expected_start_counts_words_from_the_anchor() -> None:
    assert expected_start(2, 1000.0, 4, [10, 100, 200, 5], 2.0) == pytest.approx(1600.0)
    assert expected_start(3, 50.0, 3, [10, 100, 200], 2.0) == 50.0


def test_search_window_scales_clamps_and_widens() -> None:
    lo, hi = search_window(1000.0, 900.0, 5000.0)
    assert (lo, hi) == (910.0, 1090.0)  # floor half-width 90 s
    lo, hi = search_window(4000.0, 0.0, 5000.0)
    assert (lo, hi) == (3400.0, 4600.0)  # 15% of the distance
    lo, hi = search_window(4900.0, 0.0, 5000.0)
    assert hi == 5000.0  # clamped to the audio
    lo, hi = search_window(1000.0, 995.0, 5000.0)
    assert lo == 1000.0 and lo >= 995.0 + 5.0  # never reaches into the anchor's opening
    assert search_window(1000.0, 900.0, 5000.0, widen=2.5) == (905.0, 1225.0)


def test_find_whisper_prefers_explicit_model_then_env(tmp_path: Path, monkeypatch) -> None:
    explicit, via_env = tmp_path / "a.bin", tmp_path / "b.bin"
    explicit.write_bytes(b"x")
    via_env.write_bytes(b"x")
    monkeypatch.setattr(transcribe.shutil, "which", lambda name: f"/bin/{name}")
    monkeypatch.delenv(transcribe.BIN_ENV, raising=False)
    monkeypatch.setenv(transcribe.MODEL_ENV, str(via_env))
    assert find_whisper(explicit) == ("/bin/whisper-cli", explicit)
    assert find_whisper(None) == ("/bin/whisper-cli", via_env)


def test_find_whisper_reports_missing_binary_and_model(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv(transcribe.BIN_ENV, raising=False)
    monkeypatch.setattr(transcribe.shutil, "which", lambda name: None)
    with pytest.raises(WhisperError, match="not found on PATH"):
        find_whisper()
    monkeypatch.setattr(transcribe.shutil, "which", lambda name: "/bin/whisper-cli")
    monkeypatch.delenv(transcribe.MODEL_ENV, raising=False)
    monkeypatch.delenv("AUDIOBOOK_MCP_WHISPER_MODEL", raising=False)
    monkeypatch.setattr(transcribe, "MODEL_DIRS", (tmp_path / "nowhere",))
    with pytest.raises(WhisperError, match="no whisper model"):
        find_whisper()


def test_transcribe_window_cuts_clip_runs_whisper_and_offsets_times(
    tmp_path: Path, monkeypatch
) -> None:
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        out = "[00:00:01.000 --> 00:00:01.500]   hi\n" if cmd[0] == "whisper-cli" else ""
        return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")

    monkeypatch.setattr(transcribe.subprocess, "run", fake_run)
    words = transcribe_window(
        Path("book.mp3"), 600.0, 30.0, binary="whisper-cli", model=Path("m.bin"), scratch=tmp_path
    )
    assert words == [Word(601.0, 601.5, "hi")]
    assert calls[0][calls[0].index("-ss") + 1] == "600.000"
    assert calls[1][:3] == ["whisper-cli", "-m", "m.bin"]
    assert not (tmp_path / "window.wav").exists()  # temp clip cleaned up


def test_transcribe_window_wraps_failures(tmp_path: Path, monkeypatch) -> None:
    def boom(cmd: list[str], **kw: Any) -> None:
        raise subprocess.CalledProcessError(1, cmd, stderr="model is corrupt")

    monkeypatch.setattr(transcribe.subprocess, "run", boom)
    with pytest.raises(WhisperError, match="model is corrupt"):
        transcribe_window(
            Path("b.mp3"), 0.0, 5.0, binary="whisper-cli", model=Path("m"), scratch=tmp_path
        )
