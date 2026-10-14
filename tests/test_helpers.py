"""Unit tests for small pure helpers."""

from __future__ import annotations

import pytest

from mp3_epub_m4b.chapters import build_ffmetadata, ffmeta_escape
from mp3_epub_m4b.epub_extract import EpubMeta, _is_front_back_matter
from mp3_epub_m4b.pairing import pair_files
from mp3_epub_m4b.run import safe_filename, slugify
from mp3_epub_m4b.state import Segment


def test_slugify_and_safe_filename() -> None:
    assert slugify("  A/B: C  ") == "A_B_C"
    assert safe_filename('Fish & Chips: "A" Test/Vol 1') == "Fish & Chips A Test Vol 1"
    assert safe_filename("???") == "audiobook"


def test_ffmeta_escape() -> None:
    assert ffmeta_escape("a=b;c#d\\e") == r"a\=b\;c\#d\\e"
    assert ffmeta_escape("line1\nline2") == "line1\\\nline2"


def test_build_ffmetadata_escapes_everything() -> None:
    meta: EpubMeta = {
        "title": "T=1",
        "author": "A;B",
        "publisher": "",
        "year": "2020",
        "series": "S#",
        "series_index": "2",
    }
    segs: list[Segment] = [
        {"n": 0, "title": "One=1", "start": 0.0, "end": 1.2345, "duration": 1.2345}
    ]
    text = build_ffmetadata(meta, segs)
    assert r"title=T\=1" in text and r"artist=A\;B" in text and r"show=S\#" in text
    assert "START=0" in text and "END=1234" in text and r"title=One\=1" in text


@pytest.mark.parametrize(
    "heading,expected",
    [
        ("Cover", True),
        ("Title Page", True),
        ("Acknowledgments", True),
        ("About the Author", True),
        ("Also By Someone", True),
        ("Cover Fire", False),
        ("The Map Room", False),
        ("Preview of Danger", True),
        ("Contents of the Chest", False),
        ("", False),
    ],
)
def test_front_back_matter(heading: str, expected: bool) -> None:
    assert _is_front_back_matter(heading) is expected


def test_pair_files_patterns(tmp_path) -> None:
    for name in [
        "Volume 1.MP3",
        "Volume 2.m4a",
        "Volume 3.mp3",
        "x_01.epub",
        "x_02.EPUB",
        "y_09.epub",
    ]:
        (tmp_path / name).write_text("")
    audio, epubs = pair_files(tmp_path)
    assert sorted(audio) == [1, 2, 3] and sorted(epubs) == [1, 2, 9]
    audio, epubs = pair_files(tmp_path, r"Vol\w* (\d+)", r"y_(\d+)$")
    assert sorted(epubs) == [9]


def test_pair_files_default_patterns_broaden(tmp_path) -> None:
    for name in [
        "Vol. 1.mp3",
        "Vol2.m4a",
        "3 - Third Book.mp3",
        "a_01.epub",
        "b_2.epub",
        "3 - Third Book.epub",
        "Series 9 - Sequel.epub",  # a number mid-title is not a volume number
    ]:
        (tmp_path / name).write_text("")
    audio, epubs = pair_files(tmp_path)
    assert sorted(audio) == [1, 2, 3] and sorted(epubs) == [1, 2, 3]
