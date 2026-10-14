"""Thin ffmpeg/ffprobe helpers: run a tool, probe a duration, scan for silences."""

from __future__ import annotations

import json
import re
import subprocess
from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import NamedTuple

from loguru import logger

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
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ]
    )
    return float(out.stdout.strip())


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
        "ffmpeg",
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "info",
        "-nostats",
        "-progress",
        "pipe:2",
        "-i",
        str(path),
        "-vn",
        "-af",
        f"silencedetect=noise={noise_db}dB:d={min_silence}",
        "-f",
        "null",
        "-",
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


class OutputSummary(NamedTuple):
    """What ``ffprobe`` reports about a finished M4B."""

    chapters: int
    duration: float
    streams: int


def probe_summary(path: Path) -> OutputSummary:
    """Count a media file's chapters and streams and read its duration.

    Args:
        path: Media file to probe.

    Returns:
        Chapter count, duration in seconds and stream count.

    Raises:
        FFmpegError: If ffprobe fails.
        ValueError: If ffprobe's output can't be parsed.
    """
    out = run_tool(
        ["ffprobe", "-v", "error", "-show_chapters", "-show_format", "-of", "json", str(path)]
    )
    data = json.loads(out.stdout)
    fmt = data["format"]
    return OutputSummary(
        chapters=len(data.get("chapters", [])),
        duration=float(fmt["duration"]),
        streams=int(fmt["nb_streams"]),
    )
