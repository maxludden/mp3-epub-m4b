"""Pipeline tests: boundary search on fake silences, plus a real end-to-end ffmpeg run."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from mp3_epub_m4b.align import AlignError, AlignOptions
from mp3_epub_m4b.ffmpeg_tools import scan_silences
from mp3_epub_m4b.run import Pipeline
from mp3_epub_m4b.state import PipelineState, StateMismatchError

from .conftest import CHAPTERS, expected_boundaries


def make_pipeline(
    tmp_path: Path,
    mp3: Path | None = None,
    epub: Path | None = None,
    work: Path | None = None,
    out: Path | None = None,
    **kw,
) -> Pipeline:
    """Build a Pipeline with dummy input files unless real ones are given."""
    if mp3 is None:
        mp3 = tmp_path / "a.mp3"
        mp3.write_bytes(b"x")
    if epub is None:
        epub = tmp_path / "b.epub"
        epub.write_bytes(b"x")
    return Pipeline(
        mp3,
        epub,
        out or tmp_path / "out",
        work or tmp_path / "work",
        3.5,
        "Opening Credits",
        "End Credits",
        1800.0,
        "64k",
        **kw,
    )


def prime_align(
    p: Pipeline, silences: list[tuple[float, float]], words: list[int], total: float
) -> None:
    """Put a pipeline straight into the align stage with a fake scan and fake chapters."""
    p.silences_path.write_text(
        json.dumps({"noise_db": -30, "min_silence": 1.5, "silences": silences})
    )
    state: PipelineState = p.state
    state["epub"] = {
        "meta": {
            "title": "T",
            "author": "A",
            "publisher": "",
            "year": "",
            "series": "",
            "series_index": "",
        },
        "cover": None,
        "chapters": [{"n": i, "title": f"Ch{i}", "word_count": w} for i, w in enumerate(words, 1)],
    }
    state["mp3_duration"] = total
    state["stage"] = "align"
    state["align"] = {"boundaries": {}, "next": 1, "low_confidence": [], "fallback": []}


def test_scene_break_is_not_a_boundary(tmp_path: Path, logs: list[tuple[str, str]]) -> None:
    p = make_pipeline(tmp_path)
    # chapter 1 is 100 s of speech, with a 4 s pause at 20 s (scene break) and the
    # real chapter gap at 100 s; chapter 2 runs on to the end.
    prime_align(p, [(20.0, 24.0), (100.0, 104.0), (105.0, 107.0)], [1000, 1000], 200.0)
    p.stage_align(60)
    b = p._align()["boundaries"]
    assert b["1"] == 0.0 and b["2"] == pytest.approx(102.0)
    assert any("scene break" in m for _lvl, m in logs)
    assert p._align()["low_confidence"] == []  # companion gap at 105 s found


def test_fallback_overrun_raises(tmp_path: Path) -> None:
    p = make_pipeline(tmp_path)
    prime_align(p, [], [100, 100, 100], 1000.0)
    with pytest.raises(RuntimeError, match="end of the audio|ran out of audio"):
        p.stage_align(60)
    # chapter 2 was placed 900 s in; chapter 3 could only be clamped to the very end
    assert p._align()["boundaries"]["2"] == 900.0
    assert "3" not in p._align()["boundaries"]


def test_state_mismatch(tmp_path: Path) -> None:
    make_pipeline(tmp_path)
    with pytest.raises(StateMismatchError, match="min_gap"):
        Pipeline(
            tmp_path / "a.mp3",
            tmp_path / "b.epub",
            tmp_path / "out",
            tmp_path / "work",
            2.0,
            "Opening Credits",
            "End Credits",
            1800.0,
            "64k",
        )


def test_unknown_stage_raises(tmp_path: Path) -> None:
    p = make_pipeline(tmp_path)
    p.state["stage"] = "bogus"
    with pytest.raises(ValueError, match="unknown pipeline stage"):
        p.run(1)


def test_deliver_copy_is_resumable_and_refuses_overwrite(tmp_path: Path, monkeypatch) -> None:
    from mp3_epub_m4b import run as run_mod

    monkeypatch.setattr(run_mod, "_same_filesystem", lambda a, b: False)  # force the copy path
    p = make_pipeline(tmp_path)
    src = tmp_path / "work" / "Book.m4b"
    src.write_bytes(b"0123456789" * 1000)
    p.state["assembled_path"] = str(src)
    p.state["stage"] = "deliver"
    assert p.stage_deliver(0.0) is False  # zero budget: nothing copied yet
    part = tmp_path / "out" / "Book.m4b.part"
    assert part.exists() and not (tmp_path / "out" / "Book.m4b").exists()
    assert p.stage_deliver(30.0) is True
    assert (tmp_path / "out" / "Book.m4b").read_bytes() == b"0123456789" * 1000
    assert not part.exists() and not src.exists() and p.state["stage"] == "done"

    # a second delivery of the same name refuses to overwrite ...
    src.write_bytes(b"new")
    p.state["assembled_path"] = str(src)
    p.state["stage"] = "deliver"
    with pytest.raises(FileExistsError):
        p.stage_deliver(30.0)
    # ... unless asked to
    p.overwrite = True
    assert p.stage_deliver(30.0) is True
    assert (tmp_path / "out" / "Book.m4b").read_bytes() == b"new"


def _fake_align(calls: list[Path], fail: bool = False):
    """Stand-in for ``align_book`` that records its output path and writes a file there."""

    def fake(
        audio: Path, epub: Path, out: Path | None = None, opts: AlignOptions | None = None
    ) -> Path:
        assert out is not None
        calls.append(out)
        out.write_bytes(b"partial")
        if fail:
            raise AlignError("boom")
        return out

    return fake


def test_readaloud_runs_after_deliver(tmp_path: Path, monkeypatch) -> None:
    from mp3_epub_m4b import run as run_mod

    calls: list[Path] = []
    monkeypatch.setattr(run_mod, "align_book", _fake_align(calls))
    p = make_pipeline(tmp_path, readaloud=AlignOptions())
    assert p.stages[-1] == "readaloud"
    src = tmp_path / "work" / "Book.m4b"
    src.write_bytes(b"audio")
    p.state["assembled_path"] = str(src)
    p.state["stage"] = "deliver"
    assert p.run(5.0) is True
    assert p.state["stage"] == "readaloud"  # delivery no longer ends the pipeline
    assert p.run(5.0) is True
    out = tmp_path / "out" / "b (aligned).epub"
    assert p.state["stage"] == "done" and p.state["readaloud_output"] == str(out)
    assert out.read_bytes() == b"partial" and not calls[0].exists()  # renamed from .partial.epub


def test_readaloud_disabled_by_default(tmp_path: Path) -> None:
    p = make_pipeline(tmp_path)
    assert "readaloud" not in p.stages
    src = tmp_path / "work" / "Book.m4b"
    src.write_bytes(b"audio")
    p.state["assembled_path"] = str(src)
    p.state["stage"] = "deliver"
    p.run(5.0)
    assert p.state["stage"] == "done"


def test_readaloud_failure_keeps_stage_and_leaves_no_final_file(
    tmp_path: Path, monkeypatch
) -> None:
    from mp3_epub_m4b import run as run_mod

    monkeypatch.setattr(run_mod, "align_book", _fake_align([], fail=True))
    p = make_pipeline(tmp_path, readaloud=AlignOptions())
    m4b = tmp_path / "out" / "Book.m4b"
    m4b.parent.mkdir()
    m4b.write_bytes(b"audio")
    p.state.update({"stage": "readaloud", "final_output": str(m4b)})
    with pytest.raises(AlignError):
        p.run(5.0)
    assert p.state["stage"] == "readaloud"
    assert not (tmp_path / "out" / "b (aligned).epub").exists()


def test_finished_book_gets_readaloud_on_rerun(tmp_path: Path) -> None:
    p = make_pipeline(tmp_path)
    p.state.update({"stage": "done", "final_output": str(tmp_path / "out" / "Book.m4b")})
    p._save()
    again = make_pipeline(tmp_path, readaloud=AlignOptions())
    assert again.state["stage"] == "readaloud"


@pytest.mark.ffmpeg
def test_scan_silences_finds_known_gaps(synthetic_book) -> None:
    mp3, _epub, duration = synthetic_book
    seen: list[float] = []
    sil = scan_silences(mp3, duration, -30, 1.5, on_progress=lambda d, t: seen.append(d))
    long_gaps = [(s, e) for s, e in sil if e - s >= 3.5]
    assert len(long_gaps) == 3  # two chapter gaps + the scene break
    assert seen and seen[-1] == pytest.approx(duration)


@pytest.mark.ffmpeg
def test_end_to_end(tmp_path: Path, synthetic_book, logs: list[tuple[str, str]]) -> None:
    mp3, epub, duration = synthetic_book
    p = make_pipeline(tmp_path, mp3, epub)
    for _ in range(50):
        if p.state["stage"] == "done":
            break
        p.run(5.0)
    assert p.state["stage"] == "done"

    final = p.state.get("final_output")
    assert final is not None
    out = Path(final)
    assert out.name == "Fish & Chips A Test.m4b" and out.exists()
    # no stray intermediates left under the final name
    assert not list(out.parent.glob("*.part")) and not list(out.parent.glob("*.tmp"))

    probe = json.loads(
        subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_chapters",
                "-show_format",
                "-show_streams",
                "-of",
                "json",
                str(out),
            ],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    )
    chapters = probe["chapters"]
    assert [c["tags"]["title"] for c in chapters] == [ch.title for ch in CHAPTERS]
    starts = [float(c["start_time"]) for c in chapters]
    for got, want in zip(starts, expected_boundaries(), strict=True):
        assert got == pytest.approx(want, abs=0.5)
    # chapters are contiguous and cover the whole file (gapless single encode)
    for a, b in zip(chapters, chapters[1:], strict=False):
        assert a["end"] == b["start"]
    assert float(probe["format"]["duration"]) == pytest.approx(duration, abs=1.0)
    tags = probe["format"]["tags"]
    assert tags["title"] == "Fish & Chips: A Test" and tags["artist"] == "Ann Author"
    assert any(s["codec_type"] == "video" for s in probe["streams"])  # cover art
    # the only warning allowed is the (expected) "chapters are very short" plausibility hint
    warnings = [m for lvl, m in logs if lvl == "warning" and "right EPUB" not in m]
    assert not warnings, warnings

    # re-running a finished pipeline is a no-op, and a fresh one refuses to clobber it
    p2 = make_pipeline(tmp_path, mp3, epub)
    assert p2.state["stage"] == "done"
    p3 = make_pipeline(tmp_path, mp3, epub, work=tmp_path / "work2")
    with pytest.raises(FileExistsError):
        for _ in range(50):
            if p3.run(5.0) and p3.state["stage"] == "done":
                break


def _deliver_pipeline(tmp_path: Path, **kw) -> tuple[Pipeline, Path]:
    p = make_pipeline(tmp_path, **kw)
    src = tmp_path / "work" / "Book.m4b"
    src.write_bytes(b"audio")
    p.state["assembled_path"] = str(src)
    p.state["stage"] = "deliver"
    return p, src


def test_deliver_stale_part_does_not_clobber_existing_output(tmp_path: Path) -> None:
    p, _src = _deliver_pipeline(tmp_path)
    out = tmp_path / "out"
    out.mkdir()
    (out / "Book.m4b").write_bytes(b"precious")
    (out / "Book.m4b.part").write_bytes(b"stale")
    with pytest.raises(FileExistsError):
        p.stage_deliver(5.0)
    assert (out / "Book.m4b").read_bytes() == b"precious"


def test_deliver_resumes_after_crash_following_move(tmp_path: Path) -> None:
    p, src = _deliver_pipeline(tmp_path)
    out = tmp_path / "out"
    out.mkdir()
    src.replace(out / "Book.m4b")  # moved, but the state was never saved
    assert p.stage_deliver(5.0) is True
    assert p.state["stage"] == "done" and p.state["final_output"] == str(out / "Book.m4b")


def test_deliver_resumes_after_crash_before_src_removed(tmp_path: Path) -> None:
    p, src = _deliver_pipeline(tmp_path)
    out = tmp_path / "out"
    out.mkdir()
    (out / "Book.m4b").write_bytes(b"audio")  # copy promoted; src still around
    p.state["deliver_bytes_done"] = 5
    assert p.stage_deliver(5.0) is True
    assert p.state["stage"] == "done" and not src.exists()


def test_cli_defaults_and_slice_validation() -> None:
    from typer.testing import CliRunner

    from mp3_epub_m4b.cli import app

    result = CliRunner().invoke(app, ["convert", __file__, __file__, "--slice-seconds", "0"])
    assert result.exit_code != 0 and "slice-seconds" in result.output.lower().replace("_", "-")
    import typer.main

    for name in ("convert", "batch"):
        params = {q.name: q.default for q in typer.main.get_command(app).commands[name].params}  # type: ignore[attr-defined]
        assert params["detect_intro"] is False and params["detect_outro"] is False


def test_stale_resume_fingerprint_rejects_changed_input(tmp_path: Path) -> None:
    mp3 = tmp_path / "a.mp3"
    mp3.write_bytes(b"x")
    make_pipeline(tmp_path, mp3=mp3)
    mp3.write_bytes(b"a much longer, different recording")  # same name, new size
    with pytest.raises(StateMismatchError, match="mp3_size"):
        make_pipeline(tmp_path, mp3=mp3)


def test_stale_resume_accepts_identical_input(tmp_path: Path) -> None:
    p = make_pipeline(tmp_path)
    p.state["stage"] = "scan"
    p._save()
    assert make_pipeline(tmp_path).state["stage"] == "scan"


def test_boundary_never_lands_beyond_the_end_of_the_audio(tmp_path: Path) -> None:
    p = make_pipeline(tmp_path)
    # a silence that runs well past EOF (as a trailing-silence scan can report)
    prime_align(p, [(190.0, 260.0)], [1000, 1000], 200.0)
    p.stage_align(60)
    assert 0.0 < p._align()["boundaries"]["2"] < 200.0


def test_deliver_never_overwrites_without_the_flag(tmp_path: Path) -> None:
    p, _src = _deliver_pipeline(tmp_path)
    out = tmp_path / "out"
    out.mkdir()
    (out / "Book.m4b").write_bytes(b"existing")
    with pytest.raises(FileExistsError, match="already exists"):
        p.stage_deliver(5.0)
    assert (out / "Book.m4b").read_bytes() == b"existing"
    assert p.state["stage"] == "deliver"  # nothing advanced
