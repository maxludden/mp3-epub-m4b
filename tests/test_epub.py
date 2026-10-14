"""EPUB extraction tests, including URL-encoded hrefs, entities and EPUB 2 NCX."""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from mp3_epub_m4b.epub_extract import extract_epub

from .conftest import CHAPTERS, WORDS_PER_SECOND, make_epub


@pytest.mark.ffmpeg
def test_extract_epub3(tmp_path: Path) -> None:
    epub = tmp_path / "b.epub"
    make_epub(epub)
    r = extract_epub(epub, tmp_path / "x")
    assert r["meta"] == {
        "title": "Fish & Chips: A Test",
        "author": "Ann Author",
        "publisher": "Pub",
        "year": "2021",
        "series": "The Series",
        "series_index": "2",
    }
    assert r["cover"] is not None and Path(r["cover"]).exists()
    assert [(c["n"], c["title"]) for c in r["chapters"]] == [
        (i, ch.title) for i, ch in enumerate(CHAPTERS, start=1)
    ]
    # word counts include the heading words
    assert abs(r["chapters"][0]["word_count"] - CHAPTERS[0].seconds * WORDS_PER_SECOND) < 10


def test_extract_epub2_ncx_and_blocklist_fallback(tmp_path: Path) -> None:
    def build(path: Path, ncx: bool) -> None:
        body = " ".join(["word"] * 300)
        files = {
            "META-INF/container.xml": "<container><rootfiles><rootfile full-path='c.opf'/></rootfiles></container>",
            "c.opf": (
                "<package xmlns='http://www.idpf.org/2007/opf'><metadata xmlns:dc='http://purl.org/dc/elements/1.1/'>"
                "<dc:title>T</dc:title></metadata><manifest>"
                "<item id='ncx' href='toc.ncx' media-type='application/x-dtbncx+xml'/>"
                "<item id='t' href='title.xhtml' media-type='application/xhtml+xml'/>"
                "<item id='a' href='a.xhtml' media-type='application/xhtml+xml'/>"
                "<item id='b' href='b.xhtml' media-type='application/xhtml+xml'/>"
                "</manifest><spine><itemref idref='t'/><itemref idref='a'/><itemref idref='b'/></spine></package>"
            ),
            "title.xhtml": "<html><body><h1>Title Page</h1><p>x</p></body></html>",
            "a.xhtml": f"<html><body><h1>Cover Fire</h1><p>{body}</p></body></html>",
            "b.xhtml": f"<html><body><h1>The Map Room</h1><p>{body}</p></body></html>",
        }
        if ncx:
            files["toc.ncx"] = (
                "<ncx xmlns='http://www.daisy.org/z3986/2005/ncx/'><navMap>"
                "<navPoint id='1'><navLabel><text>1. Cover Fire</text></navLabel><content src='a.xhtml'/>"
                "<navPoint id='2'><navLabel><text>2. The Map Room</text></navLabel><content src='b.xhtml#top'/></navPoint>"
                "</navPoint></navMap></ncx>"
            )
        else:
            files["toc.ncx"] = "<ncx xmlns='http://www.daisy.org/z3986/2005/ncx/'><navMap/></ncx>"
        with zipfile.ZipFile(path, "w") as zf:
            for name, data in files.items():
                zf.writestr(name, data)

    build(tmp_path / "ncx.epub", ncx=True)
    r = extract_epub(tmp_path / "ncx.epub", tmp_path / "w1")
    assert [(c["n"], c["title"]) for c in r["chapters"]] == [(1, "Cover Fire"), (2, "The Map Room")]

    build(tmp_path / "plain.epub", ncx=False)  # no numbered TOC -> blocklist fallback
    r = extract_epub(tmp_path / "plain.epub", tmp_path / "w2")
    assert [c["title"] for c in r["chapters"]] == ["Cover Fire", "The Map Room"]


def test_spine_fallback_attaches_epigraphs_and_stops_at_back_matter(tmp_path: Path) -> None:
    """A short file before a chapter is its epigraph; a preview after back matter is not a chapter."""
    body = " ".join(f"b{i}" for i in range(300))
    epigraph = "If men read these words let them know that power is a heavy burden to carry"
    pages = {
        "praise": "<p>" + " ".join(["praise"] * 40) + "</p>",  # short, but before chapter 1
        "one": f"<h1>One</h1><p>FIRST chapter {body}</p>",
        "epi": f"<p>{epigraph}</p>",
        "two": f"<h1>Two</h1><p>SECOND chapter {body}</p>",
        "arcanum": f"<h1>Ars Arcanum</h1><p>{body}</p>",
        "sample": f"<p>SAMPLE of the next book {body}</p>",  # no heading to blocklist
    }
    files = {
        "META-INF/container.xml": "<container><rootfiles><rootfile full-path='c.opf'/></rootfiles></container>",
        "c.opf": (
            "<package xmlns='http://www.idpf.org/2007/opf'><metadata xmlns:dc='http://purl.org/dc/elements/1.1/'>"
            "<dc:title>T</dc:title></metadata><manifest>"
            + "".join(
                f"<item id='{k}' href='{k}.xhtml' media-type='application/xhtml+xml'/>"
                for k in pages
            )
            + "</manifest><spine>"
            + "".join(f"<itemref idref='{k}'/>" for k in pages)
            + "</spine></package>"
        ),
        **{f"{k}.xhtml": f"<html><body>{v}</body></html>" for k, v in pages.items()},
    }
    epub = tmp_path / "b.epub"
    with zipfile.ZipFile(epub, "w") as zf:
        for name, data in files.items():
            zf.writestr(name, data)

    chapters = extract_epub(epub, tmp_path / "w")["chapters"]
    assert [c["title"] for c in chapters] == ["One", "Two"]  # sample after Ars Arcanum is dropped
    assert chapters[0]["opening"].startswith("FIRST chapter")
    assert "body_opening" not in chapters[0]  # never borrow the praise page for chapter 1
    assert chapters[1]["opening"].startswith("If men read these words")
    assert chapters[1]["body_opening"].startswith("SECOND chapter")
