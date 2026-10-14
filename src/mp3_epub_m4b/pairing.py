"""Match audio files to their EPUBs by number (``Volume 3`` / ``..._03``)."""

from __future__ import annotations

import re
from pathlib import Path

from loguru import logger

from mp3_epub_m4b.options import AUDIO_SUFFIXES, DEFAULT_AUDIO_PATTERN, DEFAULT_EPUB_PATTERN


def add_numbered(index: dict[int, Path], n: int, path: Path) -> None:
    """Record ``path`` as number ``n``, keeping the first file and warning about duplicates."""
    if n in index:
        logger.warning(f"{path.name} duplicates number {n} ({index[n].name}); ignoring it")
    else:
        index[n] = path


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
        audio_pattern: Regex with one number group, fully matched against an
            audio file's stem.
        epub_pattern: Regex with one number group, searched in an EPUB's stem.

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
            add_numbered(audio, int(m.group(1)), f)
        elif suffix == ".epub" and (m := epub_re.search(f.stem)):
            add_numbered(epubs, int(m.group(1)), f)
    return audio, epubs
