"""Tests for the CLI helpers and work-folder safety."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from mp3_epub_m4b import converter
from mp3_epub_m4b.cli import app
from mp3_epub_m4b.converter import convert_book, stage_progress
from mp3_epub_m4b.options import Options
from mp3_epub_m4b.pairing import find_epub, pair_by_name, pair_files
from mp3_epub_m4b.run import work_name
from mp3_epub_m4b.state import PipelineState


def _options(work_dir: Path, *, fresh: bool = False) -> Options:
    """Build :class:`Options` with throwaway values."""
    return Options(work_dir, 3.5, "96k", False, 120.0, False, 180.0, 5.0, fresh=fresh)


def test_work_name_never_empty() -> None:
    assert work_name("Book 1") == "Book_1"
    unicode_name = work_name("日本語")
    assert unicode_name.startswith("book_") and len(unicode_name) > len("book_")
    assert work_name("日本語") == unicode_name != work_name("中文")


def test_fresh_with_unsluggable_name_keeps_other_books(tmp_path: Path) -> None:
    work = tmp_path / "work"
    other = work / "Other_Book"
    other.mkdir(parents=True)
    (other / "state.json").write_text("{}")
    epub = tmp_path / "日本語.epub"  # not a real EPUB: the pipeline fails after --fresh ran
    epub.write_bytes(b"")
    audio = tmp_path / "a.mp3"
    audio.write_bytes(b"")
    with pytest.raises(Exception):  # noqa: B017 - any pipeline failure is fine here
        convert_book(audio, epub, tmp_path / "out", _options(work, fresh=True))
    assert (other / "state.json").exists()


def test_pair_files_ignores_duplicate_numbers(tmp_path: Path) -> None:
    for name in ["Volume 1.mp3", "Volume 1.m4a", "a_01.epub", "b_1.epub"]:
        (tmp_path / name).write_text("")
    audio, epubs = pair_files(tmp_path)
    assert audio[1].name == "Volume 1.m4a"  # sorted order: first one wins
    assert epubs[1].name == "a_01.epub"


def test_stage_progress_align_and_idle() -> None:
    state: PipelineState = {"stage": "scan"}
    assert stage_progress(state) == (0, None)
    state = {
        "stage": "align",
        "align": {"boundaries": {}, "next": 3, "low_confidence": [], "fallback": []},
        "epub": {
            "meta": {
                "title": "",
                "author": "",
                "publisher": "",
                "year": "",
                "series": "",
                "series_index": "",
            },
            "cover": None,
            "chapters": [{"n": i, "title": "", "word_count": 1} for i in range(1, 6)],
        },
    }
    assert stage_progress(state) == (2, 5)


def test_stage_progress_deliver_in_megabytes(tmp_path: Path) -> None:
    f = tmp_path / "x.m4b"
    f.write_bytes(b"x" * 2_000_000)
    state: PipelineState = {
        "stage": "deliver",
        "assembled_path": str(f),
        "deliver_bytes_done": 500_000,
    }
    assert stage_progress(state) == (0.5, 2.0)


def test_help_lists_commands() -> None:
    result = CliRunner().invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "convert" in result.output and "batch" in result.output


@pytest.mark.ffmpeg
def test_dry_run_prints_chapters_and_encodes_nothing(
    tmp_path: Path, synthetic_book: tuple[Path, Path, float]
) -> None:
    mp3, epub, _duration = synthetic_book
    work, out = tmp_path / "work", tmp_path / "out"
    result = CliRunner().invoke(
        app,
        ["convert", str(mp3), str(epub), "-o", str(out), "--work-dir", str(work), "--dry-run"],
    )
    assert result.exit_code == 0, result.output
    for title in ("First Light", "Tom & Jerry", "Fate = Doom; #3"):
        assert title in result.output
    assert not out.exists() and not list(work.rglob("*.m4b*"))

    # a real run afterwards reuses the saved scan and placement, and verifies the result
    result = CliRunner().invoke(
        app,
        [
            "convert", str(mp3), str(epub), "-o", str(out),
            "--work-dir", str(work), "--no-readaloud",
        ],
    )  # fmt: skip
    assert result.exit_code == 0, result.output
    assert (out / "Fish & Chips A Test.m4b").exists()
    assert "chapters: 3" in result.output and "verify:" not in result.output


def test_convert_evicts_source_only_when_asked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:

    audio, epub = tmp_path / "a.mp3", tmp_path / "b.epub"
    audio.write_bytes(b"x")
    epub.write_bytes(b"x")
    final = tmp_path / "out" / "B.m4b"
    state: PipelineState = {"stage": "done", "final_output": str(final)}
    evicted: list[Path] = []
    monkeypatch.setattr(converter, "run_pipeline", lambda *_a, **_k: state)
    monkeypatch.setattr(converter.icloud, "ensure_local", lambda _p: None)

    def fake_evict(path: Path) -> bool:
        evicted.append(path)
        return True

    monkeypatch.setattr(converter.icloud, "evict", fake_evict)
    monkeypatch.setattr(converter, "report_verification", lambda *_a: None)

    converter.convert_book(audio, epub, tmp_path / "out", _options(tmp_path / "work"))
    assert evicted == []
    opts = _options(tmp_path / "work")
    opts.evict = True
    assert converter.convert_book(audio, epub, tmp_path / "out", opts) == final
    assert evicted == [audio]


def test_work_dir_honours_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import typer.main

    monkeypatch.setenv("MP3_EPUB_M4B_WORK", str(tmp_path))
    params = typer.main.get_command(app).commands["convert"].params  # type: ignore[attr-defined]
    work = next(p for p in params if p.name == "work_dir")
    assert work.envvar == "MP3_EPUB_M4B_WORK"


def test_pair_by_name_matches_identical_stems(tmp_path: Path) -> None:
    for name in ["Dune.mp3", "dune.EPUB", "Emma.m4a", "Emma.epub", "Solo.mp3", "Volume 1.mp3"]:
        (tmp_path / name).write_text("")
    pairs = pair_by_name(tmp_path, skip_audio={tmp_path / "Volume 1.mp3"})
    assert [(s, a.name, e.name) for s, a, e in pairs] == [
        ("Dune", "Dune.mp3", "dune.EPUB"),
        ("Emma", "Emma.m4a", "Emma.epub"),
    ]


def test_find_epub_matches_stem(tmp_path: Path) -> None:
    audio = tmp_path / "Dune.mp3"
    for name in ["Dune.mp3", "Dune II.epub", "Dune.EPUB"]:
        (tmp_path / name).write_text("")
    assert find_epub(audio).name == "Dune.EPUB"
    with pytest.raises(FileNotFoundError):
        find_epub(tmp_path / "Emma.mp3")


def _batch_folder(tmp_path: Path) -> Path:
    """A folder with one name-paired book (placeholder files, never converted)."""
    folder = tmp_path / "books"
    folder.mkdir()
    (folder / "Dune.mp3").write_text("")
    (folder / "Dune.epub").write_text("")
    return folder


@pytest.mark.parametrize("answer, converts", [("n\n", False), ("y\n", True)])
def test_batch_lists_pairs_and_asks_to_confirm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, answer: str, converts: bool
) -> None:
    converted: list[Path] = []
    monkeypatch.setattr("mp3_epub_m4b.cli._require_tools", lambda: None)
    monkeypatch.setattr(
        "mp3_epub_m4b.cli._process_book", lambda audio, *_a: converted.append(audio)
    )
    args = ["batch", str(_batch_folder(tmp_path)), "--work-dir", str(tmp_path / "w")]
    result = CliRunner().invoke(app, args, input=answer)
    assert "Dune.mp3" in result.output and "Convert these 1 book(s)?" in result.output
    assert bool(converted) is converts


def test_batch_yes_skips_the_question(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    converted: list[Path] = []
    monkeypatch.setattr("mp3_epub_m4b.cli._require_tools", lambda: None)
    monkeypatch.setattr(
        "mp3_epub_m4b.cli._process_book", lambda audio, *_a: converted.append(audio)
    )
    args = ["batch", str(_batch_folder(tmp_path)), "--work-dir", str(tmp_path / "w"), "-y"]
    result = CliRunner().invoke(app, args)
    assert "Convert these" not in result.output and len(converted) == 1
