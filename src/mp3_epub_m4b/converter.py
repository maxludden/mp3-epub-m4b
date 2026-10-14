"""Convert one book: drive the resumable :class:`Pipeline` with rich progress bars.

The pipeline runs in short time-budgeted slices; its checkpointed state is read
between slices to update the bars, so re-running after an interruption resumes
where it left off.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from loguru import logger
from rich.console import Group
from rich.live import Live
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
from rich.text import Text

from mp3_epub_m4b import icloud
from mp3_epub_m4b.ffmpeg_tools import FFmpegError
from mp3_epub_m4b.log import console
from mp3_epub_m4b.options import MIN_FREE_GB, OUTRO_SEARCH, Options
from mp3_epub_m4b.pairing import find_epub
from mp3_epub_m4b.report import verify_output
from mp3_epub_m4b.run import Pipeline, work_name
from mp3_epub_m4b.state import STAGES, PipelineState, Segment

# Stages that report seconds of audio via Pipeline.on_progress; shown in minutes.
_TIMED_STAGES = {"scan", "assemble"}


def stage_progress(state: PipelineState) -> tuple[float, float | None]:
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


def free_gb(path: Path) -> float:
    """Free space (GB) on the volume holding ``path``, using its nearest existing parent."""
    p = path.resolve()
    while not p.exists() and p != p.parent:
        p = p.parent
    return shutil.disk_usage(p).free / 1e9


def low_space(work_dir: Path, out_dir: Path) -> list[tuple[str, float]]:
    """``(label, free GB)`` for each of the work and output volumes below :data:`MIN_FREE_GB`."""
    free = (("work", free_gb(work_dir)), ("output", free_gb(out_dir)))
    return [(label, gb) for label, gb in free if gb < MIN_FREE_GB]


def prepare_work(epub: Path, o: Options) -> Path:
    """Create (or, with ``--fresh``, reset) the book's work folder and return it."""
    work = o.work_dir / work_name(epub.stem)
    if work.resolve() == o.work_dir.resolve():  # never treat the work root as a book folder
        raise ValueError(f"refusing to use {work} as a book work folder")
    if o.fresh and work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True, exist_ok=True)
    return work


def convert_book(audio: Path, epub: Path | None, out_dir: Path, o: Options) -> Path:
    """Run the pipeline to completion, rendering per-stage progress bars.

    Sources evicted from iCloud are downloaded first. Afterwards the M4B is
    verified (``o.verify``) and the source evicted again (``o.evict``).

    Args:
        audio: Source audio file.
        epub: Companion EPUB, or ``None`` to use the identically named one beside ``audio``.
        out_dir: Directory for the finished ``.m4b``.
        o: Shared conversion options.

    Returns:
        Path of the delivered ``.m4b`` (the readaloud EPUB, if enabled, lands beside it).

    Raises:
        StateMismatchError: If saved progress belongs to different files/options.
        FileExistsError: If the output exists and ``o.overwrite`` is false.
        FileNotFoundError: If ``epub`` is ``None`` and no identically named EPUB exists.
        RuntimeError: If the audio and EPUB don't line up (or ffmpeg fails).
    """
    epub = epub or find_epub(audio)
    work = prepare_work(epub, o)
    logger.info(f"converting {audio.name} with {epub.name} (work folder {work})")
    # Full DEBUG trail (including per-slice status) goes to a log file in the work dir.
    file_sink = logger.add(work / "pipeline.log", level="DEBUG", rotation="5 MB", retention=3)
    try:
        icloud.ensure_local(audio)
        icloud.ensure_local(epub)
        state = run_pipeline(audio, epub, out_dir, work, o)
    finally:
        logger.remove(file_sink)
    final = state.get("final_output")
    if final is None:
        raise RuntimeError("pipeline finished without recording an output path")
    if o.verify:
        report_verification(Path(final), state)
    if o.evict and icloud.evict(audio):
        logger.info("source evicted from local disk (still in iCloud)")
    return Path(final)


def plan_book(audio: Path, epub: Path | None, o: Options) -> list[Segment]:
    """Place the chapters (extract, scan, align) without encoding anything.

    Progress is checkpointed like a real run, so converting afterwards reuses
    the scan and the chapter placement. ``epub=None`` finds the identically
    named EPUB beside ``audio``.

    Returns:
        The planned chapter segments.
    """
    epub = epub or find_epub(audio)
    work = prepare_work(epub, o)
    logger.info(f"planning chapters for {audio.name} with {epub.name} (no encoding)")
    file_sink = logger.add(work / "pipeline.log", level="DEBUG", rotation="5 MB", retention=3)
    try:
        icloud.ensure_local(audio)
        icloud.ensure_local(epub)
        state = run_pipeline(audio, epub, work, work, o, stop_at="assemble")
    finally:
        logger.remove(file_sink)
    segments = state.get("segments")
    if segments is None:
        raise RuntimeError("chapter placement finished without producing segments")
    return segments


def report_verification(output: Path, state: PipelineState) -> None:
    """Log the verification summary for a finished M4B."""
    try:
        result = verify_output(
            output, state.get("mp3_duration", 0.0), len(state.get("segments", [])) or None
        )
    except (FFmpegError, ValueError, KeyError) as e:
        logger.warning(f"could not verify {output.name}: {e}")
        return
    logger.success(f"{output.name}: {result.summary()}")
    for problem in result.problems:
        logger.warning(f"verify: {problem}")


def run_pipeline(
    audio: Path,
    epub: Path,
    out_dir: Path,
    work: Path,
    o: Options,
    stop_at: str | None = None,
) -> PipelineState:
    """Drive a :class:`Pipeline` to completion (or until ``stop_at``), rendering progress bars.

    Args:
        audio: Source audio file.
        epub: Companion EPUB.
        out_dir: Directory for the finished ``.m4b``.
        work: The book's work folder.
        o: Shared conversion options.
        stop_at: Stop before running this stage (e.g. ``"assemble"`` for a dry run).

    Returns:
        The final checkpoint.
    """
    pipeline = Pipeline(
        audio,
        epub,
        out_dir,
        work,
        o.min_gap,
        "Opening Credits",
        "End Credits",
        OUTRO_SEARCH,
        o.bitrate,
        detect_intro=o.detect_intro,
        intro_max_len=o.intro_max_len,
        detect_outro=o.detect_outro,
        outro_max_tail=o.outro_max_tail,
        overwrite=o.overwrite,
        readaloud=None if stop_at else o.readaloud,
        align_by=o.align_by,
        whisper_model=o.whisper_model,
    )
    stages = pipeline.stages[: pipeline.stages.index(stop_at)] if stop_at else pipeline.stages

    def finished() -> bool:
        stage = pipeline.state["stage"]
        return stage == "done" or (stop_at is not None and STAGES.index(stage) >= len(stages))

    progress = Progress(
        SpinnerColumn(),
        TextColumn("[bold]{task.description:<16}"),
        BarColumn(),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        TimeRemainingColumn(),
        console=console,
    )
    # what the pipeline is doing right now, on its own line under the bars so a
    # long description never squeezes them
    now_line = Text("", style="#00aaff", no_wrap=True, overflow="ellipsis")
    with Live(Group(progress, now_line), console=console, refresh_per_second=4):
        overall = progress.add_task("overall", total=len(stages))
        tasks: dict[str, TaskID] = {}

        def on_progress(done: float, total: float) -> None:
            """Update the current timed stage's bar (seconds of audio -> minutes)."""
            stage = pipeline.state["stage"]
            if stage in tasks:
                progress.update(
                    tasks[stage], completed=int(done / 60), total=max(1, int(total / 60))
                )

        def on_status(detail: str, done: float | None, total: float | None) -> None:
            """Show the pipeline's latest status line and, when given, advance the stage's bar."""
            now_line.plain = detail
            stage = pipeline.state["stage"]
            if stage in tasks and done is not None and total:
                progress.update(tasks[stage], completed=done, total=total)

        pipeline.on_progress = on_progress
        pipeline.on_status = on_status

        while not finished():
            stage = pipeline.state["stage"]
            if stage not in tasks:
                label = f"{stage} (min)" if stage in _TIMED_STAGES else stage
                if stage == "deliver":
                    label = "deliver (MB)"
                tasks[stage] = progress.add_task(label, total=None)

            pipeline.run(o.slice_seconds)

            if pipeline.state["stage"] == stage:
                done, total = stage_progress(pipeline.state)
                if stage not in _TIMED_STAGES:
                    progress.update(tasks[stage], completed=done, total=total)
            else:
                progress.update(tasks[stage], total=1, completed=1)
                progress.update(overall, completed=pipeline.stages.index(stage) + 1)
        now_line.plain = ""

    return pipeline.state
