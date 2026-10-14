"""Shared option definitions for the command-line interface.

Holds the settings dataclass, the enums behind choice options, the default
values, and the ``Annotated`` option types that ``convert`` and ``batch`` share,
so the commands in :mod:`mp3_epub_m4b.cli` only declare what differs.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Annotated

import typer

from mp3_epub_m4b.align import AlignOptions

# Scratch space for checkpoints and intermediates; override with MP3_EPUB_M4B_WORK.
DEFAULT_WORK = Path.home() / ".mp3-epub-m4b-work"
MIN_FREE_GB = 8
AUDIO_SUFFIXES = {".mp3", ".m4a"}
DEFAULT_AUDIO_PATTERN = r"Vol\.?u?m?e?\s?(\d+)|(\d+) - .*"
DEFAULT_EPUB_PATTERN = r"_(\d+)$|(\d+) - "
OUTRO_SEARCH = 1800.0  # how far (s) past the last chapter's start to look for end credits


@dataclass
class Options:
    """Settings shared by every book converted in one CLI invocation.

    Attributes:
        work_dir: Scratch directory for checkpoints and intermediates.
        min_gap: Minimum silence (s) counted as a chapter transition.
        bitrate: AAC bitrate, e.g. ``"96k"``.
        detect_intro: Split opening credits into their own chapter.
        intro_max_len: Longest accepted intro (s).
        detect_outro: Split end credits into their own chapter.
        outro_max_tail: Longest accepted outro tail (s).
        slice_seconds: How often (s) the progress bars refresh.
        overwrite: Replace an existing output file.
        fresh: Discard any saved progress for the book and start over.
        readaloud: Alignment settings for the readaloud stage; ``None`` skips it.
        evict: Evict the source audio from local disk afterwards (iCloud Drive only).
        verify: Probe the finished M4B and report chapters, duration and streams.
    """

    work_dir: Path
    min_gap: float
    bitrate: str
    detect_intro: bool
    intro_max_len: float
    detect_outro: bool
    outro_max_tail: float
    slice_seconds: float
    overwrite: bool = False
    fresh: bool = False
    readaloud: AlignOptions | None = None
    evict: bool = False
    verify: bool = True


class Granularity(StrEnum):
    """Alignment granularity."""

    sentence = "sentence"
    word = "word"


class Device(StrEnum):
    """Compute device for the CTC aligner."""

    auto = "auto"
    cpu = "cpu"
    webgpu = "webgpu"
    dml = "dml"
    cuda = "cuda"
    mlx = "mlx"


class Codec(StrEnum):
    """Audio codec for the aligned EPUB's audio."""

    copy = "copy"
    aac = "aac"
    libopus = "libopus"
    libmp3lame = "libmp3lame"


OutDir = Annotated[
    Path | None,
    typer.Option(
        "--out", "-o", file_okay=False, help="Output directory (default: <audio folder>/m4b)."
    ),
]
WorkDir = Annotated[
    Path,
    typer.Option(
        file_okay=False,
        envvar="MP3_EPUB_M4B_WORK",
        help="Scratch dir for checkpoints and intermediates.",
    ),
]
MinGap = Annotated[float, typer.Option(help="Minimum silence (s) counted as a chapter transition.")]
Bitrate = Annotated[str, typer.Option(help="AAC bitrate.")]
DetectIntro = Annotated[bool, typer.Option(help="Split opening credits into their own chapter.")]
IntroMax = Annotated[float, typer.Option(help="Max length (s) of an accepted intro.")]
DetectOutro = Annotated[bool, typer.Option(help="Split end credits into their own chapter.")]
OutroMax = Annotated[float, typer.Option(help="Max length (s) of an accepted outro.")]
SliceSeconds = Annotated[
    float, typer.Option(min=0.1, help="How often (s) the progress bars refresh.")
]
Overwrite = Annotated[bool, typer.Option(help="Replace an existing .m4b in the output directory.")]
Fresh = Annotated[
    bool, typer.Option(help="Discard saved progress for the book and start from scratch.")
]
Readaloud = Annotated[
    bool,
    typer.Option(
        "--readaloud/--no-readaloud",
        help="After the M4B is delivered, align it with the EPUB into a readaloud EPUB 3.",
    ),
]
AlignDevice = Annotated[
    Device | None, typer.Option(help="Compute device for the readaloud aligner (default: auto).")
]
AlignLanguage = Annotated[
    str, typer.Option(help="BCP 47 audio language for the readaloud aligner.")
]
AudioPattern = Annotated[
    str,
    typer.Option(
        help="Regex with one number group, fully matching an audio file's name without extension."
    ),
]
EpubPattern = Annotated[
    str,
    typer.Option(help="Regex with one number group, searched in an EPUB's name without extension."),
]
Yes = Annotated[
    bool,
    typer.Option("--yes", "-y", help="Don't ask for confirmation of the pairs before converting."),
]
DryRun = Annotated[
    bool,
    typer.Option(
        "--dry-run",
        help="Place chapters and print the chapter table, but don't encode anything.",
    ),
]
Verify = Annotated[
    bool,
    typer.Option(
        "--verify/--no-verify",
        help="Probe the finished M4B and report its chapters, duration and streams.",
    ),
]
Evict = Annotated[
    bool,
    typer.Option(
        "--evict/--no-evict",
        help="After a successful run, evict the source audio from local disk "
        "(only for files in iCloud Drive; macOS).",
    ),
]
