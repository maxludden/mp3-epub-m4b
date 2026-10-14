"""
Generalized EPUB chapter/metadata extraction.

Works for any EPUB2 (toc.ncx) or EPUB3 (nav.xhtml) book, not just this
series. The package document is parsed as XML (namespaces, entities and
URL-encoded hrefs are handled properly). Chapter detection strategy:
    1. Preferred: TOC entries whose label matches "<number>. <title>" (the
        common pattern for numbered-chapter fiction, incl. this publisher).
    2. Fallback: every spine item NOT matching a front/back-matter title
        blocklist (title page, copyright, contents, acknowledgments, also-by,
        about the author, etc.), taken in spine order.

Always prints a summary (chapter count + first/last titles) so a human
can sanity-check before the rest of the pipeline runs on it.
"""

from __future__ import annotations

import posixpath
import re
import warnings
import zipfile
from collections.abc import Iterator
from pathlib import Path
from typing import Annotated, NamedTuple, NotRequired, TypedDict
from urllib.parse import unquote

import typer
from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning
from loguru import logger
from lxml import etree
from rich.progress import Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
from rich.table import Table

from mp3_epub_m4b.log import console, setup_logging


class EpubMeta(TypedDict):
    """Book metadata read from the OPF package document."""

    title: str
    author: str
    publisher: str
    year: str
    series: str
    series_index: str


class Chapter(TypedDict):
    """One detected chapter: its number, title and word count.

    ``opening`` is the first words a narrator reads, including a leading
    epigraph; ``body_opening`` is the same without the epigraph and is only
    present when there is one. Both are absent in checkpoints from older versions.
    """

    n: int
    title: str
    word_count: int
    opening: NotRequired[str]
    body_opening: NotRequired[str]


class EpubResult(TypedDict):
    """Return value of :func:`extract_epub`."""

    meta: EpubMeta
    cover: str | None
    chapters: list[Chapter]


class ManifestItem(NamedTuple):
    """One OPF manifest entry; ``href`` is URL-decoded and relative to the OPF directory."""

    href: str
    media_type: str
    properties: frozenset[str]


app = typer.Typer(
    help="Extract chapters, metadata and cover for an MP3/M4A audiobook from an EPUB to generate an M4B.",
    add_completion=False,
    rich_markup_mode="rich",
)

# Headings that mark front/back matter. A heading matches if it equals an
# EXACT entry or starts with a PREFIX entry (after normalising case and
# punctuation). Whole-heading matching keeps real chapters such as "Cover
# Fire" or "The Map Room" from being dropped.
FRONT_BACK_MATTER_EXACT = frozenset(
    {
        "cover",
        "title page",
        "titlepage",
        "copyright",
        "contents",
        "table of contents",
        "dedication",
        "afterword",
        "map",
        "maps",
        "glossary",
        "appendix",
        "ars arcanum",
        "preview",
        "discord",
        "newsletter",
        "groups",
    }
)
FRONT_BACK_MATTER_PREFIXES = (
    "also by",
    "acknowledg",
    "about the author",
    "epilogue note",
    "author's note",
    "a note from the author",
    "thank you for reading",
    "sneak peek",
    "preview of",
    "special preview",
    "table of contents",
    "copyright",
)

OPENING_WORDS = 40  # how much of a chapter's start to keep for transcript matching
LEAD_MIN_WORDS = 15  # shorter files are stubs ("PART THREE"), not narrated epigraphs
LEAD_MAX_WORDS = 200  # at or above this a file is a chapter in its own right

NUMBERED_RE = re.compile(r"^\s*(?:chapter\s+)?(\d+)\s*[\.\):\-–—]\s*(.+?)\s*$", re.I)

DC_NS = "http://purl.org/dc/elements/1.1/"
_XML_PARSER = etree.XMLParser(recover=True, resolve_entities=False, no_network=True)


def _normalize_heading(heading: str) -> str:
    """Lowercase a heading, unify apostrophes and collapse punctuation/whitespace."""
    h = heading.lower().replace("’", "'")
    h = re.sub(r"[^a-z0-9' ]+", " ", h)
    return " ".join(h.split())


def _is_front_back_matter(heading: str) -> bool:
    """Return whether ``heading`` looks like front/back matter (see the blocklist constants)."""
    h = _normalize_heading(heading)
    return h in FRONT_BACK_MATTER_EXACT or h.startswith(FRONT_BACK_MATTER_PREFIXES)


def _opening_text(path: Path) -> str:
    """The first ``OPENING_WORDS`` words of body text in an XHTML file (headings left out)."""
    soup = _soup(_read(path))
    body = soup.body or soup
    for h in body.find_all(["h1", "h2", "h3"]):
        h.decompose()
    return " ".join(body.get_text(" ", strip=True).split()[:OPENING_WORDS])


def _lead_in(base: Path, spine_hrefs: list[str], idx: int) -> str:
    """Opening text of a short epigraph file directly before spine item ``idx``.

    Many books split a chapter into a heading-less epigraph file followed by
    the numbered file. A narrator reads the epigraph first, so it is where the
    chapter starts in the audio. Returns ``""`` when the previous file is a
    stub, a full chapter, or front/back matter.
    """
    if idx < 1:
        return ""
    prev = base / spine_hrefs[idx - 1]
    if not prev.exists():
        return ""
    heading, words = _chapter_word_count(prev)
    if not LEAD_MIN_WORDS <= words < LEAD_MAX_WORDS or (heading and _is_front_back_matter(heading)):
        return ""
    return _opening_text(prev)


def _with_openings(chapter: Chapter, lead: str, body: str) -> Chapter:
    """Attach ``opening`` (and ``body_opening`` if ``lead`` is non-empty) to ``chapter``."""
    if body or lead:
        chapter["opening"] = f"{lead} {body}".strip() if lead else body
    if lead and body:
        chapter["body_opening"] = body
    return chapter


def _read(path: Path) -> str:
    """Read a UTF-8 text file, replacing undecodable bytes instead of raising."""
    return path.read_text(encoding="utf-8", errors="replace")


def _soup(markup: str) -> BeautifulSoup:
    """Parse XHTML leniently, silencing bs4's XML-as-HTML warning for this call only."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", XMLParsedAsHTMLWarning)
        return BeautifulSoup(markup, "lxml")


def _xml(path: Path) -> etree._Element:
    """Parse an XML file leniently (recovering from minor errors; no entity or network access)."""
    root = etree.fromstring(path.read_bytes(), _XML_PARSER)
    if root is None:
        raise ValueError(f"could not parse {path}")
    return root


def _local(el: etree._Element) -> str:
    """Local (namespace-free) tag name; empty for comments and processing instructions."""
    tag = el.tag
    return etree.QName(tag).localname if isinstance(tag, str) else ""


def _text(el: etree._Element) -> str:
    """All text under ``el`` with whitespace collapsed."""
    return " ".join("".join(str(t) for t in el.itertext()).split())


def _norm_href(base_dir: str, href: str) -> str:
    """Resolve ``href`` against ``base_dir``: drop the fragment, URL-decode, normalise."""
    return posixpath.normpath(posixpath.join(base_dir, unquote(href.split("#")[0])))


def extract_epub(epub_path: Path, work_dir: Path) -> EpubResult:
    """Unpack an EPUB and extract its metadata, cover image and chapter list.

    Chapters come from numbered TOC entries when present, otherwise from the
    spine minus front/back matter (see the module docstring).

    Args:
        epub_path: EPUB file to read.
        work_dir: Directory to extract the archive into; created if missing.

    Returns:
        The metadata, the cover image path (``None`` if none was found) and
        the detected chapters.

    Raises:
        FileNotFoundError: If the archive contains no ``.opf`` package file.
        zipfile.BadZipFile: If ``epub_path`` is not a valid zip archive.
    """
    work_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(epub_path) as zf:
        zf.extractall(work_dir)

    opf_path = _find_opf(work_dir)
    logger.debug(f"package document: {opf_path.relative_to(work_dir)}")
    root = _xml(opf_path)
    base = opf_path.parent

    manifest = _read_manifest(root)
    spine = [
        idref
        for el in root.iter()
        if _local(el) == "itemref" and (idref := el.get("idref")) is not None
    ]

    meta = _extract_metadata(root)
    cover_path = _find_cover(root, manifest, base)

    chapters = _extract_chapters_via_toc(base, manifest, spine, root)
    if chapters:
        logger.debug(f"{len(chapters)} chapters from the numbered table of contents")
    else:
        logger.warning("no numbered table of contents; falling back to spine order")
        chapters = _extract_chapters_via_blocklist(base, manifest, spine)
        logger.debug(f"{len(chapters)} chapters from the spine")

    if cover_path is None:
        logger.debug("no cover image found")
    return {
        "meta": meta,
        "cover": str(cover_path) if cover_path else None,
        "chapters": chapters,
    }


def _find_opf(work_dir: Path) -> Path:
    """Locate the OPF package file via ``META-INF/container.xml``, falling back to a glob.

    Raises:
        FileNotFoundError: If no ``.opf`` file exists under ``work_dir``.
    """
    container = work_dir / "META-INF" / "container.xml"
    if container.exists():
        for el in _xml(container).iter():
            if _local(el) == "rootfile" and (full := el.get("full-path")):
                candidate = work_dir / unquote(full)
                if candidate.exists():
                    return candidate
    candidates = sorted(work_dir.rglob("*.opf"))
    if not candidates:
        raise FileNotFoundError("no .opf found in epub")
    return candidates[0]


def _read_manifest(root: etree._Element) -> dict[str, ManifestItem]:
    """Map manifest item ids to their decoded href, media type and properties."""
    manifest: dict[str, ManifestItem] = {}
    for el in root.iter():
        if _local(el) != "item":
            continue
        item_id, href = el.get("id"), el.get("href")
        if item_id and href:
            manifest[item_id] = ManifestItem(
                _norm_href("", href),
                el.get("media-type", ""),
                frozenset((el.get("properties") or "").split()),
            )
    return manifest


def _extract_metadata(root: etree._Element) -> EpubMeta:
    """Pull title, author, publisher, year and series info out of the OPF.

    Series comes from calibre ``<meta>`` tags, or from the EPUB 3
    ``belongs-to-collection`` / ``group-position`` properties. Missing fields
    come back as empty strings; XML entities are decoded.
    """

    def dc(name: str) -> str:
        for el in root.iter(f"{{{DC_NS}}}{name}"):
            return _text(el)
        return ""

    calibre: dict[str, str] = {}
    props: dict[str, str] = {}
    for el in root.iter():
        if _local(el) != "meta":
            continue
        if (name := el.get("name")) and el.get("content") is not None:
            calibre.setdefault(name, el.get("content") or "")
        if (prop := el.get("property")) and prop not in props:
            props[prop] = _text(el)

    date = dc("date")
    return {
        "title": dc("title"),
        "author": dc("creator"),
        "publisher": dc("publisher"),
        "year": date[:4] if date[:4].isdigit() else "",
        "series": calibre.get("calibre:series") or props.get("belongs-to-collection", ""),
        "series_index": calibre.get("calibre:series_index") or props.get("group-position", ""),
    }


def _find_cover(root: etree._Element, manifest: dict[str, ManifestItem], base: Path) -> Path | None:
    """Find the cover image path, or ``None``.

    Tries, in order: the EPUB 3 ``cover-image`` manifest property, the EPUB 2
    ``<meta name="cover">`` id, then any manifest jpg/png whose id contains
    "cover".
    """
    candidates: list[str] = [i for i, it in manifest.items() if "cover-image" in it.properties]
    for el in root.iter():
        if _local(el) == "meta" and el.get("name") == "cover" and el.get("content"):
            candidates.append(el.get("content") or "")
    candidates += [
        i
        for i, it in manifest.items()
        if "cover" in i.lower() and re.search(r"\.(jpe?g|png)$", it.href, re.I)
    ]
    for item_id in candidates:
        if item_id in manifest:
            p = base / manifest[item_id].href
            if p.exists() and p.suffix.lower() in {".jpg", ".jpeg", ".png", ".gif", ".webp"}:
                return p
    return None


def _chapter_word_count(path: Path) -> tuple[str, int]:
    """Return ``(heading, word_count)`` for an XHTML file.

    The heading is the text of the first ``h1``/``h2``/``h3`` found (checked in
    that order), or ``""`` if there is none.
    """
    soup = _soup(_read(path))
    text = soup.get_text(" ", strip=True)
    heading = ""
    for tag in ["h1", "h2", "h3"]:
        h = soup.find(tag)
        if h:
            heading = h.get_text(" ", strip=True)
            break
    return heading, len(text.split())


def _toc_sources(
    root: etree._Element, manifest: dict[str, ManifestItem], base: Path
) -> Iterator[list[tuple[str, str]]]:
    """Yield TOC entry lists as ``(label, path-relative-to-OPF)``: EPUB 3 nav first, then NCX.

    Hrefs are resolved against the TOC file's own directory, so they compare
    equal to manifest paths.
    """
    nav_hrefs = [it.href for it in manifest.values() if "nav" in it.properties]
    nav_hrefs += [p.relative_to(base).as_posix() for p in sorted(base.rglob("nav.xhtml"))]
    for nav_href in dict.fromkeys(nav_hrefs):
        path = base / nav_href
        if not path.exists():
            continue
        soup = _soup(_read(path))
        toc_nav = soup.find("nav", attrs={"epub:type": "toc"}) or soup.find("nav")
        if toc_nav:
            nav_dir = posixpath.dirname(nav_href)
            yield [
                (a.get_text(" ", strip=True), _norm_href(nav_dir, str(a["href"])))
                for a in toc_nav.find_all("a", href=True)
            ]
        break

    ncx_hrefs = [it.href for it in manifest.values() if it.media_type == "application/x-dtbncx+xml"]
    ncx_hrefs += [p.relative_to(base).as_posix() for p in sorted(base.rglob("*.ncx"))]
    for ncx_href in dict.fromkeys(ncx_hrefs):
        path = base / ncx_href
        if not path.exists():
            continue
        ncx_dir = posixpath.dirname(ncx_href)
        entries: list[tuple[str, str]] = []
        for point in _xml(path).iter():
            if _local(point) != "navPoint":
                continue
            label = next((_text(c) for c in point if _local(c) == "navLabel"), "")
            src = next((c.get("src") for c in point if _local(c) == "content"), None)
            if src:
                entries.append((label, _norm_href(ncx_dir, src)))
        yield entries
        break


def _extract_chapters_via_toc(
    base: Path,
    manifest: dict[str, ManifestItem],
    spine: list[str],
    root: etree._Element,
) -> list[Chapter]:
    """Build the chapter list from numbered TOC entries (``nav.xhtml`` or ``toc.ncx``).

    Word counts span every spine file from a chapter's entry up to the next
    chapter's, stopping early at front/back-matter headings. Returns an empty
    list when no TOC has ``"<number>. <title>"`` entries.

    Args:
        base: Directory containing the OPF; manifest hrefs are relative to it.
        manifest: Manifest items by id.
        spine: Item ids in reading order.
        root: Parsed OPF document.
    """
    # spine gives reading order; a chapter's real text often spans multiple
    # spine files when the source HTML was auto-split (part0005_split_000,
    # _001, _002, ...). Resolve each TOC entry to its spine index, then sum
    # word counts across every spine file up to (not including) the next
    # chapter's spine index.
    spine_hrefs = [manifest[idref].href for idref in spine if idref in manifest]
    path_to_index = {href: i for i, href in enumerate(spine_hrefs)}
    by_name: dict[str, list[int]] = {}
    for i, href in enumerate(spine_hrefs):
        by_name.setdefault(posixpath.basename(href), []).append(i)

    deduped: list[tuple[int, str, int]] = []
    for entries in _toc_sources(root, manifest, base):
        numbered: list[tuple[int, str, int]] = []  # (n, title, spine_index)
        for label, href in entries:
            m = NUMBERED_RE.match(label)
            if not m:
                continue
            idx = path_to_index.get(href)
            if idx is None:  # tolerate a TOC written relative to a different root
                same_name = by_name.get(posixpath.basename(href), [])
                idx = same_name[0] if len(same_name) == 1 else None
            if idx is None:
                continue
            numbered.append((int(m.group(1)), m.group(2), idx))
        if not numbered:
            continue
        numbered.sort(key=lambda t: t[2])  # by spine position, not claimed number
        seen_n: set[int] = set()
        for n, title, idx in numbered:
            if n not in seen_n:
                seen_n.add(n)
                deduped.append((n, title, idx))
        break

    chapters: list[Chapter] = []
    for i, (n, title, start_idx) in enumerate(deduped):
        end_idx = deduped[i + 1][2] if i + 1 < len(deduped) else len(spine_hrefs)
        wc = 0
        for j, href in enumerate(spine_hrefs[start_idx:end_idx]):
            p = base / href
            if not p.exists():
                continue
            heading, w = _chapter_word_count(p)
            # guard against the last chapter silently absorbing trailing
            # back matter (thank-you page, afterword, ...) that has no
            # numbered TOC entry of its own to bound it
            if j > 0 and heading and _is_front_back_matter(heading):
                break
            wc += w
        body = next(
            (
                _opening_text(base / h)
                for h in spine_hrefs[start_idx:end_idx]
                if (base / h).exists() and _chapter_word_count(base / h)[1] >= LEAD_MIN_WORDS
            ),
            "",
        )
        # an epigraph file directly before the entry belongs to this chapter,
        # unless the previous entry starts there (then it is that chapter's body)
        own_lead = i > 0 and deduped[i - 1][2] < start_idx - 1
        lead = _lead_in(base, spine_hrefs, start_idx) if own_lead else ""
        chapters.append(_with_openings({"n": n, "title": title, "word_count": wc}, lead, body))

    chapters.sort(key=lambda c: c["n"])
    return chapters


def _extract_chapters_via_blocklist(
    base: Path, manifest: dict[str, ManifestItem], spine: list[str]
) -> list[Chapter]:
    """Fallback chapter detection: every spine file that isn't front/back matter.

    Skips the title page, blocklisted headings and files under 200 words, and
    numbers the rest sequentially from 1. Stops at the first blocklisted heading
    after a chapter, so trailing previews and ads are not counted.
    """
    hrefs = [manifest[idref].href for idref in spine if idref in manifest]
    chapters: list[Chapter] = []
    n = 0
    for idx, href in enumerate(hrefs):
        p = base / href
        if not p.exists() or p.name == "titlepage.xhtml":
            continue
        heading, wc = _chapter_word_count(p)
        if _is_front_back_matter(heading):
            if chapters:  # back matter after the story: whatever follows is a sample or ad
                break
            continue
        if wc < 200:  # too short to plausibly be real chapter content
            continue
        n += 1
        # never borrow a lead-in for the first chapter: what precedes it is
        # usually praise quotes or a dedication, which are not narrated
        lead = _lead_in(base, hrefs, idx) if chapters else ""
        chapters.append(
            _with_openings(
                {"n": n, "title": heading or f"Chapter {n}", "word_count": wc},
                lead,
                _opening_text(p),
            )
        )
    return chapters


@app.command()
def main(
    epub: Annotated[Path, typer.Argument(exists=True, dir_okay=False, help="EPUB file to read.")],
    work_dir: Annotated[
        Path, typer.Option(file_okay=False, help="Directory to extract the EPUB into.")
    ],
    out: Annotated[Path, typer.Option(dir_okay=False, help="JSON file to write the result to.")],
) -> None:
    """Extract an EPUB, write the result as JSON and print a summary."""
    import json

    setup_logging()
    with Progress(
        SpinnerColumn(),
        TextColumn("[bold]{task.description}"),
        TimeElapsedColumn(),
        console=console,
        transient=True,
    ) as progress:
        progress.add_task(f"extracting {epub.name}", total=None)
        result = extract_epub(epub, work_dir)
        out.write_text(json.dumps(result, indent=2, ensure_ascii=False))

    meta: EpubMeta = result["meta"]
    info = Table.grid(padding=(0, 2))
    info.add_row("[bold]title[/bold]", meta["title"])
    info.add_row("[bold]author[/bold]", meta["author"])
    info.add_row("[bold]series[/bold]", f"{meta['series']} #{meta['series_index']}")
    info.add_row("[bold]cover[/bold]", str(result["cover"]))
    info.add_row("[bold]chapters[/bold]", str(len(result["chapters"])))
    console.print(info)

    chapters = result["chapters"]
    table = Table(show_edge=False)
    table.add_column("#", justify="right")
    table.add_column("Title")
    table.add_column("Words", justify="right")
    for c in chapters[:3]:
        table.add_row(str(c["n"]), c["title"], str(c["word_count"]))
    if len(chapters) > 6:
        table.add_row("...", "...", "...")
    for c in chapters[max(3, len(chapters) - 3) :]:
        table.add_row(str(c["n"]), c["title"], str(c["word_count"]))
    console.print(table)
    logger.success(f"wrote {out}")


if __name__ == "__main__":
    app()
