#!/usr/bin/env python3
"""Align an audiobook with its EPUB (EPUB 3 + Media Overlays) via @storyteller-platform/align.

Python/Typer port of align.sh, with Rich progress bars.

    uv run scripts/align.py book.m4b book.epub
    uv run scripts/align.py "audio dir/" book.epub out.epub -g word --device cpu

Notes:
  * Needs Node >= 24 (the tool uses DisposableStack).
  * The tool is installed once into ~/.audiobook-kit-align (plus the MLX
    backend on Apple Silicon), so nothing depends on the npx cache.
  * `align --audiobook` requires a directory, so a single audio file is
    staged into a temporary directory via symlink.
"""

from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
import tempfile
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Annotated, Optional

import typer
from rich.console import Console
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TaskID,
    TextColumn,
    TimeElapsedColumn,
)

TOOL_DIR = Path.home() / ".audiobook-kit-align"
LOG_DIR = Path.home() / ".audiobook-kit-work" / "align-logs"
MIN_NODE_MAJOR = 24

console = Console()
app = typer.Typer(add_completion=False, help=__doc__, rich_markup_mode=None)


class Granularity(str, Enum):
    sentence = "sentence"
    word = "word"


class Device(str, Enum):
    auto = "auto"
    cpu = "cpu"
    webgpu = "webgpu"
    dml = "dml"
    cuda = "cuda"
    mlx = "mlx"


class Codec(str, Enum):
    copy = "copy"
    aac = "aac"
    libopus = "libopus"
    libmp3lame = "libmp3lame"


def fail(msg: str) -> "typer.Exit":
    console.print(f"[bold red]error:[/] {msg}")
    return typer.Exit(1)


def node_major(node: str) -> int:
    try:
        out = subprocess.run(
            [node, "-p", "process.versions.node.split('.')[0]"],
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout.strip()
        return int(out)
    except (OSError, ValueError, subprocess.SubprocessError):
        return 0


def find_node() -> Path:
    cands = [shutil.which("node"), "/opt/homebrew/bin/node", "/usr/local/bin/node"]
    for cand in cands:
        if cand and Path(cand).exists() and node_major(cand) >= MIN_NODE_MAJOR:
            return Path(cand)
    raise fail(f"Node >= {MIN_NODE_MAJOR} required (brew install node)")


def ensure_installed(node: Path, env: dict[str, str]) -> Path:
    align_bin = TOOL_DIR / "node_modules" / ".bin" / "align"
    if align_bin.exists():
        return align_bin
    npm = node.parent / "npm"
    npm_cmd = str(npm) if npm.exists() else "npm"
    pkgs = ["@storyteller-platform/align"]
    if platform.system() == "Darwin" and platform.machine() == "arm64":
        pkgs.append("@storyteller-platform/mlx-audio")
    TOOL_DIR.mkdir(parents=True, exist_ok=True)
    with console.status(f"Installing {', '.join(pkgs)} into {TOOL_DIR} ..."):
        try:
            if not (TOOL_DIR / "package.json").exists():
                subprocess.run(
                    [npm_cmd, "init", "-y"], cwd=TOOL_DIR, env=env, check=True,
                    capture_output=True,
                )
            subprocess.run(
                [npm_cmd, "install", *pkgs], cwd=TOOL_DIR, env=env, check=True,
                capture_output=True, text=True,
            )
        except subprocess.CalledProcessError as e:
            console.print(e.stderr or "")
            raise fail("npm install failed") from e
    if not align_bin.exists():
        raise fail(f"install finished but {align_bin} is missing")
    return align_bin


# --- progress parsing -------------------------------------------------------
# align prints log lines ("INFO: Transcribing...") and, unless --no-progress,
# \r-updated bars containing "NN%" (model downloads, transcription, ...).
PCT_RE = re.compile(r"(\d{1,3})%")
LOG_RE = re.compile(r"\] (?:INFO|WARN|ERROR|DEBUG): (.*)")
ITEM_RE = re.compile(r"Marking up epub item #(\d+)")


def stream_chunks(proc: subprocess.Popen[bytes]):
    """Yield text segments split on \\r or \\n so live bars arrive as updates."""
    assert proc.stdout is not None
    buf = b""
    while chunk := proc.stdout.read1(4096):  # type: ignore[attr-defined]
        buf += chunk
        parts = re.split(rb"[\r\n]", buf)
        buf = parts.pop()
        for p in parts:
            if p.strip():
                yield p.decode("utf-8", "replace")
    if buf.strip():
        yield buf.decode("utf-8", "replace")


def run_with_progress(cmd: list[str], env: dict[str, str], log_path: Path) -> int:
    progress = Progress(
        SpinnerColumn(),
        TextColumn("[bold]{task.description}"),
        BarColumn(),
        TextColumn("{task.percentage:>5.1f}%", justify="right"),
        TimeElapsedColumn(),
        console=console,
        transient=False,
    )
    task: TaskID | None = None
    stage = "Starting"
    with progress, log_path.open("w") as log:
        task = progress.add_task(stage, total=None)
        proc = subprocess.Popen(
            cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT
        )
        for seg in stream_chunks(proc):
            log.write(seg + "\n")
            line = seg.strip()
            if (m := LOG_RE.search(line)) and not line.startswith("█"):
                msg = m.group(1)
                if "Marking up epub item" not in msg and len(msg) < 90:
                    stage = msg.rstrip(".")
                    progress.reset(task, total=None, description=stage)
                    continue
            if (m := ITEM_RE.search(line)):
                progress.update(task, description="Marking up EPUB", total=None)
                continue
            if "█" in line or "░" in line or PCT_RE.search(line):
                if m := PCT_RE.search(line):
                    pct = min(int(m.group(1)), 100)
                    progress.update(task, total=100, completed=pct)
                    # e.g. "... 537.45/601.76 MB" -> show it as the stage detail
                    if (sz := re.search(r"([\d.]+/[\d.]+ \w+)", line)):
                        progress.update(task, description=f"{stage} ({sz.group(1)})")
            elif line.startswith(("Error", "ReferenceError", "TypeError")):
                console.print(f"[red]{line}[/]")
        rc = proc.wait()
        if rc == 0:
            progress.update(task, total=100, completed=100, description="Done")
    return rc


@app.command()
def align(
    audiobook: Annotated[
        Path, typer.Argument(help="Audio file (.m4b/.mp3/.m4a) or directory of audio files")
    ],
    epub: Annotated[Path, typer.Argument(help="Source EPUB")],
    output: Annotated[
        Optional[Path],
        typer.Argument(help='Output EPUB (default: "<epub> (aligned).epub")'),
    ] = None,
    granularity: Annotated[Granularity, typer.Option("--granularity", "-g")] = Granularity.sentence,
    language: Annotated[str, typer.Option("--language", "-l", help="BCP 47 audio language")] = "en-US",
    device: Annotated[Optional[Device], typer.Option("--device", "-d", help="CTC inference device")] = None,
    engine: Annotated[
        Optional[str],
        typer.Option("--engine", "-e", help="Use a transcription engine (e.g. whisper.cpp) instead of CTC"),
    ] = None,
    model: Annotated[Optional[str], typer.Option("--model", "-m", help="CTC or whisper model")] = None,
    codec: Annotated[Optional[Codec], typer.Option(help="Audio codec to transcode to")] = None,
    extra: Annotated[
        Optional[list[str]],
        typer.Option("--extra", help="Extra argument passed through to align (repeatable)"),
    ] = None,
) -> None:
    audio = audiobook.expanduser().resolve()
    book = epub.expanduser().resolve()
    out = (
        output.expanduser().resolve()
        if output
        else book.with_name(f"{book.stem} (aligned).epub")
    )

    if not audio.exists():
        raise fail(f"audiobook not found: {audio}")
    if not book.is_file():
        raise fail(f"epub not found: {book}")
    if out.suffix != ".epub":
        raise fail(f"output must end in .epub: {out}")
    if out.exists():
        raise fail(f"output already exists, refusing to overwrite: {out}")
    if not shutil.which("ffmpeg"):
        raise fail("ffmpeg missing (brew install ffmpeg)")

    node = find_node()
    env = {**os.environ, "PATH": f"{node.parent}{os.pathsep}{os.environ['PATH']}"}
    align_bin = ensure_installed(node, env)

    with tempfile.TemporaryDirectory(prefix="audiobook-kit-align-") as stage:
        if audio.is_dir():
            audio_dir = audio
        else:
            (Path(stage) / audio.name).symlink_to(audio)
            audio_dir = Path(stage)

        cmd = [
            str(align_bin), "--audiobook", str(audio_dir), "--epub", str(book),
            "--output", str(out), "-g", granularity.value, "--language", language,
            "-u", "--time", "--log-level", "info",
        ]
        if engine:
            cmd += ["--engine", engine]
            if model:
                cmd += ["--model", model]
        else:
            cmd.append("--ctc")
            if device:
                cmd += ["--device", device.value]
            if model:
                cmd += ["--model", model]
        if codec:
            cmd += ["--codec", codec.value]
        cmd += extra or []

        LOG_DIR.mkdir(parents=True, exist_ok=True)
        log_path = LOG_DIR / f"{book.stem}-{datetime.now():%Y%m%d-%H%M%S}.log"
        console.print(f"node:   {node} ({subprocess.run([str(node), '-v'], capture_output=True, text=True).stdout.strip()})")
        console.print(f"output: {out}")
        console.print(f"log:    {log_path}")

        rc = run_with_progress(cmd, env, log_path)

    if rc != 0:
        raise fail(f"align failed (exit {rc}) — see {log_path}")
    size = out.stat().st_size / 1e6 if out.exists() else 0
    console.print(f"[bold green]DONE:[/] {out} ({size:,.0f} MB)")


if __name__ == "__main__":
    app()
