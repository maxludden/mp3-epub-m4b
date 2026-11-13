"""Ask a waiting anchoring question in the terminal: play the clip, show the choices, read the pick."""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

from rich.markup import escape

from mp3_epub_m4b.log import console
from mp3_epub_m4b.questions import Question

PLAYERS = (
    ["afplay"],  # macOS
    ["ffplay", "-nodisp", "-autoexit", "-loglevel", "error"],  # comes with ffmpeg
)


def play_clip(path: Path) -> None:
    """Play ``path`` with the first available player; do nothing if there is none."""
    for cmd in PLAYERS:
        if shutil.which(cmd[0]):
            subprocess.run([*cmd, str(path)], check=False, capture_output=True)
            return


def ask(
    question: Question,
    *,
    input_fn: Callable[[str], str] = input,
    play: Callable[[Path], None] = play_clip,
) -> str:
    """Show ``question``, play its clip, and return the id of the choice the person picks.

    Accepts a choice number, ``r`` to replay the clip, or Enter for the
    suggested choice (when there is one); anything else asks again.
    """
    choices = question["choices"]
    console.print()
    console.print(f"[bold]{escape(question['prompt'])}[/bold]")
    console.print(
        f"[dim]Clip: {question['clip_seconds']:.0f}s from {question['clip_start']:.0f}s "
        f"({escape(question['clip_path'])})[/dim]"
    )
    console.print(f"Heard: [italic]{escape(question['heard'] or '(nothing)')}[/italic]")
    for i, c in enumerate(choices, start=1):
        mark = " [green](suggested)[/green]" if c["id"] == question["suggested"] else ""
        console.print(f"  [bold]{i}[/bold]. {escape(c['label'])}{mark}")
        if c["text"]:
            console.print(f"     [dim]{escape(c['text'])}[/dim]")
    play(Path(question["clip_path"]))
    hint = f"1-{len(choices)}, r to replay" + (
        ", Enter for suggested" if question["suggested"] else ""
    )
    while True:
        raw = input_fn(f"Choice ({hint}): ").strip().lower()
        if raw == "" and question["suggested"]:
            return question["suggested"]
        if raw == "r":
            play(Path(question["clip_path"]))
            continue
        if raw.isdigit() and 1 <= int(raw) <= len(choices):
            return choices[int(raw) - 1]["id"]
        console.print(f"[yellow]Please enter {hint}.[/yellow]")
