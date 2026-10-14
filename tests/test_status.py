"""Tests for progress records and status reports."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest
from rich.console import Console

from mp3_epub_m4b.status import (
    PROGRESS_FILE,
    ProgressWriter,
    fmt_duration,
    pid_alive,
    read_all_status,
    read_progress,
    read_status,
    render_overview,
    render_status,
)


class Clock:
    """A settable fake clock."""

    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def test_writer_throttles_but_always_writes_stage_changes_and_forced_updates(
    tmp_path: Path,
) -> None:
    clock = Clock()
    path = tmp_path / PROGRESS_FILE
    w = ProgressWriter(path, min_interval=1.0, clock=clock)
    w.update("align", done=0, total=40, unit="chapters", detail="starting")
    assert read_progress(path)["detail"] == "starting"  # type: ignore[index]
    clock.t += 0.2
    w.update("align", done=1, detail="too soon")  # throttled
    assert read_progress(path)["detail"] == "starting"  # type: ignore[index]
    clock.t += 1.0
    w.update("align", done=2)  # late enough; keeps the last detail
    rec = read_progress(path)
    assert rec is not None and rec["done"] == 2 and rec["detail"] == "too soon"
    clock.t += 0.1
    w.update("align", done=3, detail="forced", force=True)
    assert read_progress(path)["done"] == 3  # type: ignore[index]
    clock.t += 0.1
    w.update("assemble")  # a new stage writes at once and starts from scratch
    rec = read_progress(path)
    assert rec is not None
    assert rec["stage"] == "assemble" and rec["done"] is None and rec["detail"] == ""
    assert rec["started_at"] == clock.t and rec["pid"] == os.getpid()


def test_writer_records_a_speed_baseline_for_a_resumed_stage(tmp_path: Path) -> None:
    clock = Clock()
    w = ProgressWriter(tmp_path / PROGRESS_FILE, clock=clock)
    w.update("align", done=12, total=40, unit="chapters")  # resumed at chapter 12
    clock.t += 30
    w.update("align", done=14, force=True)
    rec = read_progress(tmp_path / PROGRESS_FILE)
    assert rec is not None and rec["base_done"] == 12 and rec["done"] == 14


def test_writer_survives_an_unwritable_location(tmp_path: Path) -> None:
    w = ProgressWriter(tmp_path / "missing" / PROGRESS_FILE)
    w.update("align", done=1)  # must not raise


@pytest.mark.parametrize(
    "text", ["", "{", "[]", '{"stage": "align"}', '{"stage":"a","updated_at":"x"}']
)
def test_read_progress_treats_damaged_files_as_missing(tmp_path: Path, text: str) -> None:
    path = tmp_path / PROGRESS_FILE
    path.write_text(text)
    assert read_progress(path) is None
    assert read_progress(tmp_path / "nope.json") is None


def test_pid_alive() -> None:
    assert pid_alive(os.getpid())
    assert not pid_alive(0)
    assert not pid_alive(-5)
    assert not pid_alive(2**22 + 12345)  # far above any real pid


def make_book(work: Path, *, stage: str = "align", placed: int = 2, **extra: Any) -> Path:
    """Write a checkpoint for a 4-chapter book with ``placed`` chapters located."""
    work.mkdir(parents=True, exist_ok=True)
    chapters = [{"n": i, "title": f"Chapter {i}", "word_count": 1000} for i in range(1, 5)]
    state: dict[str, Any] = {
        "stage": stage,
        "epub": {
            "meta": {
                "title": "The Book",
                "author": "A",
                "publisher": "",
                "year": "",
                "series": "",
                "series_index": "",
            },
            "cover": None,
            "chapters": chapters,
        },
        "mp3_duration": 4000.0,
        "align": {
            "boundaries": {str(i): 100.0 * i for i in range(1, placed + 1)},
            "next": placed + 1,
            "low_confidence": [2] if placed >= 2 else [],
            "fallback": [],
            "method": "transcribe",
        },
        **extra,
    }
    (work / "state.json").write_text(json.dumps(state))
    return work


def write_progress(work: Path, **kw: Any) -> None:
    rec = {
        "pid": os.getpid(),
        "stage": "align",
        "detail": "chapter 3: listening 200-400s",
        "done": 2,
        "total": 4,
        "unit": "chapters",
        "started_at": 1000.0,
        "base_done": 0,
        "updated_at": 1100.0,
        **kw,
    }
    (work / PROGRESS_FILE).write_text(json.dumps(rec))


def test_read_status_summarizes_a_running_alignment(tmp_path: Path) -> None:
    work = make_book(tmp_path / "book")
    write_progress(work)
    st = read_status(work, now=1110.0, alive=lambda pid: True)
    assert st is not None
    assert st.title == "The Book" and st.stage == "align" and st.method == "transcribe"
    assert st.stages == [
        ("extract", "done"),
        ("scan", "done"),
        ("align", "running"),
        ("assemble", "pending"),
        ("deliver", "pending"),
    ]
    assert (st.chapters_placed, st.chapters_total) == (2, 4)
    assert st.fraction == 0.5 and st.unit == "chapters"
    assert st.low_confidence == [2] and [p.n for p in st.recent] == [1, 2]
    assert st.recent[1].note == "low confidence"
    assert st.detail == "chapter 3: listening 200-400s"
    # 2 chapters in 100 s -> 2 more take about 100 s
    assert st.eta == pytest.approx(100.0)
    assert (
        st.running is True and st.idle == pytest.approx(10.0) and st.elapsed == pytest.approx(100.0)
    )
    assert st.warnings == []


def test_read_status_flags_a_dead_process_and_a_missing_record(tmp_path: Path) -> None:
    work = make_book(tmp_path / "book")
    assert "no progress record" in read_status(work, now=5.0).warnings[0]  # type: ignore[union-attr]
    write_progress(work)
    st = read_status(work, now=1500.0, alive=lambda pid: False)
    assert st is not None and st.running is False and st.idle == pytest.approx(400.0)
    assert read_status(tmp_path / "nowhere") is None
    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / "state.json").write_text("{")
    assert read_status(broken) is None


def test_read_status_warns_when_alive_but_silent_for_a_long_time(tmp_path: Path) -> None:
    work = make_book(tmp_path / "book")
    write_progress(work, updated_at=1100.0)
    st = read_status(work, now=1100.0 + 1200, alive=lambda pid: True)
    assert st is not None and any("silent" in w for w in st.warnings)


def test_read_status_ignores_a_record_from_another_stage(tmp_path: Path) -> None:
    work = make_book(tmp_path / "book")
    write_progress(work, stage="scan", detail="stale", done=9, total=10)
    st = read_status(work, now=1100.0, alive=lambda pid: True)
    assert (
        st is not None and st.detail == "" and st.eta is None and st.fraction == 0.5
    )  # chapters, not the record


def test_read_status_reports_audio_progress_for_the_encode(tmp_path: Path) -> None:
    work = make_book(tmp_path / "book", stage="assemble", placed=4)
    write_progress(
        work,
        stage="assemble",
        unit="audio",
        done=1000.0,
        total=4000.0,
        started_at=1000.0,
        detail="",
    )
    st = read_status(work, now=1100.0, alive=lambda pid: True)
    assert st is not None and st.fraction == 0.25 and st.unit == "audio"
    assert st.eta == pytest.approx(300.0)  # 1000 s of audio per 100 s -> 3000 s left = 300 s


def test_read_status_for_a_finished_book(tmp_path: Path) -> None:
    work = make_book(tmp_path / "book", stage="done", placed=4, final_output="/out/The Book.m4b")
    st = read_status(work, now=1.0, alive=lambda pid: False)
    assert st is not None and st.output == "/out/The Book.m4b"
    assert all(s == "done" for _n, s in st.stages) and st.running is None and st.warnings == []


def render(obj: Any) -> str:
    console = Console(record=True, width=100, force_terminal=False)
    console.print(obj)
    return console.export_text()


def test_render_status_shows_the_essentials(tmp_path: Path) -> None:
    work = make_book(tmp_path / "book")
    write_progress(work)
    st = read_status(work, now=1110.0, alive=lambda pid: True)
    assert st is not None
    text = render(render_status(st))
    for needle in (
        "The Book",
        "▶ align",
        "✓ extract",
        "50%",
        "2/4 chapters placed by transcript",
        "1 low confidence",
        "listening 200-400s",
        "about 1m 40s left",
        "running (pid",
    ):
        assert needle in text, needle


def test_render_status_for_stopped_and_finished_runs(tmp_path: Path) -> None:
    work = make_book(tmp_path / "a")
    write_progress(work)
    stopped = read_status(work, now=1200.0, alive=lambda pid: False)
    assert stopped is not None and "not running" in render(render_status(stopped))
    done = read_status(
        make_book(tmp_path / "b", stage="done", placed=4, final_output="/o/B.m4b"), now=1.0
    )
    assert done is not None and "Finished: /o/B.m4b" in render(render_status(done))


def test_read_all_status_orders_by_recent_activity(tmp_path: Path) -> None:
    old = make_book(tmp_path / "old")
    new = make_book(tmp_path / "new")
    os.utime(old / "state.json", (1, 1))
    (tmp_path / "stray").mkdir()
    (tmp_path / "file.txt").write_text("x")
    names = [s.work_dir.name for s in read_all_status(tmp_path)]
    assert names == ["new", "old"]
    text = render(render_overview(read_all_status(tmp_path)))
    assert "The Book" in text and "align" in text
    assert read_all_status(tmp_path / "missing") == []
    assert new.exists()


def test_fmt_duration() -> None:
    assert [fmt_duration(s) for s in (4, 59.6, 65, 3599, 3660, 7325)] == [
        "4s",
        "1m 00s",
        "1m 05s",
        "59m 59s",
        "1h 01m",
        "2h 02m",
    ]


def test_build_view_single_book_multiple_books_and_none(tmp_path: Path) -> None:
    from mp3_epub_m4b.status import build_view

    view, active = build_view([])
    assert "No conversions" in render(view) and active is False
    work = make_book(tmp_path / "a")
    write_progress(work)
    live = read_status(work, now=1100.0, alive=lambda pid: True)
    done = read_status(make_book(tmp_path / "b", stage="done", placed=4), now=1.0)
    assert live is not None and done is not None
    view, active = build_view([live])
    assert active is True and "▶ align" in render(view)
    view, active = build_view([done, live])
    text = render(view)
    assert active is True and "Conversions" in text and "▶ align" in text  # overview + live panel
    view, active = build_view([done])
    assert active is False


def test_status_command_by_audio_file_work_folder_and_default(tmp_path: Path) -> None:
    from typer.testing import CliRunner

    from mp3_epub_m4b.cli import app
    from mp3_epub_m4b.run import work_name

    root = tmp_path / "work"
    book = make_book(root / work_name("My Book"))
    write_progress(book, updated_at=1e12)  # "now" is far in the past: a stale but alive record
    runner = CliRunner()

    by_audio = runner.invoke(
        app, ["status", str(tmp_path / "My Book.mp3"), "--work-dir", str(root)]
    )
    assert by_audio.exit_code == 0, by_audio.output
    assert "The Book" in by_audio.output and "2/4 chapters placed" in by_audio.output

    by_folder = runner.invoke(app, ["status", str(book), "--work-dir", str(root)])
    assert by_folder.exit_code == 0 and "The Book" in by_folder.output

    everything = runner.invoke(app, ["status", "--work-dir", str(root)])
    assert everything.exit_code == 0 and "The Book" in everything.output

    missing = runner.invoke(app, ["status", str(tmp_path / "Nope.mp3"), "--work-dir", str(root)])
    assert missing.exit_code == 1 and "no conversion found" in missing.output

    empty = runner.invoke(app, ["status", "--work-dir", str(tmp_path / "empty")])
    assert empty.exit_code == 0 and "No conversions" in empty.output
