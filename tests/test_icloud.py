"""Tests for the iCloud helpers, with ``brctl`` and the clock faked out."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from mp3_epub_m4b import icloud


def test_regular_file_is_local(tmp_path: Path) -> None:
    f = tmp_path / "a.bin"
    f.write_bytes(b"x" * 5000)
    assert icloud.is_local(f)
    icloud.ensure_local(f)  # no brctl needed


def test_in_icloud_drive(tmp_path: Path) -> None:
    inside = tmp_path / "Library" / "Mobile Documents" / "com~apple~CloudDocs" / "a.mp3"
    assert icloud.in_icloud_drive(inside)
    assert not icloud.in_icloud_drive(tmp_path / "a.mp3")


def test_ensure_local_waits_for_download(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    f = tmp_path / "a.mp3"
    f.write_bytes(b"x")
    states = iter([False, False, True])
    calls: list[list[str]] = []
    monkeypatch.setattr(icloud, "is_local", lambda _p: next(states))
    monkeypatch.setattr(icloud, "_brctl", lambda: "brctl")
    monkeypatch.setattr(icloud.subprocess, "run", lambda cmd, **_k: calls.append(cmd))
    monkeypatch.setattr(icloud.time, "sleep", lambda _s: None)
    icloud.ensure_local(f)
    assert calls == [["brctl", "download", str(f)]]


def test_ensure_local_times_out(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    f = tmp_path / "a.mp3"
    f.write_bytes(b"x")
    monkeypatch.setattr(icloud, "is_local", lambda _p: False)
    monkeypatch.setattr(icloud, "_brctl", lambda: "brctl")
    monkeypatch.setattr(icloud.subprocess, "run", lambda *_a, **_k: None)
    monkeypatch.setattr(icloud.time, "sleep", lambda _s: None)
    with pytest.raises(TimeoutError):
        icloud.ensure_local(f, timeout=0.0)


def test_ensure_local_without_brctl_is_a_no_op(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    f = tmp_path / "a.mp3"
    f.write_bytes(b"x")
    monkeypatch.setattr(icloud, "is_local", lambda _p: False)
    monkeypatch.setattr(icloud, "_brctl", lambda: None)
    icloud.ensure_local(f)


def test_evict_only_inside_icloud_drive(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    class Done:
        returncode = 0

    monkeypatch.setattr(icloud, "_brctl", lambda: "brctl")

    def fake_run(cmd: list[str], **_k: object) -> Done:
        calls.append(cmd)
        return Done()

    monkeypatch.setattr(icloud.subprocess, "run", fake_run)
    outside = tmp_path / "a.mp3"
    outside.write_bytes(b"x")
    assert icloud.evict(outside) is False and not calls
    inside = tmp_path / "Mobile Documents" / "a.mp3"
    inside.parent.mkdir()
    inside.write_bytes(b"x")
    assert icloud.evict(inside) is True and calls == [["brctl", "evict", str(inside)]]
    assert os.path.exists(inside)
