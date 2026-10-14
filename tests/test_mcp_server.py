"""Tests for the MCP server tools (called directly; the decorator returns the function)."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from mp3_epub_m4b import mcp_server
from mp3_epub_m4b.mcp_server import (
    conversion_status,
    find_pairs,
    mcp,
    start_conversion,
    summarize_segments,
    summarize_state,
    verify_m4b,
)
from mp3_epub_m4b.state import PipelineState, Segment


@pytest.fixture(autouse=True)
def work_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the scratch root at a temp dir so tests never touch ~/.mp3-epub-m4b-work."""
    root = tmp_path / "work"
    monkeypatch.setenv("MP3_EPUB_M4B_WORK", str(root))
    return root


@pytest.fixture
def book(tmp_path: Path) -> tuple[Path, Path]:
    """An (empty) audio file with an identically named EPUB."""
    audio, epub = tmp_path / "Book.mp3", tmp_path / "Book.epub"
    audio.write_bytes(b"")
    epub.write_bytes(b"")
    return audio, epub


def test_registered_tools() -> None:
    names = {t.name for t in asyncio.run(mcp.list_tools())}
    assert names == {"find_pairs", "start_conversion", "conversion_status", "verify_m4b"}


def test_find_pairs(tmp_path: Path) -> None:
    for name in ["Volume 1.mp3", "Series_01.epub", "Volume 2.mp3", "Solo.mp3", "Solo.epub"]:
        (tmp_path / name).write_text("")
    result = find_pairs(str(tmp_path))
    assert [p["label"] for p in result["pairs"]] == ["Book 1", "Solo"]
    assert [Path(p).name for p in result["audio_without_epub"]] == ["Volume 2.mp3"]
    assert result["epub_without_audio"] == []


def test_find_pairs_rejects_bad_input(tmp_path: Path) -> None:
    with pytest.raises(ToolError, match="not found"):
        find_pairs(str(tmp_path / "nope"))
    with pytest.raises(ToolError, match="invalid pattern"):
        find_pairs(str(tmp_path), audio_pattern="(")


def test_start_conversion_spawns_detached_cli(
    book: tuple[Path, Path], work_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    audio, epub = book
    calls: list[tuple[list[str], dict[str, Any]]] = []

    class FakeProc:
        pid = 4242

    def fake_popen(cmd: list[str], **kwargs: Any) -> FakeProc:
        calls.append((cmd, kwargs))
        return FakeProc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    monkeypatch.setattr(mcp_server, "low_space", lambda *_: [])
    result = start_conversion(str(audio), dry_run=True, fresh=True)

    cmd, kwargs = calls[0]
    assert cmd[1:4] == ["-m", "mp3_epub_m4b.cli", "convert"]
    assert str(audio.resolve()) in cmd and str(epub.resolve()) in cmd
    assert {"--dry-run", "--fresh"} <= set(cmd)
    assert kwargs["stdin"] == subprocess.DEVNULL and kwargs["start_new_session"] is True
    assert result["pid"] == 4242
    assert Path(result["work_dir"]) == work_root / "Book"


def test_start_conversion_refuses_duplicate_and_low_space(
    book: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    audio, epub = book
    record, _ = mcp_server._job_files(epub)
    record.parent.mkdir(parents=True)
    record.write_text(json.dumps({"pid": os.getpid()}))  # this process is certainly alive
    with pytest.raises(ToolError, match="already converting"):
        start_conversion(str(audio))
    record.write_text(json.dumps({"pid": 2**22 + 12345}))  # not alive: allowed to proceed
    monkeypatch.setattr(mcp_server, "low_space", lambda *_: [("work", 1.0)])
    with pytest.raises(ToolError, match="only 1GB free"):
        start_conversion(str(audio))


def test_start_conversion_needs_an_epub(tmp_path: Path) -> None:
    audio = tmp_path / "Lonely.mp3"
    audio.write_bytes(b"")
    with pytest.raises(ToolError, match="no .epub"):
        start_conversion(str(audio))


def test_conversion_status_before_and_after_checkpoint(
    book: tuple[Path, Path], work_root: Path
) -> None:
    audio, epub = book
    assert conversion_status(str(audio))["stage"] is None
    assert conversion_status(str(epub))["running"] is False

    work = work_root / "Book"
    work.mkdir(parents=True)
    segs: list[Segment] = [
        {"n": 1, "title": "One", "start": 0.0, "end": 10.0, "duration": 10.0},
        {"n": 2, "title": "Two", "start": 10.0, "end": 25.0, "duration": 15.0},
    ]
    state: PipelineState = {"stage": "deliver", "segments": segs}
    (work / "state.json").write_text(json.dumps(state))
    status = conversion_status(str(audio))
    assert status["stage"] == "deliver" and status["chapters"]["chapter_count"] == 2


def test_summarize_state_done() -> None:
    summary = summarize_state({"stage": "done", "final_output": "/x/Book.m4b"})
    assert summary["final_output"] == "/x/Book.m4b" and summary["stage_number"] == "done"


def test_verify_m4b_reports_probe_errors(tmp_path: Path) -> None:
    junk = tmp_path / "x.m4b"
    junk.write_bytes(b"not media")
    with pytest.raises(ToolError):
        verify_m4b(str(junk))


def test_tools_write_nothing_to_stdout(
    tmp_path: Path, book: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """stdout is the JSON-RPC channel under stdio, so tools must stay silent on it."""
    find_pairs(str(tmp_path))
    conversion_status(str(book[0]))
    assert capsys.readouterr().out == ""


def test_server_speaks_clean_json_over_stdio() -> None:
    """Smoke test: initialize + tools/list against the real server process."""
    msgs = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "t", "version": "0"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    ]  # fmt: skip
    # Keep stdin open until the tools/list reply arrives: the server exits on EOF, and
    # closing early races it against its own response.
    proc = subprocess.Popen(
        [sys.executable, "-m", "mp3_epub_m4b.mcp_server"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
    )  # fmt: skip
    assert proc.stdin is not None and proc.stdout is not None
    try:
        proc.stdin.write("".join(json.dumps(m) + "\n" for m in msgs))
        proc.stdin.flush()
        replies = []
        for line in proc.stdout:  # every line must be JSON
            replies.append(json.loads(line))
            if replies[-1].get("id") == 2:
                break
    finally:
        proc.kill()
        proc.wait()
    tools = next(r for r in replies if r.get("id") == 2)["result"]["tools"]
    assert {t["name"] for t in tools} >= {"find_pairs", "start_conversion"}


def test_summarize_segments_edges_and_detail() -> None:
    segs: list[Segment] = [
        {"n": i, "title": f"Ch {i}", "start": i * 10.0, "end": i * 10.0 + 10, "duration": 10.0}
        for i in range(1, 21)
    ]
    short = summarize_segments(segs)
    assert [c["n"] for c in short["first_chapters"]] == [1, 2, 3]
    assert [c["n"] for c in short["last_chapters"]] == [18, 19, 20]
    assert "chapters" not in short
    full = summarize_segments(segs, detail=True)
    assert len(full["chapters"]) == 20 and "first_chapters" not in full
    assert "chapters" in summarize_segments(segs[:4])  # small books are just listed
