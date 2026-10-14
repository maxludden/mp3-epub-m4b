"""Typer CLI.

    mp3-epub-m4b convert "Volume 1.mp3" book_01.epub -o ./m4b
        mp3-epub-m4b batch "/path/to/series folder"
    mp3-epub-m4b stalign book.m4b book.epub

The commands only parse options and pick files; :mod:`mp3_epub_m4b.converter`
runs the resumable pipeline (re-running a command after an interruption resumes
where it left off) and :mod:`mp3_epub_m4b.pairing` matches audio to EPUBs.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Annotated

import typer
from loguru import logger

from mp3_epub_m4b.align import AlignError, AlignOptions, align_book
from mp3_epub_m4b.converter import convert_book, low_space, plan_book
from mp3_epub_m4b.log import console, setup_logging
from mp3_epub_m4b.options import (
    DEFAULT_AUDIO_PATTERN,
    DEFAULT_EPUB_PATTERN,
    DEFAULT_WORK,
    MIN_FREE_GB,
    AlignDevice,
    AlignLanguage,
    AudioPattern,
    Bitrate,
    Codec,
    DetectIntro,
    DetectOutro,
    Device,
    DryRun,
    EpubPattern,
    Evict,
    Fresh,
    Granularity,
    IntroMax,
    MinGap,
    Options,
    OutDir,
    OutroMax,
    Overwrite,
    Readaloud,
    SliceSeconds,
    Verify,
    WorkDir,
)
from mp3_epub_m4b.pairing import pair_files
from mp3_epub_m4b.report import chapter_table

app = typer.Typer(
    help="Convert audiobooks (mp3/m4a) plus EPUBs into chaptered M4B files.",
    no_args_is_help=True,
    add_completion=False,
)


def _readaloud_options(enabled: bool, device: Device | None, language: str) -> AlignOptions | None:
    """Alignment settings for the readaloud stage, or ``None`` when it is disabled."""
    if not enabled:
        return None
    return AlignOptions(language=language, device=device.value if device else None)


def _build_options(
    *,
    work_dir: Path,
    min_gap: float,
    bitrate: str,
    detect_intro: bool,
    intro_max_len: float,
    detect_outro: bool,
    outro_max_tail: float,
    slice_seconds: float,
    overwrite: bool,
    fresh: bool,
    readaloud: bool,
    align_device: Device | None,
    align_language: str,
    evict: bool,
    verify: bool,
) -> Options:
    """Assemble the :class:`Options` shared by ``convert`` and ``batch`` from their CLI flags."""
    return Options(
        work_dir,
        min_gap,
        bitrate,
        detect_intro,
        intro_max_len,
        detect_outro,
        outro_max_tail,
        slice_seconds,
        overwrite,
        fresh,
        _readaloud_options(readaloud, align_device, align_language),
        evict,
        verify,
    )


def _require_tools() -> None:
    """Ensure ``ffmpeg`` and ``ffprobe`` are on ``PATH``.

    Raises:
        typer.BadParameter: If either tool is missing.
    """
    for tool in ("ffmpeg", "ffprobe"):
        if shutil.which(tool) is None:
            raise typer.BadParameter(f"{tool} not found on PATH (brew install ffmpeg)")


@app.command()
def convert(
    audio: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
    epub: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
    out: OutDir = None,
    work_dir: WorkDir = DEFAULT_WORK,
    min_gap: MinGap = 3.5,
    bitrate: Bitrate = "96k",
    detect_intro: DetectIntro = False,
    intro_max_len: IntroMax = 120.0,
    detect_outro: DetectOutro = False,
    outro_max_tail: OutroMax = 180.0,
    slice_seconds: SliceSeconds = 5.0,
    overwrite: Overwrite = False,
    fresh: Fresh = False,
    readaloud: Readaloud = True,
    align_device: AlignDevice = None,
    align_language: AlignLanguage = "en-US",
    verify: Verify = True,
    evict: Evict = False,
    dry_run: DryRun = False,
) -> None:
    """Convert one AUDIO file with its EPUB, then build a readaloud EPUB from the M4B.

    With --dry-run, only place the chapters and print the chapter table.
    """
    setup_logging()
    _require_tools()
    opts = _build_options(
        work_dir=work_dir,
        min_gap=min_gap,
        bitrate=bitrate,
        detect_intro=detect_intro,
        intro_max_len=intro_max_len,
        detect_outro=detect_outro,
        outro_max_tail=outro_max_tail,
        slice_seconds=slice_seconds,
        overwrite=overwrite,
        fresh=fresh,
        readaloud=readaloud,
        align_device=align_device,
        align_language=align_language,
        evict=evict,
        verify=verify,
    )
    out_dir = out or audio.parent / "m4b"
    if not dry_run:
        for label, free_gb in low_space(work_dir, out_dir):
            logger.warning(f"only {free_gb:.0f}GB free on the {label} volume")
    try:
        final = _process_book(audio, epub, out_dir, opts, dry_run)
    except (RuntimeError, ValueError, OSError) as e:
        logger.error(f"Failed: {e}")
        raise typer.Exit(1) from e
    if final is not None:
        logger.success(f"Done: {final}")


def _process_book(
    audio: Path, epub: Path, out_dir: Path, opts: Options, dry_run: bool
) -> Path | None:
    """Convert one book, or with ``dry_run`` just print its planned chapters (returns ``None``)."""
    if dry_run:
        console.print(chapter_table(plan_book(audio, epub, opts)))
        return None
    return convert_book(audio, epub, out_dir, opts)


@app.command()
def batch(
    folder: Annotated[Path, typer.Argument(exists=True, file_okay=False)],
    out: OutDir = None,
    work_dir: WorkDir = DEFAULT_WORK,
    min_gap: MinGap = 3.5,
    bitrate: Bitrate = "96k",
    detect_intro: DetectIntro = False,
    intro_max_len: IntroMax = 120.0,
    detect_outro: DetectOutro = False,
    outro_max_tail: OutroMax = 180.0,
    slice_seconds: SliceSeconds = 5.0,
    overwrite: Overwrite = False,
    fresh: Fresh = False,
    audio_pattern: AudioPattern = DEFAULT_AUDIO_PATTERN,
    epub_pattern: EpubPattern = DEFAULT_EPUB_PATTERN,
    readaloud: Readaloud = True,
    align_device: AlignDevice = None,
    align_language: AlignLanguage = "en-US",
    verify: Verify = True,
    evict: Evict = False,
    dry_run: DryRun = False,
) -> None:
    """Convert every numbered audio + EPUB pair in FOLDER (default: 'Volume N.mp3' + '*_NN.epub')."""
    setup_logging()
    _require_tools()
    opts = _build_options(
        work_dir=work_dir,
        min_gap=min_gap,
        bitrate=bitrate,
        detect_intro=detect_intro,
        intro_max_len=intro_max_len,
        detect_outro=detect_outro,
        outro_max_tail=outro_max_tail,
        slice_seconds=slice_seconds,
        overwrite=overwrite,
        fresh=fresh,
        readaloud=readaloud,
        align_device=align_device,
        align_language=align_language,
        evict=evict,
        verify=verify,
    )
    try:
        audio, epubs = pair_files(folder, audio_pattern, epub_pattern)
    except re.error as e:
        raise typer.BadParameter(f"invalid pattern: {e}") from e
    for n in sorted(audio.keys() - epubs.keys()):
        logger.warning(f"SKIP: {audio[n].name} has no matching epub")
    for n in sorted(epubs.keys() - audio.keys()):
        logger.info(f"note: {epubs[n].name} has no matching audio")
    jobs = [(f"Book {n}", audio[n], epubs[n]) for n in sorted(audio.keys() & epubs.keys())]
    if not jobs:
        logger.error(f"no convertible pairs found in {folder}")
        raise typer.Exit(1)
    logger.info(f"Converting {len(jobs)} book(s): {', '.join(label for label, _, _ in jobs)}")

    failed = False
    for label, book_audio, book_epub in jobs:
        out_dir = out or book_audio.parent / "m4b"
        if not dry_run and (low := low_space(work_dir, out_dir)):
            lbl, g = low[0]
            logger.error(
                f"STOPPING: only {g:.0f}GB free on the {lbl} volume (need {MIN_FREE_GB}GB)"
            )
            failed = True
            break
        console.rule(label)
        try:
            final = _process_book(book_audio, book_epub, out_dir, opts, dry_run)
            if final is not None:
                logger.success(f"{label} complete: {final}")
        except Exception as e:  # keep going with the next book
            logger.error(f"{label} failed: {e}")
            failed = True
    raise typer.Exit(1 if failed else 0)


_PASSTHROUGH = {"allow_extra_args": True, "ignore_unknown_options": True}


@app.command("align", hidden=True, context_settings=_PASSTHROUGH)  # silent alias of stalign
@app.command("stalign", context_settings=_PASSTHROUGH)
def stalign(
    ctx: typer.Context,
    audio: Annotated[
        Path,
        typer.Argument(exists=True, help="An .m4b/.mp3/.m4a file, or a directory of audio files."),
    ],
    epub: Annotated[Path, typer.Argument(exists=True, dir_okay=False, help="The book's EPUB.")],
    output: Annotated[
        Path | None,
        typer.Argument(dir_okay=False, help='Output EPUB (default: "<epub name> (aligned).epub").'),
    ] = None,
    granularity: Annotated[
        Granularity, typer.Option("--granularity", "-g", help="Alignment granularity.")
    ] = Granularity.sentence,
    language: Annotated[
        str, typer.Option("--language", "-l", help="BCP 47 audio language.")
    ] = "en-US",
    device: Annotated[
        Device | None, typer.Option("--device", "-d", help="Compute device (default: auto).")
    ] = None,
    engine: Annotated[
        str | None,
        typer.Option(
            "--engine", "-e", help="Transcribe with this engine (e.g. whisper.cpp) instead of CTC."
        ),
    ] = None,
    model: Annotated[
        str | None, typer.Option("--model", "-m", help="CTC model (default mms) or whisper model.")
    ] = None,
    codec: Annotated[Codec | None, typer.Option(help="Audio codec (default: copy).")] = None,
    progress: Annotated[bool, typer.Option(help="Show the aligner's progress bars.")] = False,
) -> None:
    """Align AUDIO with EPUB into an EPUB 3 with Media Overlays.

    Uses @storyteller-platform/align (needs Node >= 24); unknown options are
    passed straight through to it.
    """
    setup_logging()
    opts = AlignOptions(
        granularity=granularity.value,
        language=language,
        device=device.value if device else None,
        engine=engine,
        model=model,
        codec=codec.value if codec else None,
        progress=progress,
        extra=list(ctx.args),
    )
    try:
        final = align_book(audio, epub, output, opts)
    except AlignError as e:
        logger.error(str(e))
        raise typer.Exit(1) from e
    logger.success(f"DONE: {final} ({final.stat().st_size / 1e6:.0f} MB)")


def main() -> None:
    """Console-script entry point; runs the Typer app."""
    app()


if __name__ == "__main__":
    main()
