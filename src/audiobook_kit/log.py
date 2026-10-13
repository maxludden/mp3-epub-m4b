"""Shared Rich console and loguru setup for the command-line tools.

The library modules log through loguru (disabled by default; see the package
``__init__``). Each CLI entry point calls :func:`setup_logging` to route those
messages to :data:`console`, which is also what the progress bars render on, so
log lines appear cleanly above the bars.
"""

from __future__ import annotations

import os

from loguru import logger
from rich.console import Console
from rich.markup import escape

console = Console()

_STYLES = {
    "DEBUG": "dim",
    "INFO": "dim",
    "SUCCESS": "green",
    "WARNING": "yellow",
    "ERROR": "red",
    "CRITICAL": "bold red",
}


def _sink(message: object) -> None:
    """Loguru sink: print one record to :data:`console`, styled by level."""
    record = message.record  # type: ignore[attr-defined]
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
        level: Minimum level to show. Defaults to the ``AUDIOBOOK_KIT_LOG_LEVEL``
            environment variable, then ``INFO``.
    """
    logger.enable("audiobook_kit")
    logger.remove()
    chosen = (level or os.environ.get("AUDIOBOOK_KIT_LOG_LEVEL", "INFO")).upper()
    logger.add(_sink, level=chosen, format="{message}")
