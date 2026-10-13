"""Render align.pretty.md in the terminal with color.

Usage: uv run scripts/align_help.py [--pager]
"""

import sys
from pathlib import Path

from rich.console import Console
from rich.markdown import Markdown
from rich.theme import Theme
from rich_gradient.theme import RICH_GRADIENT_THEME

THEME = Theme(
    {
        "markdown.h1": "bold bright_magenta",
        "markdown.h2": "bold bright_cyan",
        "markdown.h3": "bold bright_green",
        "markdown.h4": "bold yellow",
        "markdown.code": "bold bright_yellow",
        "markdown.code_block": "bright_white",
        "markdown.table.header": "bold bright_blue",
        "markdown.table.border": "bright_black",
    }
)

SOURCE = Path(__file__).with_name("align.pretty.md")


def main() -> None:
    console = Console(theme=THEME, record=True)
    md = Markdown(SOURCE.read_text(), code_theme="monokai", hyperlinks=False)
    if "--pager" in sys.argv:
        with console.pager(styles=True):
            console.print(md)
    else:
        console.print(md)

    console.save_html("align_help.html")


if __name__ == "__main__":
    main()
