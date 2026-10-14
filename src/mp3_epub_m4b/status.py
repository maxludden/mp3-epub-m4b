"""Progress records and status reports for the pipeline's long-running stages.

While a book converts, the pipeline writes a small ``progress.json`` beside its
``state.json``. :func:`read_status` combines the two into a :class:`BookStatus`
(stage checklist, chapters placed, speed, ETA, whether the process is alive) and
:func:`render_status` draws it, so a run started in one terminal can be watched
from another.

``progress.json`` is best-effort: it is written atomically, at most once per
``min_interval`` seconds, and every reader tolerates it being missing or stale.
``state.json`` stays the source of truth for resuming.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypedDict

from loguru import logger
from rich.console import Group, RenderableType
from rich.panel import Panel
from rich.progress_bar import ProgressBar
from rich.table import Table
from rich.text import Text

from mp3_epub_m4b.log import highlighted
from mp3_epub_m4b.state import STAGES, PipelineState

PROGRESS_FILE = "progress.json"
RECENT_CHAPTERS = 5  # how many of the latest placed chapters the report lists
AUDIO_UNIT = "audio"  # progress unit: seconds of audio (shown as H:MM:SS)

StatusFn = Callable[[str, float | None, float | None], None]
"""Callback ``(detail, done, total)`` the pipeline calls with a human-readable status line."""


class ProgressRecord(TypedDict):
    """Contents of ``progress.json``."""

    pid: int
    stage: str
    detail: str
    done: float | None
    total: float | None
    unit: str  # "chapters", "audio" (seconds of audio) or ""
    started_at: float  # when this process began the stage
    base_done: float  # ``done`` at that moment, so a resumed stage reports its own speed
    updated_at: float


class ProgressWriter:
    """Throttled, atomic writer for ``progress.json``.

    Attributes:
        path: Where the record is written.
        min_interval: Fewest seconds between unforced writes.
    """

    def __init__(
        self, path: Path, min_interval: float = 1.0, clock: Callable[[], float] = time.time
    ) -> None:
        self.path = path
        self.min_interval = min_interval
        self._clock = clock
        self._last_write = float("-inf")
        self._stage: str | None = None
        self._started_at = 0.0
        self._base_done = 0.0
        self._done: float | None = None
        self._total: float | None = None
        self._unit = ""
        self._detail = ""

    def update(
        self,
        stage: str,
        *,
        detail: str | None = None,
        done: float | None = None,
        total: float | None = None,
        unit: str | None = None,
        force: bool = False,
    ) -> None:
        """Record the current status; fields left as ``None`` keep their last value.

        A change of ``stage`` always writes, and restarts the speed baseline.

        Args:
            stage: Current pipeline stage.
            detail: Short human-readable line about what is happening now.
            done: Amount completed so far.
            total: Amount expected in all.
            unit: ``"chapters"``, ``"audio"`` or ``""``.
            force: Write even if one was written less than ``min_interval`` ago.
        """
        now = self._clock()
        changed = stage != self._stage
        if changed:
            self._stage, self._started_at = stage, now
            self._done = self._total = None
            self._unit = self._detail = ""
        if detail is not None:
            self._detail = detail
        if unit is not None:
            self._unit = unit
        if total is not None:
            self._total = total
        if done is not None:
            if changed or self._done is None:
                self._base_done = done
            self._done = done
        if not (force or changed) and now - self._last_write < self.min_interval:
            return
        record: ProgressRecord = {
            "pid": os.getpid(),
            "stage": stage,
            "detail": self._detail,
            "done": self._done,
            "total": self._total,
            "unit": self._unit,
            "started_at": self._started_at,
            "base_done": self._base_done,
            "updated_at": now,
        }
        tmp = self.path.with_suffix(".json.tmp")
        try:
            tmp.write_text(json.dumps(record))
            tmp.replace(self.path)
            self._last_write = now
        except OSError as e:  # progress is advisory: never fail a conversion over it
            logger.debug(f"could not write {self.path.name}: {e}")


def read_progress(path: Path) -> ProgressRecord | None:
    """Read ``progress.json``; ``None`` if it is missing, unreadable or half-written."""
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or "stage" not in data or "updated_at" not in data:
        return None
    try:
        updated = float(data["updated_at"])
        rec: ProgressRecord = {
            "pid": int(data.get("pid", 0)),
            "stage": str(data["stage"]),
            "detail": str(data.get("detail", "")),
            "done": data.get("done"),
            "total": data.get("total"),
            "unit": str(data.get("unit", "")),
            "started_at": float(data.get("started_at") or updated),
            "base_done": float(data.get("base_done") or 0.0),
            "updated_at": updated,
        }
    except (TypeError, ValueError):  # a hand-edited or damaged record counts as missing
        return None
    return rec


def pid_alive(pid: int) -> bool:
    """Whether a process with this id exists (signal 0 probes without delivering anything)."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # exists, but belongs to someone else
        return True
    return True


@dataclass(frozen=True)
class Placement:
    """One chapter already located in the audio."""

    n: int
    title: str
    start: float
    note: str


@dataclass(frozen=True)
class BookStatus:
    """Everything the status report shows about one book's conversion.

    Attributes:
        work_dir: The book's work folder.
        title: Book title (from the EPUB) or the folder name before extraction.
        stage: Current stage, or ``"done"``.
        stages: ``(stage, state)`` for each stage, state being ``done``, ``running`` or ``pending``.
        method: ``"silence"``, ``"transcribe"`` or ``""`` before alignment starts.
        chapters_total: Chapters in the EPUB (0 before extraction).
        chapters_placed: Chapters located so far.
        unmatched: Chapters whose opening was never heard (transcribe mode).
        low_confidence: Chapters placed with low confidence.
        fallback: Chapters placed by a fixed guess (silence mode).
        recent: The latest placed chapters, oldest first.
        detail: What the pipeline last said it was doing.
        done: Progress through the current stage, in ``unit``.
        total: Size of the current stage, in ``unit``.
        unit: ``"chapters"``, ``"audio"`` or ``""``.
        eta: Estimated seconds left in the current stage, if measurable.
        elapsed: Seconds this process has spent in the current stage.
        running: Whether the recording process is alive; ``None`` if unknown.
        pid: That process id, if recorded.
        idle: Seconds since the last progress update.
        output: Delivered M4B path, once done.
        warnings: Notes the reader wants to surface (for example, a stale record).
    """

    work_dir: Path
    title: str
    stage: str
    stages: list[tuple[str, str]]
    method: str = ""
    chapters_total: int = 0
    chapters_placed: int = 0
    unmatched: list[int] = field(default_factory=list)
    low_confidence: list[int] = field(default_factory=list)
    fallback: list[int] = field(default_factory=list)
    recent: list[Placement] = field(default_factory=list)
    detail: str = ""
    done: float | None = None
    total: float | None = None
    unit: str = ""
    eta: float | None = None
    elapsed: float | None = None
    running: bool | None = None
    pid: int | None = None
    idle: float | None = None
    output: str | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def fraction(self) -> float | None:
        """Share (0-1) of the current stage that is finished, if measurable."""
        if self.done is None or not self.total:
            return None
        return max(0.0, min(1.0, self.done / self.total))


def _stage_states(state: PipelineState) -> list[tuple[str, str]]:
    """The stage checklist for a checkpoint (``readaloud`` shown only when it applies)."""
    stage = state.get("stage", "extract")
    with_readaloud = stage == "readaloud" or "readaloud_output" in state
    names = STAGES if with_readaloud else STAGES[:-1]
    if stage == "done":
        return [(s, "done") for s in names]
    current = names.index(stage) if stage in names else -1
    return [
        (s, "done" if i < current else "running" if i == current else "pending")
        for i, s in enumerate(names)
    ]


def _placements(state: PipelineState) -> tuple[int, list[Placement]]:
    """Chapters handled so far (placed or given up on), and the latest few that were placed."""
    align, epub = state.get("align"), state.get("epub")
    if align is None or epub is None:
        return 0, []
    chapters = epub["chapters"]
    unmatched = set(align.get("unmatched", []))
    low = set(align["low_confidence"])
    fallback = set(align["fallback"])
    placed = sorted(int(k) for k in align["boundaries"] if int(k) not in unmatched)
    recent = []
    for n in placed[-RECENT_CHAPTERS:]:
        note = "fallback" if n in fallback else "low confidence" if n in low else ""
        recent.append(Placement(n, chapters[n - 1]["title"], align["boundaries"][str(n)], note))
    return align["next"] - 1, recent


def _estimate_eta(
    rec: ProgressRecord | None, done: float | None, total: float | None
) -> float | None:
    """Seconds left in the stage at the speed this process has shown so far."""
    if rec is None or done is None or not total:
        return None
    spent = rec["updated_at"] - rec["started_at"]
    gained = done - rec["base_done"]
    if spent < 5 or gained <= 0:
        return None
    return max(0.0, (total - done) / (gained / spent))


def read_status(
    work_dir: Path,
    *,
    now: float | None = None,
    alive: Callable[[int], bool] = pid_alive,
) -> BookStatus | None:
    """Summarize a book's conversion from its work folder.

    Args:
        work_dir: A book's work folder (holds ``state.json``).
        now: Current time (s since the epoch); defaults to the clock.
        alive: Process-liveness test, replaceable in tests.

    Returns:
        The status, or ``None`` if the folder has no readable checkpoint.
    """
    try:
        state: PipelineState = json.loads((work_dir / "state.json").read_text())
    except (OSError, ValueError):
        return None
    now = time.time() if now is None else now
    stage = state.get("stage", "extract")
    epub = state.get("epub")
    align = state.get("align")
    rec = read_progress(work_dir / PROGRESS_FILE)
    placed, recent = _placements(state)
    total_chapters = len(epub["chapters"]) if epub else 0

    done: float | None = None
    total: float | None = None
    unit = ""
    detail = ""
    if stage == "align" and total_chapters:
        done, total, unit = float(placed), float(total_chapters), "chapters"
    if rec is not None and rec["stage"] == stage:
        detail = rec["detail"]
        if unit == "" and rec["total"]:
            done, total, unit = rec["done"], rec["total"], rec["unit"]
    if stage == "deliver" and state.get("assembled_path"):
        try:
            total = Path(state["assembled_path"]).stat().st_size / 1e6
            done, unit = state.get("deliver_bytes_done", 0) / 1e6, "MB"
        except OSError:
            pass

    warnings: list[str] = []
    running: bool | None = None
    pid = idle = elapsed = None
    if rec is not None:
        pid, idle = rec["pid"], max(0.0, now - rec["updated_at"])
        elapsed = max(0.0, rec["updated_at"] - rec["started_at"]) if rec["stage"] == stage else None
        if stage != "done":
            running = alive(rec["pid"])
            if running and idle > 600:
                warnings.append(
                    f"alive but silent for {idle / 60:.0f} min; a single window may be slow"
                )
    if stage != "done" and rec is None:
        warnings.append(
            "no progress record yet; the run may not have started or is an older version"
        )

    return BookStatus(
        work_dir=work_dir,
        title=(epub["meta"]["title"] if epub else "") or work_dir.name,
        stage=stage,
        stages=_stage_states(state),
        method=align.get("method", "") if align else "",
        chapters_total=total_chapters,
        chapters_placed=placed,
        unmatched=list(align.get("unmatched", [])) if align else [],
        low_confidence=list(align["low_confidence"]) if align else [],
        fallback=list(align["fallback"]) if align else [],
        recent=recent,
        detail=detail,
        done=done,
        total=total,
        unit=unit,
        eta=_estimate_eta(rec, done, total) if rec and rec["stage"] == stage else None,
        elapsed=elapsed,
        running=running,
        pid=pid,
        idle=idle,
        output=state.get("final_output"),
        warnings=warnings,
    )


def read_all_status(root: Path, **kw: Any) -> list[BookStatus]:
    """Status of every book folder directly under ``root``, most recently active first."""
    found: list[tuple[float, BookStatus]] = []
    for d in root.iterdir() if root.is_dir() else []:
        try:
            stamp = (d / "state.json").stat().st_mtime
        except OSError:
            continue
        if st := read_status(d, **kw):
            found.append((stamp, st))
    return [st for _t, st in sorted(found, key=lambda t: t[0], reverse=True)]


def fmt_duration(seconds: float) -> str:
    """Format a duration as ``45s``, ``12m 05s`` or ``1h 03m``."""
    s = round(seconds)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m {s % 60:02d}s"
    return f"{s // 3600}h {s % 3600 // 60:02d}m"


def _clock(seconds: float) -> str:
    """Format seconds as ``H:MM:SS``."""
    s = round(seconds)
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}"


def _amount(value: float, unit: str) -> str:
    """Format one progress quantity in its unit."""
    if unit == AUDIO_UNIT:
        return _clock(value)
    if unit == "MB":
        return f"{value:,.0f} MB"
    return f"{value:.0f}"


_ICON = {
    "done": "[green]✓[/green]",
    "running": "[bold #00aaff]▶[/bold #00aaff]",
    "pending": "[dim]·[/dim]",
}


def render_status(st: BookStatus) -> RenderableType:
    """Draw one book's status as a panel: stage checklist, progress bar, chapters and liveness."""
    parts: list[RenderableType] = [
        Text.from_markup("   ".join(f"{_ICON[s]} {name}" for name, s in st.stages))
    ]
    if st.stage == "done":
        parts.append(highlighted(f"Finished: {st.output}" if st.output else "Finished", "green"))
    else:
        label = {"chapters": "chapters", "audio": "audio", "MB": "copied"}.get(st.unit, "")
        if st.fraction is not None and st.done is not None and st.total:
            bar = ProgressBar(total=st.total, completed=st.done, width=40)
            line = Text(
                f" {st.fraction:.0%}  {_amount(st.done, st.unit)} / {_amount(st.total, st.unit)} {label}"
            )
            row = Table.grid(padding=(0, 1))
            row.add_row(bar, line)
            parts.append(row)
        if st.detail:
            parts.append(highlighted(st.detail, "#00aaff"))
        timing = []
        if st.elapsed is not None:
            timing.append(f"elapsed {fmt_duration(st.elapsed)}")
        if st.eta is not None:
            timing.append(f"about {fmt_duration(st.eta)} left in this stage")
        if timing:
            parts.append(highlighted("  ·  ".join(timing), "dim"))
    if st.chapters_total and st.stage in {"align", "assemble", "deliver", "readaloud", "done"}:
        how = {"transcribe": "by transcript", "silence": "by silence"}.get(st.method, "")
        bits = [f"{st.chapters_placed}/{st.chapters_total} chapters placed {how}".strip()]
        if st.unmatched:
            bits.append(f"{len(st.unmatched)} unmatched ({', '.join(map(str, st.unmatched))})")
        if st.fallback:
            bits.append(f"{len(st.fallback)} guessed")
        if st.low_confidence:
            bits.append(f"{len(st.low_confidence)} low confidence")
        parts.append(highlighted("  ·  ".join(bits)))
    if st.recent:
        table = Table(show_header=True, header_style="dim", box=None, pad_edge=False)
        table.add_column("#", justify="right")
        table.add_column("Chapter")
        table.add_column("Starts", justify="right")
        table.add_column("")
        for p in st.recent:
            table.add_row(str(p.n), p.title, _clock(p.start), p.note)
        parts.append(table)
    if st.running is not None and st.stage != "done":
        if st.running:
            ago = f", updated {fmt_duration(st.idle)} ago" if st.idle is not None else ""
            parts.append(highlighted(f"running (pid {st.pid}{ago})", "green"))
        else:
            ago = f" {fmt_duration(st.idle)} ago" if st.idle is not None else ""
            parts.append(
                Text(
                    f"not running; last update{ago}. Re-run the same command to resume.",
                    style="yellow",
                )
            )
    for w in st.warnings:
        parts.append(Text(f"note: {w}", style="yellow"))
    return Panel(Group(*parts), title=st.title, title_align="left", border_style="dim")


def render_overview(statuses: list[BookStatus]) -> RenderableType:
    """A compact table of several books (for a batch), one row each."""
    table = Table(title="Conversions", title_justify="left")
    for col in ("Book", "Stage", "Progress", "State"):
        table.add_column(col)
    for st in statuses:
        pct = f"{st.fraction:.0%}" if st.fraction is not None else ""
        state = (
            "finished"
            if st.stage == "done"
            else "running"
            if st.running
            else "stopped"
            if st.running is False
            else "?"
        )
        table.add_row(st.title, st.stage, pct, state)
    return table


def build_view(statuses: list[BookStatus]) -> tuple[RenderableType, bool]:
    """Compose the status report for one or several books.

    Args:
        statuses: The books to show.

    Returns:
        ``(renderable, active)``: one book gets its full panel; several get an
        overview table plus a panel for each book still running. ``active`` is
        true while any book is running, so ``--watch`` knows when to stop.
    """
    if not statuses:
        return Text("No conversions found.", style="yellow"), False
    active = any(s.running and s.stage != "done" for s in statuses)
    if len(statuses) == 1:
        return render_status(statuses[0]), active
    panels = [render_status(s) for s in statuses if s.running and s.stage != "done"]
    return Group(render_overview(statuses), *panels), active
