"""Typer CLI with rich progress bars.

    audiobook-kit convert "Volume 1.mp3" book_01.epub -o ./m4b
    audiobook-kit batch "/path/to/series folder"
    audiobook-kit stalign book.m4b book.epub

Drives the resumable Pipeline in short time-budgeted slices and reads its
checkpointed state between slices to update the bars, so re-running the same
command after an interruption resumes where it left off.
"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Annotated

import typer
from loguru import logger
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TaskID,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
)

from audiobook_kit.log import console, setup_logging
from audiobook_kit.run import Pipeline, PipelineState, slugify
from audiobook_kit.stalign import AlignError, AlignOptions, align_book

app = typer.Typer(
    help="Convert audiobooks (mp3/m4a) plus EPUBs into chaptered M4B files.",
    no_args_is_help=True,
    add_completion=False,
)

DEFAULT_WORK = Path.home() / ".audiobook-kit-work"
MIN_FREE_GB = 8
AUDIO_SUFFIXES = {".mp3", ".m4a"}
DEFAULT_AUDIO_PATTERN = r"Volume (\d+)"
DEFAULT_EPUB_PATTERN = r"_(\d+)$"

# Stages that report seconds of audio via Pipeline.on_progress; shown in minutes.
_TIMED_STAGES = {"scan", "assemble"}


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
    typer.Option("--out", "-o", file_okay=False, help="Output directory (default: <audio folder>/m4b)."),
]
WorkDir = Annotated[
    Path, typer.Option(file_okay=False, help="Scratch dir for checkpoints and intermediates.")
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
AlignLanguage = Annotated[str, typer.Option(help="BCP 47 audio language for the readaloud aligner.")]
AudioPattern = Annotated[
    str,
    typer.Option(help="Regex with one number group, fully matching an audio file's name without extension."),
]
EpubPattern = Annotated[
    str,
    typer.Option(help="Regex with one number group, searched in an EPUB's name without extension."),
]


def _stage_progress(state: PipelineState) -> tuple[float, float | None]:
    """Return ``(completed, total)`` for the current stage.

    ``total`` is ``None`` when the stage has no measurable progress here. The
    ``deliver`` stage reports in megabytes and ``align`` in chapters; ``scan``
    and ``assemble`` report through ``Pipeline.on_progress`` instead.
    """
    stage = state["stage"]
    align, epub, assembled = state.get("align"), state.get("epub"), state.get("assembled_path")
    if stage == "align" and align is not None and epub is not None:
        return align["next"] - 1, len(epub["chapters"])
    if stage == "deliver" and assembled is not None:  # reported in MB
        total = Path(assembled).stat().st_size / 1e6
        return state.get("deliver_bytes_done", 0) / 1e6, total
    return 0, None


def _free_gb(path: Path) -> float:
    """Free space (GB) on the volume holding ``path``, using its nearest existing parent."""
    p = path.resolve()
    while not p.exists() and p != p.parent:
        p = p.parent
    return shutil.disk_usage(p).free / 1e9


def convert_book(audio: Path, epub: Path, out_dir: Path, o: Options) -> Path:
    """Run the pipeline to completion, rendering per-stage progress bars.

    Args:
        audio: Source audio file.
        epub: Companion EPUB.
        out_dir: Directory for the finished ``.m4b``.
        o: Shared conversion options.

    Returns:
        Path of the delivered ``.m4b`` (the readaloud EPUB, if enabled, lands beside it).

    Raises:
        StateMismatchError: If saved progress belongs to different files/options.
        FileExistsError: If the output exists and ``o.overwrite`` is false.
        RuntimeError: If the audio and EPUB don't line up (or ffmpeg fails).
    """
    work = o.work_dir / slugify(epub.stem)
    if o.fresh and work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True, exist_ok=True)
    # Full DEBUG trail (including per-slice status) goes to a log file in the work dir.
    file_sink = logger.add(work / "pipeline.log", level="DEBUG", rotation="5 MB", retention=3)
    try:
        return _run_pipeline(audio, epub, out_dir, work, o)
    finally:
        logger.remove(file_sink)


def _run_pipeline(audio: Path, epub: Path, out_dir: Path, work: Path, o: Options) -> Path:
    """Drive a :class:`Pipeline` to completion, rendering progress bars."""
    pipeline = Pipeline(
        audio, epub, out_dir, work, o.min_gap,
        "Opening Credits", "End Credits", 1800.0, o.bitrate,
        detect_intro=o.detect_intro, intro_max_len=o.intro_max_len,
        detect_outro=o.detect_outro, outro_max_tail=o.outro_max_tail,
        overwrite=o.overwrite, readaloud=o.readaloud,
    )

    with Progress(
        SpinnerColumn(),
        TextColumn("[bold]{task.description:<16}"),
        BarColumn(),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        TimeRemainingColumn(),
        console=console,
    ) as progress:
        overall = progress.add_task("overall", total=len(pipeline.stages))
        tasks: dict[str, TaskID] = {}

        def on_progress(done: float, total: float) -> None:
            """Update the current timed stage's bar (seconds of audio -> minutes)."""
            stage = pipeline.state["stage"]
            if stage in tasks:
                progress.update(tasks[stage], completed=int(done / 60), total=max(1, int(total / 60)))

        pipeline.on_progress = on_progress

        while pipeline.state["stage"] != "done":
            stage = pipeline.state["stage"]
            if stage not in tasks:
                label = f"{stage} (min)" if stage in _TIMED_STAGES else stage
                if stage == "deliver":
                    label = "deliver (MB)"
                tasks[stage] = progress.add_task(label, total=None)

            pipeline.run(o.slice_seconds)

            if pipeline.state["stage"] == stage:
                done, total = _stage_progress(pipeline.state)
                if stage not in _TIMED_STAGES:
                    progress.update(tasks[stage], completed=done, total=total)
            else:
                progress.update(tasks[stage], total=1, completed=1)
                progress.update(overall, completed=pipeline.stages.index(stage) + 1)

    final = pipeline.state.get("final_output")
    if final is None:
        raise RuntimeError("pipeline finished without recording an output path")
    return Path(final)


def _readaloud_options(
    enabled: bool, device: Device | None, language: str
) -> AlignOptions | None:
    """Alignment settings for the readaloud stage, or ``None`` when it is disabled."""
    if not enabled:
        return None
    return AlignOptions(language=language, device=device.value if device else None)


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
) -> None:
    """Convert one AUDIO file with its EPUB, then build a readaloud EPUB from the M4B."""
    setup_logging()
    _require_tools()
    opts = Options(work_dir, min_gap, bitrate, detect_intro, intro_max_len,
                   detect_outro, outro_max_tail, slice_seconds, overwrite, fresh,
                   _readaloud_options(readaloud, align_device, align_language))
    out_dir = out or audio.parent / "m4b"
    for label, path in (("work", work_dir), ("output", out_dir)):
        free_gb = _free_gb(path)
        if free_gb < MIN_FREE_GB:
            logger.warning(f"only {free_gb:.0f}GB free on the {label} volume")
    try:
        final = convert_book(audio, epub, out_dir, opts)
    except (RuntimeError, ValueError, OSError) as e:
        logger.error(f"Failed: {e}")
        raise typer.Exit(1) from e
    logger.success(f"Done: {final}")


def _pair_files(
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
            audio[int(m.group(1))] = f
        elif suffix == ".epub" and (m := epub_re.search(f.stem)):
            epubs[int(m.group(1))] = f
    return audio, epubs


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
) -> None:
    """Convert every numbered audio + EPUB pair in FOLDER (default: 'Volume N.mp3' + '*_NN.epub')."""
    setup_logging()
    _require_tools()
    opts = Options(work_dir, min_gap, bitrate, detect_intro, intro_max_len,
                   detect_outro, outro_max_tail, slice_seconds, overwrite, fresh,
                   _readaloud_options(readaloud, align_device, align_language))
    try:
        audio, epubs = _pair_files(folder, audio_pattern, epub_pattern)
    except re.error as e:
        raise typer.BadParameter(f"invalid pattern: {e}") from e
    for n in sorted(audio.keys() - epubs.keys()):
        logger.warning(f"SKIP: {audio[n].name} has no matching epub")
    for n in sorted(epubs.keys() - audio.keys()):
        logger.info(f"note: {epubs[n].name} has no matching audio")
    pairs = sorted(audio.keys() & epubs.keys())
    if not pairs:
        logger.error(f"no convertible pairs found in {folder}")
        raise typer.Exit(1)
    logger.info(f"Converting {len(pairs)} book(s): {', '.join(map(str, pairs))}")

    failed = False
    for n in pairs:
        out_dir = out or audio[n].parent / "m4b"
        low = [(lbl, g) for lbl, p in (("work", work_dir), ("output", out_dir))
               if (g := _free_gb(p)) < MIN_FREE_GB]
        if low:
            lbl, g = low[0]
            logger.error(f"STOPPING: only {g:.0f}GB free on the {lbl} volume (need {MIN_FREE_GB}GB)")
            failed = True
            break
        console.rule(f"Book {n}")
        try:
            final = convert_book(audio[n], epubs[n], out_dir, opts)
            logger.success(f"Book {n} complete: {final}")
        except Exception as e:  # keep going with the next book
            logger.error(f"Book {n} failed: {e}")
            failed = True
    raise typer.Exit(1 if failed else 0)


_PASSTHROUGH = {"allow_extra_args": True, "ignore_unknown_options": True}


@app.command("align", hidden=True, context_settings=_PASSTHROUGH)  # silent alias of stalign
@app.command("stalign", context_settings=_PASSTHROUGH)
def stalign(
    ctx: typer.Context,
    audio: Annotated[
        Path, typer.Argument(exists=True, help="An .m4b/.mp3/.m4a file, or a directory of audio files.")
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
        typer.Option("--engine", "-e", help="Transcribe with this engine (e.g. whisper.cpp) instead of CTC."),
    ] = None,
    model: Annotated[
        str | None, typer.Option("--model", "-m", help="CTC model (default mms) or whisper model.")
    ] = None,
    codec: Annotated[
        Codec | None, typer.Option(help="Audio codec (default: copy).")
    ] = None,
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
