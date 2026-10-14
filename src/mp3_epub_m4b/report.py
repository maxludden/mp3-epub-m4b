"""Human-readable reports: the chapter table and the output verification summary."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from rich.table import Table

from mp3_epub_m4b.chapters import deviating_segments
from mp3_epub_m4b.ffmpeg_tools import probe_summary
from mp3_epub_m4b.state import Segment

DURATION_TOLERANCE = 3.0  # seconds the output may differ from the source before we warn


def fmt_time(seconds: float) -> str:
    """Format seconds as ``H:MM:SS``."""
    total = round(seconds)
    return f"{total // 3600}:{total % 3600 // 60:02d}:{total % 60:02d}"


def chapter_table(segments: Sequence[Segment]) -> Table:
    """Build a table of the planned chapters.

    Columns: number, title, start, duration, EPUB word count, and a note for
    chapters placed with low confidence, by fallback, or whose length is far
    from what their word count predicts.
    """
    flagged = {n for n, _title, _actual, _expected in deviating_segments(segments)}
    table = Table(title="Chapters", title_justify="left")
    table.add_column("#", justify="right")
    table.add_column("Title")
    table.add_column("Start", justify="right")
    table.add_column("Length", justify="right")
    table.add_column("Words", justify="right")
    table.add_column("Note")
    for i, seg in enumerate(segments, start=1):
        notes = [note] if (note := seg.get("note")) else []
        if seg["n"] in flagged:
            notes.append("length vs words")
        table.add_row(
            str(i),
            seg["title"],
            fmt_time(seg["start"]),
            fmt_time(seg["duration"]),
            str(seg["words"]) if "words" in seg else "",
            ", ".join(notes),
            style="yellow" if notes else None,
        )
    if segments:
        table.caption = f"{len(segments)} chapters, {fmt_time(segments[-1]['end'])} total"
    return table


@dataclass
class Verification:
    """Result of probing a finished M4B against its source.

    Attributes:
        chapters: Chapter count in the M4B.
        duration: M4B duration (s).
        source_duration: Source audio duration (s).
        streams: Stream count (audio + cover + chapter track is typical).
        expected_chapters: Chapters the pipeline planned, if known.
    """

    chapters: int
    duration: float
    source_duration: float
    streams: int
    expected_chapters: int | None = None

    @property
    def problems(self) -> list[str]:
        """What looks wrong, if anything (empty when the output checks out)."""
        found = []
        if self.chapters == 0:
            found.append("the M4B has no chapters")
        elif self.expected_chapters is not None and self.chapters != self.expected_chapters:
            found.append(f"expected {self.expected_chapters} chapters but found {self.chapters}")
        if abs(self.duration - self.source_duration) > DURATION_TOLERANCE:
            found.append(
                f"duration is {self.duration:.1f}s but the source is {self.source_duration:.1f}s"
            )
        return found

    def summary(self) -> str:
        """One-line summary for the console."""
        return (
            f"chapters: {self.chapters} | duration: {fmt_time(self.duration)} "
            f"(source {fmt_time(self.source_duration)}) | streams: {self.streams}"
        )


def verify_output(
    output: Path, source_duration: float, expected_chapters: int | None = None
) -> Verification:
    """Probe ``output`` and compare it with the source.

    Args:
        output: The finished ``.m4b``.
        source_duration: Duration (s) of the source audio.
        expected_chapters: Chapter count the pipeline planned, if known.

    Raises:
        FFmpegError: If ffprobe fails on the output.
    """
    probed = probe_summary(output)
    return Verification(
        probed.chapters, probed.duration, source_duration, probed.streams, expected_chapters
    )
