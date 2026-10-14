"""Shared Rich console and loguru setup for the command-line tools.

The library modules log through loguru (disabled by default; see the package
``__init__``). Each CLI entry point calls :func:`setup_logging` to route those
messages to :data:`console`, which is also what the progress bars render on, so
log lines appear cleanly above the bars.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING

from loguru import logger
from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.traceback import Traceback
from rich.traceback import install as install_rich_traceback

if TYPE_CHECKING:
    from loguru import Message, Record

console = Console()

_ICONS = {
    "TRACE": "·",
    "DEBUG": "·",
    "INFO": "ℹ",
    "SUCCESS": "✔",
    "WARNING": "⚠",
    "ERROR": "✖",
    "CRITICAL": "‼",
}
_ASCII_ICONS = {
    "TRACE": ".",
    "DEBUG": ".",
    "INFO": "i",
    "SUCCESS": "+",
    "WARNING": "!",
    "ERROR": "x",
    "CRITICAL": "!",
}
_OFF = {"0", "false", "no", "off"}

_STYLES = {
    "DEBUG": "dim",
    "INFO": "#00aaff",
    "SUCCESS": "b #00ff00",
    "WARNING": "i yellow",
    "ERROR": "i #FF8C00",
    "CRITICAL": "bold red",
}


# (pattern, style) pairs applied in order; where matches overlap, later ones win,
# so broad patterns come first and specific ones (paths, quotes) last. Styles are
# given directly rather than as theme names, so they work on any Console.
_AUDIO_FILES = r"mp3|m4a|m4b|epub|json|jpe?g|png|log"
_HIGHLIGHTS: tuple[tuple[str, str], ...] = (
    (r"(?<![\w.])\d+(?:[.,]\d+)*(?:\s?(?:ms|s|m|h|min|kB|MB|GB|%))?(?!\w)", "magenta"),
    (r"(?<![\w:])\d+:\d{2}:\d{2}(?![\w:])", "bold magenta"),
    (r"(?<=score )\d\.\d+", "bold magenta"),
    (
        r"\b(?:heard|matched|placed|found|extracted|assembled|delivered|finished|done)\b",
        "bold green",
    ),
    (
        r"\b(?:unmatched|not heard|failed|fallback|guess(?:ed)?|low confidence|interpolat\w+"
        r"|not running|stopped)\b",
        "bold #ff8700",
    ),
    (
        r"\b(?:switching|listening|scanning|encoding|transcribing|resuming|running)\b",
        "bold #00aaff",
    ),
    (r"\b(?:transcript|silence|whisper\.cpp|whisper|ffmpeg)\b", "italic"),
    (r"(?<![\w-])--[a-z][a-z-]*", "bold yellow"),
    (r"'[^'\n]*'|\"[^\"\n]*\"", "green"),
    # lazily extend over spaces so "/Brandon Sanderson/Mistborn/1 - Book.mp3" is one path
    (rf"(?:~|/)[^\s'\"]*?(?: [^\s'\"]+?)*?\.(?:{_AUDIO_FILES})(?!\w)", "underline cyan"),
)
_COMPILED = tuple((re.compile(pat, re.IGNORECASE), style) for pat, style in _HIGHLIGHTS)


def highlight_text(text: Text) -> Text:
    """Colour the numbers, times, scores, paths, flags, quoted titles and key words in ``text``.

    The text's own style stays underneath, so a line's base colour (its log
    level) still shows between the highlights. Returns ``text`` for chaining.
    """
    for pattern, style in _COMPILED:
        text.highlight_regex(pattern, style)
    return text


def highlighted(message: str, style: str = "") -> Text:
    """``message`` as a :class:`~rich.text.Text` with ``style`` as the base and highlights on top."""
    return highlight_text(Text(message, style=style))


def _timestamps_default() -> bool:
    """Whether log lines carry a time, from ``MP3_EPUB_M4B_LOG_TIME`` (on unless 0/false/no/off)."""
    return os.environ.get("MP3_EPUB_M4B_LOG_TIME", "1").strip().lower() not in _OFF


def _make_sink(timestamps: bool) -> Callable[[Message], None]:
    """Build a loguru sink that prints each record to :data:`console` as ``time icon message``.

    The line is a borderless grid, so a message that wraps keeps its hanging
    indent under the text instead of running back under the time and icon.
    Looks up :data:`console` per message so it can be swapped (e.g. in tests).
    """

    def sink(message: Message) -> None:
        record: Record = message.record
        name = record["level"].name
        style = _STYLES.get(name, "")
        icons = _ASCII_ICONS if console.options.ascii_only else _ICONS
        body = highlighted(record["message"], style)
        if name in {"WARNING", "ERROR", "CRITICAL"}:
            body = Text.assemble((f"{name.lower()}: ", style), body)
        row = Table.grid(padding=(0, 1))
        if timestamps:
            row.add_column(no_wrap=True)
        row.add_column(no_wrap=True)
        row.add_column(ratio=1)
        cells: list[Text] = []
        if timestamps:
            cells.append(Text(record["time"].strftime("%H:%M:%S"), style="dim"))
        cells.append(Text(icons.get(name, " "), style=style))
        cells.append(body)
        row.add_row(*cells)
        console.print(row, highlight=False)
        exc = record["exception"]
        if exc is not None and exc.type and exc.value and exc.traceback:
            console.print(Traceback.from_exception(exc.type, exc.value, exc.traceback))

    return sink


def setup_logging(level: str | None = None, *, timestamps: bool | None = None) -> None:
    """Send this package's log output to the console, and pretty-print uncaught errors.

    Args:
        level: Minimum level to show. Defaults to the ``MP3_EPUB_M4B_LOG_LEVEL``
            environment variable, then ``INFO``.
        timestamps: Prefix each line with the time. Defaults to the
            ``MP3_EPUB_M4B_LOG_TIME`` environment variable, then on.
    """
    logger.enable("mp3_epub_m4b")
    logger.remove()
    chosen = (level or os.environ.get("MP3_EPUB_M4B_LOG_LEVEL", "INFO")).upper()
    show_time = _timestamps_default() if timestamps is None else timestamps
    logger.add(_make_sink(show_time), level=chosen, format="{message}")
    install_rich_traceback(console=console, show_locals=False)


def result_panel(title: str, rows: Sequence[tuple[str, str]], *, ok: bool = True) -> Panel:
    """A framed summary of an outcome: green with a tick on success, red with a cross on failure.

    Args:
        title: Heading shown in the panel border, e.g. ``"Done"``.
        rows: ``(label, value)`` pairs; values get the same highlighting as log lines.
        ok: Whether the outcome was a success.
    """
    colour = "green" if ok else "red"
    icon = _ICONS["SUCCESS" if ok else "ERROR"]
    grid = Table.grid(padding=(0, 2))
    grid.add_column(style="bold", no_wrap=True)
    grid.add_column(overflow="fold")
    for label, value in rows:
        grid.add_row(label, highlighted(value))
    return Panel(
        grid,
        title=f"[bold {colour}]{icon} {title}[/]",
        title_align="left",
        border_style=colour,
        box=box.ROUNDED,
        expand=False,
        padding=(0, 1),
    )
