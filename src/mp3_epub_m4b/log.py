"""Shared Rich console and loguru setup for the command-line tools.

The library modules log through loguru (disabled by default; see the package
``__init__``). Each CLI entry point calls :func:`setup_logging` to route those
messages to :data:`console`, which is also what the progress bars render on, so
log lines appear cleanly above the bars.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from loguru import logger
from rich.console import Console
from rich.markup import escape

if TYPE_CHECKING:
    from loguru import Message, Record

console = Console()

_STYLES = {
    "DEBUG": "dim",
    "INFO": "#00aaff",
    "SUCCESS": "b #00ff00",
    "WARNING": "i yellow",
    "ERROR": "i #FF8C00",
    "CRITICAL": "bold red",
}


def _sink(message: Message) -> None:
    """Loguru sink: print one record to :data:`console`, styled by level."""
    record: Record = message.record
    name = record["level"].name
    style = _STYLES.get(name, "")
    text = escape(record["message"])
    if name in {"WARNING", "ERROR", "CRITICAL"}:
        console.print(f"[{style}]{name.lower()}:[/{style}] {text}")
    else:
        console.print(f"[{style}]{text}[/{style}]")


def setup_logging(level: str | None = None) -> None:
    """Send this package's log output to the console.

    Args:
        level: Minimum level to show. Defaults to the ``MP3_EPUB_M4B_LOG_LEVEL``
            environment variable, then ``INFO``.
    """
    logger.enable("mp3_epub_m4b")
    logger.remove()
    chosen = (level or os.environ.get("MP3_EPUB_M4B_LOG_LEVEL", "INFO")).upper()
    logger.add(_sink, level=chosen, format="{message}")
