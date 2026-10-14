"""Match audio files to their EPUBs: by number (``Volume 3`` / ``..._03``) or identical name."""

from __future__ import annotations

import re
from collections.abc import Container
from dataclasses import dataclass, field
from pathlib import Path

from loguru import logger

from mp3_epub_m4b.options import AUDIO_SUFFIXES, DEFAULT_AUDIO_PATTERN, DEFAULT_EPUB_PATTERN


def find_epub(audio: Path) -> Path:
    """Find the EPUB named like ``audio`` (same stem, ``.epub``) beside it.

    Raises:
        FileNotFoundError: If no such file exists.
    """
    for f in sorted(audio.parent.iterdir()):
        if f.is_file() and f.suffix.lower() == ".epub" and f.stem == audio.stem:
            return f
    raise FileNotFoundError(f"no .epub named '{audio.stem}' beside {audio.name}")


def add_numbered(index: dict[int, Path], n: int, path: Path) -> None:
    """Record ``path`` as number ``n``, keeping the first file and warning about duplicates."""
    if n in index:
        logger.warning(f"{path.name} duplicates number {n} ({index[n].name}); ignoring it")
    else:
        index[n] = path


def _number(m: re.Match[str]) -> int:
    """The number captured by whichever alternative of the pattern matched."""
    return int(next(g for g in m.groups() if g is not None))


def pair_files(
    folder: Path,
    audio_pattern: str = DEFAULT_AUDIO_PATTERN,
    epub_pattern: str = DEFAULT_EPUB_PATTERN,
) -> tuple[dict[int, Path], dict[int, Path]]:
    """Index a folder's audio and EPUB files by number.

    By default audio files are named ``Volume N`` and EPUBs end in ``_N``
    before the extension. Extensions are matched case-insensitively.

    Args:
        folder: Directory to scan (not recursive).
        audio_pattern: Regex capturing the number (alternatives may each
            have their own group), fully matched against an
            audio file's stem.
        epub_pattern: Regex capturing the number, searched in an EPUB's stem.

    Returns:
        ``(audio, epubs)``, each mapping number to path.
    """
    audio_re = re.compile(audio_pattern)
    epub_re = re.compile(epub_pattern)
    audio: dict[int, Path] = {}
    epubs: dict[int, Path] = {}
    logger.debug(f"pairing files in {folder} (audio /{audio_pattern}/, epub /{epub_pattern}/)")
    for f in sorted(folder.iterdir()):
        suffix = f.suffix.lower()
        if suffix in AUDIO_SUFFIXES and (m := audio_re.fullmatch(f.stem)):
            add_numbered(audio, _number(m), f)
        elif suffix == ".epub" and (m := epub_re.search(f.stem)):
            add_numbered(epubs, _number(m), f)
    logger.debug(f"numbered: {len(audio)} audio file(s), {len(epubs)} epub(s)")
    return audio, epubs


def pair_by_name(
    folder: Path, skip_audio: Container[Path] = (), skip_epubs: Container[Path] = ()
) -> list[tuple[str, Path, Path]]:
    """Pair audio files with identically named EPUBs (same stem, any case) in a folder.

    Args:
        folder: Directory to scan (not recursive).
        skip_audio: Audio files already paired by number; ignored here.
        skip_epubs: EPUBs already paired by number; ignored here.

    Returns:
        ``(stem, audio, epub)`` tuples, sorted by stem. If several audio files
        share a stem (``x.mp3`` and ``x.m4a``) the first in sorted order wins.
    """
    audio: dict[str, Path] = {}
    epubs: dict[str, Path] = {}
    for f in sorted(folder.iterdir()):
        suffix, key = f.suffix.lower(), f.stem.casefold()
        if not f.is_file():
            continue
        if suffix in AUDIO_SUFFIXES and f not in skip_audio:
            audio.setdefault(key, f)
        elif suffix == ".epub" and f not in skip_epubs:
            epubs.setdefault(key, f)
    pairs = [(audio[k].stem, audio[k], epubs[k]) for k in sorted(audio.keys() & epubs.keys())]
    logger.debug(f"matched {len(pairs)} identically named pair(s) in {folder}")
    return pairs


@dataclass
class FolderPairs:
    """Everything :func:`pair_folder` found in a folder.

    Attributes:
        jobs: ``(label, audio, epub)`` for every convertible pair, numbered pairs first.
        unmatched_audio: Numbered audio files with no EPUB of the same number, by number.
        unmatched_epubs: Numbered EPUBs with no audio file of the same number, by number.
    """

    jobs: list[tuple[str, Path, Path]] = field(default_factory=list)
    unmatched_audio: dict[int, Path] = field(default_factory=dict)
    unmatched_epubs: dict[int, Path] = field(default_factory=dict)


def pair_folder(
    folder: Path,
    audio_pattern: str = DEFAULT_AUDIO_PATTERN,
    epub_pattern: str = DEFAULT_EPUB_PATTERN,
) -> FolderPairs:
    """Pair a folder's audio with its EPUBs: by number first, then by identical name.

    Shared by the ``batch`` command and the MCP server.

    Raises:
        re.error: If either pattern is not a valid regex.
    """
    audio, epubs = pair_files(folder, audio_pattern, epub_pattern)
    result = FolderPairs(
        unmatched_audio={n: audio[n] for n in sorted(audio.keys() - epubs.keys())},
        unmatched_epubs={n: epubs[n] for n in sorted(epubs.keys() - audio.keys())},
    )
    result.jobs = [(f"Book {n}", audio[n], epubs[n]) for n in sorted(audio.keys() & epubs.keys())]
    paired = {a for _, a, _ in result.jobs}, {e for _, _, e in result.jobs}
    result.jobs += [(stem, a, e) for stem, a, e in pair_by_name(folder, *paired)]
    return result
