"""MCP server: lets Claude find, start, and monitor audiobook conversions.

    mp3-epub-m4b-mcp            # stdio server, started by an MCP client such as Claude

Over stdio, **stdout is the protocol channel**, and the pipeline prints progress
bars and tables to it. So conversions never run in this process: ``start_conversion``
launches the regular ``convert`` command as a detached subprocess whose output goes
to a log file, and ``conversion_status`` reads the checkpoint (``state.json``) the
pipeline already writes. A conversion can take hours, so it also must not block a
single tool call.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

from loguru import logger
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from mp3_epub_m4b.chapters import deviating_segments
from mp3_epub_m4b.converter import low_space, stage_progress
from mp3_epub_m4b.ffmpeg_tools import FFmpegError, probe_summary
from mp3_epub_m4b.log import console
from mp3_epub_m4b.options import (
    AUDIO_SUFFIXES,
    DEFAULT_AUDIO_PATTERN,
    DEFAULT_EPUB_PATTERN,
    DEFAULT_WORK,
)
from mp3_epub_m4b.pairing import find_epub, pair_folder
from mp3_epub_m4b.run import work_name
from mp3_epub_m4b.state import STAGES, PipelineState, Segment, read_state

mcp = MCPServer(
    "mp3-epub-m4b",
    instructions=(
        "Convert single-file mp3/m4a audiobooks plus EPUBs into chaptered M4B files. "
        "Use find_pairs to see what a folder holds, start_conversion to begin (it returns "
        "immediately; conversions take a long time), then poll conversion_status. "
        "Re-running start_conversion after an interruption resumes where it left off."
    ),
)

_LOG_TAIL_LINES = 15
_EDGE_CHAPTERS = 3  # chapters shown at each end of the book unless detail is requested


def _work_root() -> Path:
    """The scratch root, honouring ``MP3_EPUB_M4B_WORK`` exactly like the CLI does."""
    return Path(os.environ.get("MP3_EPUB_M4B_WORK", DEFAULT_WORK)).expanduser()


def _resolve(path: str, *, kind: str, directory: bool = False) -> Path:
    """Expand and resolve a user-supplied path, checking that it exists.

    Raises:
        ToolError: If the path is missing or is the wrong kind (file vs directory).
    """
    p = Path(path).expanduser().resolve()
    if not p.exists():
        raise ToolError(f"{kind} not found: {p}")
    if directory != p.is_dir():
        raise ToolError(f"{kind} must be a {'directory' if directory else 'file'}: {p}")
    return p


def _epub_for(book: Path) -> Path:
    """The EPUB for ``book``: itself if it is an EPUB, else the one named like the audio file."""
    if book.suffix.lower() == ".epub":
        return book
    try:
        return find_epub(book)
    except FileNotFoundError as e:
        raise ToolError(str(e)) from e


def _job_files(epub: Path) -> tuple[Path, Path]:
    """``(job record, log file)`` for a book.

    They live beside the work folders rather than inside one, because ``--fresh``
    deletes the book's work folder when the conversion starts.
    """
    jobs = _work_root() / ".mcp-jobs"
    name = work_name(epub.stem)
    return jobs / f"{name}.json", jobs / f"{name}.log"


def _pid_alive(pid: int) -> bool:
    """Whether a process with this pid exists (signal 0 only checks, it sends nothing)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # exists, but belongs to someone else
        return True
    return True


def _read_job(epub: Path) -> dict[str, Any] | None:
    """The saved job record for a book, or ``None`` if none was started (or it is unreadable)."""
    record, _ = _job_files(epub)
    try:
        data: dict[str, Any] = json.loads(record.read_text())
    except (OSError, ValueError):
        return None
    return data


def _chapter(seg: Segment) -> dict[str, Any]:
    """One chapter as Claude sees it."""
    return {
        "n": seg["n"],
        "title": seg["title"],
        "start": round(seg["start"], 1),
        "end": round(seg["end"], 1),
    }


def summarize_segments(segments: list[Segment], detail: bool = False) -> dict[str, Any]:
    """Condense the planned chapters into something worth sending to Claude.

    A book can have hundreds of chapters, so by default this reports the totals,
    the first and last :data:`_EDGE_CHAPTERS` chapters, and the chapters whose
    length looks wrong. With ``detail`` it lists every chapter instead.
    """
    summary: dict[str, Any] = {
        "chapter_count": len(segments),
        "total_seconds": round(segments[-1]["end"], 1) if segments else 0.0,
        "suspicious_chapters": [
            {"n": n, "title": title, "actual_seconds": actual, "expected_seconds": expected}
            for n, title, actual, expected in deviating_segments(segments)
        ],
    }
    if detail or len(segments) <= 2 * _EDGE_CHAPTERS:
        summary["chapters"] = [_chapter(s) for s in segments]
    else:
        summary["first_chapters"] = [_chapter(s) for s in segments[:_EDGE_CHAPTERS]]
        summary["last_chapters"] = [_chapter(s) for s in segments[-_EDGE_CHAPTERS:]]
    return summary


def summarize_state(state: PipelineState, detail: bool = False) -> dict[str, Any]:
    """Condense a checkpoint into the fields a status poll needs."""
    done, total = stage_progress(state)
    stage = state["stage"]
    summary: dict[str, Any] = {
        "stage": stage,
        "stage_number": f"{STAGES.index(stage) + 1}/{len(STAGES)}" if stage in STAGES else "done",
        "stage_progress": {"completed": round(done, 1), "total": total},
    }
    if "segments" in state:
        summary["chapters"] = summarize_segments(state["segments"], detail)
    align = state.get("align")
    if align is not None:
        summary["low_confidence_chapters"] = align["low_confidence"]
        summary["fallback_chapters"] = align["fallback"]
    for key in ("final_output", "readaloud_output"):
        if key in state:
            summary[key] = state[key]
    return summary


def _log_tail(log: Path) -> list[str]:
    """The last few lines of the job log (a state mismatch only ever shows up here)."""
    try:
        return log.read_text(errors="replace").splitlines()[-_LOG_TAIL_LINES:]
    except OSError:
        return []


@mcp.tool()
def find_pairs(
    folder: str,
    audio_pattern: str = DEFAULT_AUDIO_PATTERN,
    epub_pattern: str = DEFAULT_EPUB_PATTERN,
) -> dict[str, Any]:
    """List the audio + EPUB pairs that can be converted in a folder.

    Pairs are matched by number ('Volume 3.mp3' + 'Title_03.epub' by default) or by
    identical name ('Book.mp3' + 'Book.epub'). Not recursive.

    Args:
        folder: Directory to scan.
        audio_pattern: Regex with a number group, fully matching an audio file's stem.
        epub_pattern: Regex with a number group, searched in an EPUB's stem.
    """
    root = _resolve(folder, kind="folder", directory=True)
    try:
        found = pair_folder(root, audio_pattern, epub_pattern)
    except re.error as e:
        raise ToolError(f"invalid pattern: {e}") from e
    return {
        "pairs": [
            {"label": label, "audio": str(audio), "epub": str(epub)}
            for label, audio, epub in found.jobs
        ],
        "audio_without_epub": [str(p) for p in found.unmatched_audio.values()],
        "epub_without_audio": [str(p) for p in found.unmatched_epubs.values()],
    }


@mcp.tool()
def start_conversion(
    audio: str,
    epub: str | None = None,
    out: str | None = None,
    dry_run: bool = False,
    readaloud: bool = True,
    overwrite: bool = False,
    fresh: bool = False,
    bitrate: str = "96k",
    detect_intro: bool = False,
    detect_outro: bool = False,
) -> dict[str, Any]:
    """Start converting one audiobook in the background and return immediately.

    Poll conversion_status for progress. Re-running after an interruption resumes
    from the saved checkpoint. With ``dry_run`` only the chapters are placed (no
    encoding), which is the quick way to preview them in conversion_status.

    Args:
        audio: The .mp3/.m4a file.
        epub: The book's EPUB (default: the .epub named like the audio, beside it).
        out: Output directory (default: an ``m4b`` folder beside the audio).
        dry_run: Only place chapters; don't encode.
        readaloud: Also build a readaloud EPUB 3 after the M4B is delivered.
        overwrite: Replace an existing .m4b in the output directory.
        fresh: Discard saved progress and start over.
        bitrate: AAC bitrate.
        detect_intro: Split opening credits into their own chapter.
        detect_outro: Split end credits into their own chapter.
    """
    audio_path = _resolve(audio, kind="audio file")
    if audio_path.suffix.lower() not in AUDIO_SUFFIXES:
        raise ToolError(f"audio must be one of {sorted(AUDIO_SUFFIXES)}: {audio_path.name}")
    epub_path = _resolve(epub, kind="epub") if epub else _epub_for(audio_path)
    out_dir = Path(out).expanduser().resolve() if out else audio_path.parent / "m4b"

    job = _read_job(epub_path)
    if job is not None and _pid_alive(job["pid"]):
        raise ToolError(f"{epub_path.stem} is already converting (pid {job['pid']})")
    if not dry_run:
        for label, free in low_space(_work_root(), out_dir):
            raise ToolError(f"only {free:.0f}GB free on the {label} volume")

    cmd = [
        sys.executable, "-m", "mp3_epub_m4b.cli", "convert", str(audio_path), str(epub_path),
        "--out", str(out_dir), "--bitrate", bitrate,
        "--readaloud" if readaloud else "--no-readaloud",
        "--detect-intro" if detect_intro else "--no-detect-intro",
        "--detect-outro" if detect_outro else "--no-detect-outro",
    ]  # fmt: skip
    for flag, on in (("--overwrite", overwrite), ("--fresh", fresh), ("--dry-run", dry_run)):
        if on:
            cmd.append(flag)

    record, log = _job_files(epub_path)
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("wb") as sink:
        # stdin=DEVNULL: nothing may prompt. start_new_session: outlive this server.
        proc = subprocess.Popen(
            cmd, stdin=subprocess.DEVNULL, stdout=sink, stderr=subprocess.STDOUT,
            start_new_session=True,
        )  # fmt: skip
    record.write_text(json.dumps({"pid": proc.pid, "cmd": cmd, "dry_run": dry_run}))
    logger.info(f"started pid {proc.pid}: {audio_path.name}")
    return {
        "pid": proc.pid,
        "epub": str(epub_path),
        "out_dir": str(out_dir),
        "work_dir": str(_work_root() / work_name(epub_path.stem)),
        "log": str(log),
    }


@mcp.tool()
def conversion_status(book: str, detail: bool = False) -> dict[str, Any]:
    """Report how far a conversion has got.

    Args:
        book: The audio file or EPUB of the book (either path identifies it).
        detail: List every planned chapter (n, title, start, end) instead of just the
            first and last few; use it to check chapter placement after a dry run.
    """
    epub = _epub_for(_resolve(book, kind="book"))
    work = _work_root() / work_name(epub.stem)
    job = _read_job(epub)
    _, log = _job_files(epub)
    status: dict[str, Any] = {
        "running": job is not None and _pid_alive(job["pid"]),
        "work_dir": str(work),
        "log_tail": _log_tail(log),
    }
    try:
        status.update(summarize_state(read_state(work / "state.json"), detail))
    except (OSError, ValueError):
        status["stage"] = None  # not started yet, or the first checkpoint isn't written
    return status


@mcp.tool()
def verify_m4b(path: str) -> dict[str, Any]:
    """Probe a finished .m4b with ffprobe: chapter count, duration and stream count.

    Args:
        path: The .m4b file.
    """
    m4b = _resolve(path, kind="m4b")
    try:
        probed = probe_summary(m4b)
    except (FFmpegError, ValueError, KeyError) as e:
        raise ToolError(f"could not probe {m4b.name}: {e}") from e
    return {
        "path": str(m4b),
        "chapters": probed.chapters,
        "duration_seconds": round(probed.duration, 1),
        "streams": probed.streams,
    }


def main() -> None:
    """Console-script entry point: serve over stdio, logging to stderr only."""
    console.file = sys.stderr  # belt and braces: nothing in-process may print to stdout
    logger.enable("mp3_epub_m4b")
    logger.remove()
    logger.add(sys.stderr, level=os.environ.get("MP3_EPUB_M4B_LOG_LEVEL", "INFO").upper())
    mcp.run()


if __name__ == "__main__":
    main()
