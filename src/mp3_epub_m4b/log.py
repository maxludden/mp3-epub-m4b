"""Shared Rich console and loguru setup for the command-line tools.

The library modules log through loguru (disabled by default; see the package
``__init__``). Each CLI entry point calls :func:`setup_logging` to route those
messages to :data:`console`, which is also what the progress bars render on, so
log lines appear cleanly above the bars.
"""

from __future__ import annotations

import os
import re
from typing import TYPE_CHECKING

from loguru import logger
from rich.console import Console
from rich.text import Text

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


def _sink(message: Message) -> None:
    """Loguru sink: print one record to :data:`console`, styled by level and highlighted."""
    record: Record = message.record
    name = record["level"].name
    style = _STYLES.get(name, "")
    line = highlighted(record["message"], style)
    if name in {"WARNING", "ERROR", "CRITICAL"}:
        console.print(Text(f"{name.lower()}:", style=style), line, highlight=False)
    else:
        console.print(line, highlight=False)


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
