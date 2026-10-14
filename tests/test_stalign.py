"""Alignment wrapper: output naming, command building, and input validation."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from mp3_epub_m4b import align as align_mod
from mp3_epub_m4b.align import (
    AlignError,
    AlignOptions,
    align_book,
    build_command,
    default_output,
)
from mp3_epub_m4b.cli import app


def test_default_output_sits_next_to_epub() -> None:
    assert default_output(Path("/a/Book One.epub")) == Path("/a/Book One (aligned).epub")


def test_build_command_ctc_defaults() -> None:
    cmd = build_command(
        Path("align"), Path("/audio"), Path("b.epub"), Path("o.epub"), AlignOptions()
    )
    assert "--ctc" in cmd and "--no-progress" in cmd
    assert "--engine" not in cmd and "--device" not in cmd
    assert cmd[cmd.index("-g") + 1] == "sentence"


def test_build_command_engine_skips_ctc_and_device() -> None:
    opts = AlignOptions(
        engine="whisper.cpp",
        model="small",
        device="cpu",
        progress=True,
        codec="aac",
        extra=["--foo"],
    )
    cmd = build_command(Path("align"), Path("/audio"), Path("b.epub"), Path("o.epub"), opts)
    assert "--ctc" not in cmd and "--device" not in cmd and "--no-progress" not in cmd
    assert cmd[cmd.index("--engine") + 1] == "whisper.cpp"
    assert cmd[cmd.index("--model") + 1] == "small"
    assert cmd[cmd.index("--codec") + 1] == "aac"
    assert cmd[-1] == "--foo"


def test_build_command_ctc_device_and_model() -> None:
    opts = AlignOptions(device="mlx", model="mms")
    cmd = build_command(Path("align"), Path("/audio"), Path("b.epub"), Path("o.epub"), opts)
    assert cmd[cmd.index("--device") + 1] == "mlx"
    assert cmd[cmd.index("--model") + 1] == "mms"


def test_align_book_refuses_to_overwrite(tmp_path: Path) -> None:
    audio, epub, out = tmp_path / "a.m4b", tmp_path / "b.epub", tmp_path / "o.epub"
    for f in (audio, epub, out):
        f.write_bytes(b"x")
    with pytest.raises(AlignError, match="refusing to overwrite"):
        align_book(audio, epub, out)


def test_align_book_rejects_bad_output_suffix(tmp_path: Path) -> None:
    audio, epub = tmp_path / "a.m4b", tmp_path / "b.epub"
    audio.write_bytes(b"x")
    epub.write_bytes(b"x")
    with pytest.raises(AlignError, match="must end in .epub"):
        align_book(audio, epub, tmp_path / "o.zip")


def test_align_book_missing_audio(tmp_path: Path) -> None:
    epub = tmp_path / "b.epub"
    epub.write_bytes(b"x")
    with pytest.raises(AlignError, match="audiobook not found"):
        align_book(tmp_path / "nope.m4b", epub)


def test_find_node_rejects_old_node(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(align_mod, "node_major", lambda _p: 20)
    with pytest.raises(AlignError, match="Node >= 24"):
        align_mod.find_node()


@pytest.mark.parametrize("name", ["stalign", "align"])
def test_cli_stalign_and_alias_report_error_and_exit_1(tmp_path: Path, name: str) -> None:
    audio, epub, out = tmp_path / "a.m4b", tmp_path / "b.epub", tmp_path / "o.epub"
    for f in (audio, epub, out):
        f.write_bytes(b"x")
    result = CliRunner().invoke(app, [name, str(audio), str(epub), str(out)])
    assert result.exit_code == 1


def test_align_alias_is_hidden_from_help() -> None:
    out = CliRunner().invoke(app, ["--help"]).output
    assert "stalign" in out
    assert " align " not in out


def test_ensure_installed_writes_valid_package_json(tmp_path: Path, monkeypatch) -> None:
    """The tool dir is dot-prefixed, so package.json must not come from `npm init`."""
    import json

    calls: list[list[str]] = []
    monkeypatch.setattr(align_mod.subprocess, "run", lambda cmd, **kw: calls.append(cmd))
    tool_dir = tmp_path / ".mp3-epub-m4b-align"
    align_mod.ensure_installed(Path("/usr/bin/node"), tool_dir)
    assert json.loads((tool_dir / "package.json").read_text())["name"] == "mp3-epub-m4b-align"
    assert len(calls) == 1 and calls[0][1] == "install"
