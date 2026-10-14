"""Match audio files to their EPUBs: by number (``Volume 3`` / ``..._03``) or identical name."""

from __future__ import annotations

import re
from collections.abc import Container
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
    for f in sorted(folder.iterdir()):
        suffix = f.suffix.lower()
        if suffix in AUDIO_SUFFIXES and (m := audio_re.fullmatch(f.stem)):
            add_numbered(audio, _number(m), f)
        elif suffix == ".epub" and (m := epub_re.search(f.stem)):
            add_numbered(epubs, _number(m), f)
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
    return [(audio[k].stem, audio[k], epubs[k]) for k in sorted(audio.keys() & epubs.keys())]
