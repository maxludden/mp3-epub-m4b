"""
Align Audible's rounded, drift-y "chapter length" figures to real audio
boundaries by looking for silence near each expected cut point.

Resumable: each invocation processes a batch of boundaries (bounded by
wall-clock budget) and persists state to a JSON file, since the sandbox
this runs in caps a single process at ~45s of wall time.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any, TypedDict

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

app = typer.Typer(
    help="Align Audible chapter lengths to real silences in the audio.",
    add_completion=False,
)


class Boundary(TypedDict):
    """Debug record for one detected chapter boundary."""

    chapter_index: int
    expected: float
    actual: float
    window: float
    method: str


class AlignState(TypedDict):
    """Resumable progress persisted to the ``*.align_state.json`` file."""

    prev_actual: float
    next_index: int
    boundaries: list[Boundary]
    n: int


def parse_mmss(s: str) -> float:
    """Parse a ``M:SS``/``MM:SS`` or ``H:MM:SS`` duration into seconds.

    Args:
        s: Colon-separated duration with two or three integer fields.

    Returns:
        The duration in seconds.

    Raises:
        ValueError: If ``s`` does not have two or three colon-separated parts,
            or a part is not an integer.
    """
    parts = [int(p) for p in s.split(":")]
    if len(parts) == 2:
        m, sec = parts
        return m * 60 + sec
    if len(parts) == 3:
        h, m, sec = parts
        return h * 3600 + m * 60 + sec
    raise ValueError(f"bad duration {s!r}")


def ffprobe_duration(path: Path) -> float:
    """Return the container duration of a media file, in seconds, via ffprobe.

    Args:
        path: Media file to probe.

    Returns:
        The duration in seconds.

    Raises:
        FFmpegError: If ffprobe fails.
        ValueError: If ffprobe's output is not a number.
    """
    out = run_tool(
        [
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", str(path),
        ]
    )
    return float(out.stdout.strip())


_FLOAT = r"(-?[0-9]+(?:\.[0-9]*)?(?:e-?[0-9]+)?)"
_SILENCE_START = re.compile(rf"silence_start:\s*{_FLOAT}")
_SILENCE_END = re.compile(rf"silence_end:\s*{_FLOAT}\s*\|\s*silence_duration:\s*{_FLOAT}")


class FFmpegError(RuntimeError):
    """An ffmpeg/ffprobe invocation failed; the message carries its stderr tail."""


def run_tool(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    """Run an ffmpeg/ffprobe command, capturing text output.

    Args:
        cmd: Full command line.

    Returns:
        The completed process (exit status zero).

    Raises:
        FFmpegError: If the program is missing or exits non-zero. The message
            includes the last lines of stderr, which ``check=True`` would hide.
    """
    logger.debug("running: {}", " ".join(cmd))
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True)
    except FileNotFoundError as e:
        raise FFmpegError(f"{cmd[0]} not found on PATH") from e
    if proc.returncode != 0:
        tail = "\n".join(proc.stderr.strip().splitlines()[-8:])
        raise FFmpegError(f"{cmd[0]} exited with {proc.returncode}:\n{tail}")
    return proc


def detect_silences(
    path: Path,
    start: float,
    dur: float,
    noise_db: float = -35,
    min_silence: float = 0.35,
) -> list[tuple[float, float]]:
    """Find silent stretches inside a clip of an audio file using ffmpeg's ``silencedetect``.

    Args:
        path: Audio file to scan.
        start: Clip start in seconds; negative values are clamped to 0.
        dur: Clip length in seconds.
        noise_db: Noise floor in dB below which audio counts as silence.
        min_silence: Minimum silence length in seconds to report.

    Returns:
        ``(silence_start, silence_end)`` pairs in seconds, **relative to the
        (clamped) start of the clip**, in chronological order. A trailing
        silence that is still open at the end of the clip is omitted.

    Raises:
        FFmpegError: If ffmpeg fails (for example an unreadable file).
    """
    start = max(0.0, start)
    cmd = [
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "info",
        "-ss", f"{start:.3f}", "-t", f"{dur:.3f}", "-i", str(path),
        "-vn", "-af", f"silencedetect=noise={noise_db}dB:d={min_silence}",
        "-f", "null", "-",
    ]
    log = run_tool(cmd).stderr
    starts = [float(m.group(1)) for m in _SILENCE_START.finditer(log)]
    ends = [(float(m.group(1)), float(m.group(2))) for m in _SILENCE_END.finditer(log)]
    intervals: list[tuple[float, float]] = []
    for i, s in enumerate(starts):
        if i < len(ends):
            e, _d = ends[i]
            intervals.append((s, e))
    return intervals


def scan_silences(
    path: Path,
    total_dur: float,
    noise_db: float = -30,
    min_silence: float = 1.5,
    on_progress: Callable[[float, float], None] | None = None,
) -> list[tuple[float, float]]:
    """Scan a whole audio file once and return every silence of at least ``min_silence``.

    One sequential decode is far cheaper than re-decoding a window per
    chapter, and the result can be cached and searched in memory.

    Args:
        path: Audio file to scan.
        total_dur: File duration in seconds (closes a silence left open at EOF
            and scales progress).
        noise_db: Noise floor in dB below which audio counts as silence.
        min_silence: Minimum silence length in seconds to report.
        on_progress: Called as ``(seconds_scanned, total_dur)`` while scanning.

    Returns:
        ``(start, end)`` pairs in absolute file seconds, chronological.

    Raises:
        FFmpegError: If ffmpeg cannot be run or exits non-zero.
    """
    cmd = [
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "info", "-nostats",
        "-progress", "pipe:2", "-i", str(path), "-vn",
        "-af", f"silencedetect=noise={noise_db}dB:d={min_silence}",
        "-f", "null", "-",
    ]
    logger.info(f"scanning {path.name} for silences")
    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, bufsize=1
        )
    except FileNotFoundError as e:
        raise FFmpegError("ffmpeg not found on PATH") from e
    assert proc.stderr is not None

    silences: list[tuple[float, float]] = []
    open_start: float | None = None
    tail: deque[str] = deque(maxlen=8)
    for line in proc.stderr:
        line = line.strip()
        if line.startswith("out_time_us="):
            if on_progress is not None and line[12:].lstrip("-").isdigit():
                on_progress(min(int(line[12:]) / 1e6, total_dur), total_dur)
            continue
        if "=" in line and " " not in line:  # other -progress key=value lines
            continue
        tail.append(line)
        if (m := _SILENCE_START.search(line)) is not None:
            open_start = float(m.group(1))
        elif (m := _SILENCE_END.search(line)) is not None:
            end, dur = float(m.group(1)), float(m.group(2))
            start = open_start if open_start is not None else end - dur
            silences.append((max(0.0, start), end))
            open_start = None
    if proc.wait() != 0:
        raise FFmpegError(f"ffmpeg exited with {proc.returncode}:\n" + "\n".join(tail))
    if open_start is not None:  # silence runs to end of file
        silences.append((max(0.0, open_start), total_dur))
    logger.debug(f"scan found {len(silences)} silences of at least {min_silence}s")
    if on_progress is not None:
        on_progress(total_dur, total_dur)
    return silences


def find_boundary(
    mp3: Path,
    expected: float,
    total_dur: float,
    noise_db: float,
    min_silence: float,
    base_window: float,
    max_window: float,
) -> tuple[float, float, str]:
    """Locate a chapter boundary by finding the silence nearest an expected time.

    Searches a window centred on ``expected``; if no silence is found the
    window doubles until it exceeds ``max_window``, after which ``expected``
    itself is used.

    Args:
        mp3: Audio file to scan.
        expected: Expected boundary position, in seconds.
        total_dur: Total file duration; the result is capped at this value.
        noise_db: Noise floor in dB for silence detection.
        min_silence: Minimum silence length in seconds.
        base_window: Initial half-width of the search window, in seconds.
        max_window: Largest half-width to try, in seconds.

    Returns:
        ``(boundary, window, method)``: the boundary in seconds, the final
        window half-width used, and ``"silence"`` or ``"fallback"``.
    """
    window = base_window
    found: float | None = None
    method = "fallback"
    while window <= max_window and found is None:
        # Clamp here (not just inside detect_silences) so the offsets added
        # back below are relative to the clip start ffmpeg actually used.
        clip_start = max(0.0, expected - window)
        clip_dur = expected + window - clip_start
        intervals = detect_silences(mp3, clip_start, clip_dur, noise_db, min_silence)
        if intervals:
            best: float | None = None
            best_dist: float | None = None
            for s, e in intervals:
                mid_abs = clip_start + (s + e) / 2
                dist = abs(mid_abs - expected)
                if best_dist is None or dist < best_dist:
                    best_dist, best = dist, mid_abs
            found, method = best, "silence"
        else:
            window *= 2
    if found is None:
        logger.warning(f"no silence within {max_window}s of {expected:.1f}s; using the estimate")
        found = expected
    return min(found, total_dur), window, method


def load_state(state_path: Path, n: int) -> AlignState:
    """Load saved alignment progress, or start fresh if there is none.

    Args:
        state_path: JSON state file.
        n: Number of chapters; a saved state for a different count is rejected.

    Returns:
        The persisted state, or a new state at chapter index 1.

    Raises:
        ValueError: If the saved state was made for a different chapter count.
    """
    if state_path.exists():
        logger.info(f"resuming from {state_path}")
        state: AlignState = json.loads(state_path.read_text())
        if state["n"] != n:
            raise ValueError(
                f"{state_path} was saved for {state['n']} chapters but the book has {n}; "
                "delete it to start over"
            )
        return state
    return {"prev_actual": 0.0, "next_index": 1, "boundaries": [], "n": n}


def save_state(state_path: Path, state: AlignState) -> None:
    """Persist alignment progress as JSON (atomically, via a temp file).

    Args:
        state_path: Destination JSON file; overwritten.
        state: Progress to write.
    """
    tmp = state_path.with_suffix(state_path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(state_path)


@app.command()
def main(
    book_json: Annotated[
        Path,
        typer.Argument(
            exists=True, dir_okay=False, help="Book JSON with source and chapters."
        ),
    ],
    project_root: Annotated[
        Path,
        typer.Option(
            file_okay=False, help="Directory the book's audio path is relative to."
        ),
    ] = Path("."),
    state_file: Annotated[
        Path | None,
        typer.Option(
            dir_okay=False, help="Progress file (default: <book>.align_state.json)."
        ),
    ] = None,
    time_budget: Annotated[
        float,
        typer.Option(
            help="Stop starting new boundaries after this many wall-clock seconds."
        ),
    ] = 35.0,
    noise_db: Annotated[
        float, typer.Option(help="Noise floor (dB) below which audio is silence.")
    ] = -35,
    min_silence: Annotated[
        float, typer.Option(help="Minimum silence length (s) to detect.")
    ] = 0.4,
    base_window: Annotated[
        float, typer.Option(help="Initial search half-width (s).")
    ] = 6.0,
    max_window: Annotated[
        float, typer.Option(help="Largest search half-width (s).")
    ] = 45.0,
    finalize: Annotated[
        bool,
        typer.Option(help="Write the final <book>.aligned.json from state and exit."),
    ] = False,
) -> None:
    """Align Audible's chapter lengths to real audio boundaries by finding nearby silences.

    Processes boundaries until --time-budget seconds have elapsed, with a
    progress bar, and saves state so a re-run continues. With --finalize and
    all boundaries found, writes <book>.aligned.json instead.
    """
    setup_logging()
    data: dict[str, Any] = json.loads(book_json.read_text())
    mp3 = project_root / data["source"]["mp3"]
    chapters = data["chapters"]
    n = len(chapters)

    state_path = state_file or book_json.with_name(book_json.stem + ".align_state.json")
    state = load_state(state_path, n)

    total_dur = ffprobe_duration(mp3)
    reported = [parse_mmss(c["reported_duration"]) for c in chapters]

    if finalize:
        if state["next_index"] < n:
            logger.error(
                f"NOT DONE: next_index={state['next_index']} of {n}; run more batches first"
            )
            raise typer.Exit(1)
        starts = [0.0] + [b["actual"] for b in state["boundaries"]]
        ends = starts[1:] + [total_dur]
        segments: list[dict[str, Any]] = []
        for c, s, e in zip(chapters, starts, ends, strict=True):
            segments.append(
                {
                    "n": c["n"],
                    "title": c["title"],
                    "reported_duration": c["reported_duration"],
                    "start": round(s, 3),
                    "end": round(e, 3),
                    "duration": round(e - s, 3),
                }
            )
        out = {
            "book": data["book"],
            "source": data["source"],
            "total_duration": total_dur,
            "segments": segments,
            "boundary_debug": state["boundaries"],
        }
        out_path = book_json.with_name(book_json.stem + ".aligned.json")
        out_path.write_text(json.dumps(out, indent=2))
        fb = sum(1 for b in state["boundaries"] if b["method"] == "fallback")
        logger.success(
            f"wrote {out_path} ({len(segments)} segments, {fb} fallback boundaries)"
        )
        return

    t0 = time.monotonic()
    processed = 0
    with Progress(
        SpinnerColumn(),
        TextColumn("[bold]boundaries"),
        BarColumn(bar_width=None),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        TimeRemainingColumn(),
        console=console,
    ) as progress:
        # n chapters have n - 1 internal boundaries; next_index starts at 1.
        task: TaskID = progress.add_task(
            "align", total=n - 1, completed=state["next_index"] - 1
        )
        while state["next_index"] < n and (time.monotonic() - t0) < time_budget:
            i: int = state["next_index"]
            expected: int | float = state["prev_actual"] + reported[i - 1]
            actual, window, method = find_boundary(
                mp3,
                expected,
                total_dur,
                noise_db,
                min_silence,
                base_window,
                max_window,
            )
            state["boundaries"].append(
                {
                    "chapter_index": i,
                    "expected": round(expected, ndigits=3),
                    "actual": round(actual, 3),
                    "window": window,
                    "method": method,
                }
            )
            state["prev_actual"] = actual
            state["next_index"] = i + 1
            processed += 1
            save_state(state_path, state)  # survive Ctrl-C mid-batch
            progress.update(task_id=task, completed=i)

    save_state(state_path, state)
    elapsed: int | float = time.monotonic() - t0
    logger.info(
        f"processed {processed} boundaries in {elapsed:.1f}s; "
        f"next_index={state['next_index']}/{n}"
    )
    if state["boundaries"]:
        last: Boundary = state["boundaries"][-1]
        logger.info(
            f"last: idx={last['chapter_index']} expected={last['expected']:.1f} "
            f"actual={last['actual']:.1f} method={last['method']}"
        )


if __name__ == "__main__":
    app()
