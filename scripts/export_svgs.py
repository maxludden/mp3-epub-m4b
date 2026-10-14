"""Export SVG screenshots of mp3-epub-m4b's terminal output, using synthetic data.

Nothing here touches ffmpeg, whisper or real audiobooks: each demo builds the
same Rich renderables the CLI prints (pairs table, chapter table, status panel,
highlighted log lines) from made-up data and records them with
``Console.save_svg``. Handy for the README and docs.

Usage::

    uv run python scripts/export_svgs.py [OUTPUT_DIR]

Writes ``pairs.svg``, ``chapters.svg``, ``status.svg``, ``overview.svg`` and
``log.svg`` to ``docs/img/`` by default.
"""

from __future__ import annotations

import io
from collections.abc import Callable
from pathlib import Path
from typing import Annotated

import typer
from loguru import logger
from rich.console import Console, RenderableType
from rich.terminal_theme import MONOKAI

import mp3_epub_m4b.log as log_module
from mp3_epub_m4b.report import chapter_table, pairs_table
from mp3_epub_m4b.state import Segment
from mp3_epub_m4b.status import BookStatus, Placement, build_view

DEFAULT_OUT = Path(__file__).resolve().parent.parent / "docs" / "img"
WIDTH = 100


def recording_console() -> Console:
    """A fixed-width console that records output (and discards the live terminal write)."""
    return Console(record=True, width=WIDTH, force_terminal=True, file=io.StringIO())


def save(console: Console, path: Path, title: str) -> None:
    """Write everything ``console`` recorded to ``path`` as an SVG window titled ``title``."""
    path.write_text(console.export_svg(title=title, theme=MONOKAI))
    typer.echo(f"wrote {path}")


def demo_pairs() -> RenderableType:
    """The table shown before a batch conversion asks for confirmation."""
    return pairs_table(
        [
            (f"Mistborn {n}", Path(f"Volume {n}.mp3"), Path(f"Mistborn_{n:02d}.epub"))
            for n in (1, 2, 3)
        ]
    )


def demo_chapters() -> RenderableType:
    """A planned chapter list with one low-confidence placement."""
    titles = ["Opening Credits", "Prologue", "Chapter 1", "Chapter 2", "Chapter 3", "Epilogue"]
    starts = [0.0, 31.4, 412.9, 1310.2, 2275.8, 3399.5]
    end = 3612.0
    segments: list[Segment] = []
    for i, (title, start) in enumerate(zip(titles, starts, strict=True)):
        nxt = starts[i + 1] if i + 1 < len(starts) else end
        seg: Segment = {
            "n": i,
            "title": title,
            "start": start,
            "end": nxt,
            "duration": round(nxt - start, 3),
        }
        if i >= 2:
            seg["words"] = int((nxt - start) * 2.6)
        if title == "Chapter 2":
            seg["note"] = "low confidence"
        segments.append(seg)
    return chapter_table(segments)


def demo_status() -> RenderableType:
    """A mid-run status panel: transcript alignment, a few chapters placed."""
    recent = [
        Placement(7, "Chapter 7", 5120.4, "heard"),
        Placement(8, "Chapter 8", 5871.0, "heard"),
        Placement(9, "Chapter 9", 6602.7, "low confidence"),
    ]
    st = BookStatus(
        work_dir=Path("/tmp/work/mistborn-1"),
        title="The Final Empire",
        stage="align",
        stages=[
            ("extract", "done"),
            ("transcribe", "done"),
            ("align", "running"),
            ("assemble", "pending"),
            ("deliver", "pending"),
        ],
        method="transcribe",
        chapters_total=38,
        chapters_placed=9,
        low_confidence=[9],
        recent=recent,
        detail="listening for the opening of chapter 10",
        done=9,
        total=38,
        unit="chapters",
        eta=312,
        elapsed=95,
        running=True,
        pid=48213,
        idle=2,
    )
    return build_view([st])[0]


def demo_overview() -> RenderableType:
    """The batch overview table: one finished, one running, one stopped book."""
    base = dict(work_dir=Path("/tmp/work"), stages=[])
    books = [
        BookStatus(title="The Final Empire", stage="done", running=False, **base),  # type: ignore[arg-type]
        BookStatus(
            title="The Well of Ascension",
            stage="align",
            done=14,
            total=52,
            unit="chapters",
            running=True,
            pid=48213,
            **base,  # type: ignore[arg-type]
        ),
        BookStatus(
            title="The Hero of Ages",
            stage="transcribe",
            done=3000,
            total=9000,
            unit="audio",
            running=False,
            **base,  # type: ignore[arg-type]
        ),
    ]
    return build_view(books)[0]


def demo_log(console: Console) -> None:
    """Emit loguru messages through the package's own sink, recorded on ``console``."""
    original = log_module.console
    log_module.console = console  # the sink prints to this module-level console
    try:
        log_module.setup_logging("DEBUG")
        logger.info("scanning '/Books/Mistborn/Volume 1.mp3' for silence")
        logger.info("transcribing 10:02:11 of audio with whisper.cpp")
        logger.success("matched 37 of 38 chapters; placed chapter 12 at 1:25:40 (score 0.94)")
        logger.warning("chapter 9: low confidence, guessed from the previous gap")
        logger.error("ffmpeg failed on 'Volume 2.mp3'; pass --verbose for details")
    finally:
        log_module.console = original
        logger.remove()


def main(
    output_dir: Annotated[Path, typer.Argument(help="Where to write the SVGs.")] = DEFAULT_OUT,
) -> None:
    """Render every demo and write its SVG into ``output_dir``."""
    output_dir.mkdir(parents=True, exist_ok=True)
    renderables: dict[str, tuple[str, Callable[[], RenderableType]]] = {
        "pairs": ("mp3-epub-m4b batch", demo_pairs),
        "chapters": ("mp3-epub-m4b convert", demo_chapters),
        "status": ("mp3-epub-m4b status", demo_status),
        "overview": ("mp3-epub-m4b status", demo_overview),
    }
    for name, (title, build) in renderables.items():
        console = recording_console()
        console.print(build())
        save(console, output_dir / f"{name}.svg", title)

    console = recording_console()
    demo_log(console)
    save(console, output_dir / "log.svg", "mp3-epub-m4b convert")


if __name__ == "__main__":
    typer.run(main)
