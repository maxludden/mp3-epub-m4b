"""Shared fixtures: a synthetic EPUB and a synthetic audiobook with known chapter gaps."""

from __future__ import annotations

import shutil
import subprocess
import zipfile
from collections.abc import Iterator
from pathlib import Path
from typing import NamedTuple

import pytest
from loguru import logger

needs_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe not installed",
)


class ChapterSpec(NamedTuple):
    """One synthetic chapter: its title and its length in seconds."""

    title: str
    seconds: float


# (title, seconds). The second chapter contains a 3.6 s pause part-way in (a
# "scene break") that must NOT be mistaken for a chapter boundary. The third
# title has characters that need escaping in ffmpeg metadata.
CHAPTERS = [
    ChapterSpec("First Light", 30.0),
    ChapterSpec("Tom & Jerry", 60.0),
    ChapterSpec("Fate = Doom; #3", 40.0),
]
WORDS_PER_SECOND = 10
GAP = 4.0  # chapter-transition silence
STING = 1.0  # short tone between the two gaps
COMPANION = 2.0  # companion silence after the sting
SCENE_AT = 10.0  # scene break position inside chapter 2
SCENE_GAP = 3.6


def expected_boundaries() -> list[float]:
    """Start time (s) of each chapter in the synthetic audio, taking each gap's midpoint."""
    starts = [0.0]
    t = 0.0
    for i, ch in enumerate(CHAPTERS[:-1]):
        t += ch.seconds + (SCENE_GAP if i == 1 else 0.0)
        starts.append(t + GAP / 2)
        t += GAP + STING + COMPANION
    return starts


def make_audio(path: Path) -> float:
    """Synthesize an mp3 of tones separated by silences; returns its duration (s)."""
    parts: list[str] = []  # lavfi sources
    for i, ch in enumerate(CHAPTERS):
        freq = 300 + 150 * i
        if i == 1:
            parts += [f"sine=f={freq}:d={SCENE_AT}", f"anullsrc=r=22050:cl=mono:d={SCENE_GAP}",
                      f"sine=f={freq}:d={ch.seconds - SCENE_AT}"]
        else:
            parts.append(f"sine=f={freq}:d={ch.seconds}")
        if i < len(CHAPTERS) - 1:
            parts += [f"anullsrc=r=22050:cl=mono:d={GAP}", f"sine=f=900:d={STING}",
                      f"anullsrc=r=22050:cl=mono:d={COMPANION}"]
    cmd = ["ffmpeg", "-y", "-nostdin", "-loglevel", "error"]
    for p in parts:
        cmd += ["-f", "lavfi", "-i", p]
    labels = "".join(f"[{k}:a]aformat=sample_rates=22050:channel_layouts=mono[a{k}];"
                     for k in range(len(parts)))
    concat_in = "".join(f"[a{k}]" for k in range(len(parts)))
    cmd += ["-filter_complex", f"{labels}{concat_in}concat=n={len(parts)}:v=0:a=1[out]",
            "-map", "[out]", "-b:a", "64k", str(path)]
    subprocess.run(cmd, check=True)
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True, text=True, check=True,
    )
    return float(out.stdout)


def make_epub(path: Path, with_cover: bool = True) -> None:
    """Write a small EPUB 3 with a nav in a subfolder, URL-encoded hrefs and an XML entity."""
    opf_items = ['<item id="nav" href="nav/nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>']
    spine = []
    nav_links = []
    files: dict[str, str | bytes] = {}
    for n, ch in enumerate(CHAPTERS, start=1):
        name = f"Chapter {n}.xhtml"  # the space is URL-encoded in the hrefs
        files[f"OEBPS/text/{name}"] = (
            "<html xmlns='http://www.w3.org/1999/xhtml'><body>"
            f"<h1>{ch.title.replace('&', '&amp;')}</h1>"
            f"<p>{'word ' * int(ch.seconds * WORDS_PER_SECOND)}</p></body></html>"
        )
        # attribute order varies on purpose (href before id)
        opf_items.append(
            f'<item href="text/Chapter%20{n}.xhtml" id="c{n}" media-type="application/xhtml+xml"/>'
        )
        spine.append(f'<itemref idref="c{n}"/>')
        title = ch.title.replace("&", "&amp;")
        nav_links.append(f'<li><a href="../text/Chapter%20{n}.xhtml">{n}. {title}</a></li>')
    cover_meta = ""
    if with_cover:
        cover = path.parent / "cover.jpg"
        subprocess.run(
            ["ffmpeg", "-y", "-nostdin", "-loglevel", "error", "-f", "lavfi",
             "-i", "color=c=red:s=64x64:d=1", "-frames:v", "1", str(cover)],
            check=True,
        )
        files["OEBPS/images/cover.jpg"] = cover.read_bytes()
        opf_items.append('<item id="cover-img" href="images/cover.jpg" media-type="image/jpeg" properties="cover-image"/>')
        cover_meta = ""
    files["OEBPS/nav/nav.xhtml"] = (
        "<html xmlns='http://www.w3.org/1999/xhtml' xmlns:epub='http://www.idpf.org/2007/ops'><body>"
        f"<nav epub:type='toc'><ol>{''.join(nav_links)}</ol></nav></body></html>"
    )
    files["OEBPS/content.opf"] = (
        "<?xml version='1.0' encoding='utf-8'?>"
        "<package xmlns='http://www.idpf.org/2007/opf' version='3.0'>"
        "<metadata xmlns:dc='http://purl.org/dc/elements/1.1/'>"
        "<dc:title>Fish &amp; Chips: A Test</dc:title>"
        "<dc:creator>Ann Author</dc:creator><dc:publisher>Pub</dc:publisher>"
        "<dc:date>2021-05-04</dc:date>"
        "<meta property='belongs-to-collection' id='c'>The Series</meta>"
        "<meta refines='#c' property='group-position'>2</meta>"
        f"{cover_meta}</metadata>"
        f"<manifest>{''.join(opf_items)}</manifest>"
        f"<spine>{''.join(spine)}</spine></package>"
    )
    files["META-INF/container.xml"] = (
        "<?xml version='1.0'?><container version='1.0' "
        "xmlns='urn:oasis:names:tc:opendocument:xmlns:container'><rootfiles>"
        "<rootfile full-path='OEBPS/content.opf' media-type='application/oebps-package+xml'/>"
        "</rootfiles></container>"
    )
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("mimetype", "application/epub+zip")
        for name, data in files.items():
            zf.writestr(name, data)


@pytest.fixture
def logs() -> Iterator[list[tuple[str, str]]]:
    """Capture the library's loguru output as ``(level, message)`` pairs."""
    captured: list[tuple[str, str]] = []
    logger.enable("audiobook_kit")
    handler = logger.add(
        lambda m: captured.append((m.record["level"].name.lower(), m.record["message"])),
        level="DEBUG",
        format="{message}",
    )
    yield captured
    logger.remove(handler)
    logger.disable("audiobook_kit")


@pytest.fixture(scope="session")
def synthetic_book(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path, float]:
    """Build the synthetic audio + EPUB once; returns ``(mp3, epub, duration)``."""
    d = tmp_path_factory.mktemp("book")
    mp3, epub = d / "Volume 1.mp3", d / "book_01.epub"
    duration = make_audio(mp3)
    make_epub(epub)
    return mp3, epub, duration
