"""
Legacy standalone tool (the main pipeline now encodes in a single pass; see
``audiobook_kit.run``).

Encode each aligned chapter segment of the source MP3 to its own AAC (.m4a)
file. Resumable/batched: a single invocation stops after a wall-clock
budget so it fits the sandbox's per-call time cap, and can be re-run to
pick up where it left off.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Annotated

import typer
from loguru import logger
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
)

from audiobook_kit.align_chapters import run_tool
from audiobook_kit.log import console, setup_logging

app = typer.Typer(
    help="Encode aligned chapter segments of the source audio to AAC (.m4a) files.",
    add_completion=False,
)


def safe_name(n: int, title: str) -> str:
    """Build a filesystem-safe segment filename from a chapter number and title.

    Runs of non-alphanumeric characters in ``title`` collapse to a single
    underscore, leading/trailing underscores are stripped, and the slug is
    truncated to 60 characters.

    Args:
        n: Segment number, zero-padded to three digits in the result.
        title: Human-readable segment title.

    Returns:
        A filename of the form ``"<NNN>_<slug>.m4a"``.
    """
    slug = re.sub(r"[^A-Za-z0-9]+", "_", title).strip("_")[:60]
    return f"{n:03d}_{slug}.m4a"


def encode_one(
    mp3: Path,
    start: float,
    end: float,
    out_path: Path,
    bitrate: str = "96k",
    channels: int = 1,
    sample_rate: int = 32000,
) -> None:
    """Encode one time range of ``mp3`` to an AAC file with ffmpeg.

    Args:
        mp3: Source audio file.
        start: Range start, in seconds.
        end: Range end, in seconds.
        out_path: Destination file; overwritten if it exists.
        bitrate: AAC bitrate passed to ffmpeg's ``-b:a`` (e.g. ``"96k"``).
        channels: Number of output audio channels.
        sample_rate: Output sample rate in Hz.

    Raises:
        FFmpegError: If ffmpeg fails; the message includes its stderr.
    """
    dur = end - start
    logger.debug(f"encoding {out_path.name} ({start:.1f}s-{end:.1f}s)")
    cmd = [
        "ffmpeg", "-y", "-nostdin", "-hide_banner", "-loglevel", "error",
        "-ss", f"{start:.3f}", "-t", f"{dur:.3f}", "-i", str(mp3),
        "-c:a", "aac", "-b:a", bitrate, "-ac", str(channels), "-ar", str(sample_rate),
        str(out_path),
    ]
    run_tool(cmd)


@app.command()
def main(
    aligned_json: Annotated[
        Path, typer.Argument(exists=True, dir_okay=False, help="The <book>.aligned.json to encode.")
    ],
    project_root: Annotated[
        Path, typer.Option(file_okay=False, help="Directory the book's audio path is relative to.")
    ] = Path("."),
    out_dir: Annotated[
        Path, typer.Option(file_okay=False, help="Segment output directory, under the project root.")
    ] = Path("work/segments"),
    time_budget: Annotated[
        float, typer.Option(help="Stop starting new segments after this many wall-clock seconds.")
    ] = 35.0,
    bitrate: Annotated[str, typer.Option(help="AAC bitrate.")] = "96k",
) -> None:
    """Encode the not-yet-done segments of an aligned book.

    Reads the <book>.aligned.json produced by audiobook_kit.align_chapters,
    encodes segments with a progress bar until --time-budget seconds have
    elapsed, and records finished segment numbers in _progress.json inside the
    output directory so a re-run resumes where it stopped.
    """
    setup_logging()
    data = json.loads(aligned_json.read_text())
    mp3 = project_root / data["source"]["mp3"]
    segments = data["segments"]

    out_dir = project_root / out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    progress_path = out_dir / "_progress.json"
    done = set(json.loads(progress_path.read_text())) if progress_path.exists() else set()

    t0 = time.monotonic()
    processed = 0
    with Progress(
        SpinnerColumn(),
        TextColumn("[bold]encode"),
        BarColumn(),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        TimeRemainingColumn(),
        console=console,
    ) as progress:
        task = progress.add_task("encode", total=len(segments), completed=len(done))
        for seg in segments:
            if seg["n"] in done:
                continue
            if (time.monotonic() - t0) >= time_budget:
                break
            out_path = out_dir / safe_name(seg["n"], seg["title"])
            encode_one(mp3, seg["start"], seg["end"], out_path, bitrate=bitrate)
            done.add(seg["n"])
            processed += 1
            progress_path.write_text(json.dumps(sorted(done)))
            progress.update(task, completed=len(done))

    progress_path.write_text(json.dumps(sorted(done)))
    elapsed = time.monotonic() - t0
    logger.success(
        f"encoded {processed} segments in {elapsed:.1f}s; total done {len(done)}/{len(segments)}"
    )


if __name__ == "__main__":
    app()
