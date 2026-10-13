"""Align an audiobook with its EPUB into an EPUB 3 with Media Overlays.

Wraps the ``align`` CLI from ``@storyteller-platform/align``
(https://www.npmjs.com/package/@storyteller-platform/align):

- needs Node >= 24 (looked for on ``PATH`` and in Homebrew);
- installs the tool once into :data:`TOOL_DIR` (plus the MLX backend on Apple
    Silicon), so nothing depends on the npx cache;
- ``align --audiobook`` requires a directory, so a single audio file is staged
    into a temporary directory via symlink;
- the tool's output is shown live and also saved to a timestamped log file.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from loguru import logger

from audiobook_kit.log import console

TOOL_DIR = Path.home() / ".audiobook-kit-align"
LOG_DIR = Path.home() / ".audiobook-kit-work" / "align-logs"
MIN_NODE_MAJOR = 24
NODE_CANDIDATES = (Path("/opt/homebrew/bin/node"), Path("/usr/local/bin/node"))
PACKAGES = ("@storyteller-platform/align",)
MLX_PACKAGE = "@storyteller-platform/mlx-audio"  # Apple Silicon only


class AlignError(RuntimeError):
    """Alignment could not be set up or the ``align`` tool failed."""


@dataclass
class AlignOptions:
    """Settings for one alignment run.

    Attributes:
        granularity: ``"sentence"`` or ``"word"``.
        language: BCP 47 locale of the audio, e.g. ``"en-US"``.
        device: ``auto|cpu|webgpu|dml|cuda|mlx`` (CTC only); ``None`` lets ``align`` choose.
        engine: Transcription engine, e.g. ``"whisper.cpp"``; ``None`` uses the CTC aligner.
        model: CTC model (default ``mms``) or whisper model; ``None`` lets ``align`` choose.
        codec: ``copy|aac|libopus|libmp3lame``; ``None`` lets ``align`` choose.
        progress: Show the tool's progress bars (hidden by default; they garble the log).
        extra: Arguments passed straight through to ``align``.
    """

    granularity: str = "sentence"
    language: str = "en-US"
    device: str | None = None
    engine: str | None = None
    model: str | None = None
    codec: str | None = None
    progress: bool = False
    extra: list[str] = field(default_factory=list)


def default_output(epub: Path) -> Path:
    """Return ``<epub name> (aligned).epub`` next to ``epub``."""
    return epub.with_name(f"{epub.stem} (aligned).epub")


def node_major(node: Path) -> int:
    """Return the major version of a node binary, or 0 if it won't run."""
    try:
        result = subprocess.run(
            [str(node), "-p", "process.versions.node.split('.')[0]"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return int(result.stdout.strip()) if result.returncode == 0 else 0
    except (OSError, ValueError, subprocess.SubprocessError):
        return 0


def find_node() -> Path:
    """Find a Node >= :data:`MIN_NODE_MAJOR`: the one on ``PATH`` first, then Homebrew.

    Raises:
        AlignError: If no suitable Node is found.
    """
    on_path = shutil.which("node")
    candidates = ([Path(on_path)] if on_path else []) + list(NODE_CANDIDATES)
    for cand in candidates:
        if cand.is_file() and os.access(cand, os.X_OK) and node_major(cand) >= MIN_NODE_MAJOR:
            return cand
    raise AlignError(f"Node >= {MIN_NODE_MAJOR} required (brew install node)")


def ensure_installed(node: Path, tool_dir: Path = TOOL_DIR) -> Path:
    """Install the aligner into ``tool_dir`` if needed and return its binary path.

    The install is skipped once the ``align`` binary exists. Adds the MLX audio
    backend only on Apple Silicon.

    Raises:
        AlignError: If ``npm`` fails.
    """
    align_bin = tool_dir / "node_modules" / ".bin" / "align"
    if os.access(align_bin, os.X_OK):
        return align_bin

    logger.info(f"installing {PACKAGES[0]} into {tool_dir} ...")
    tool_dir.mkdir(parents=True, exist_ok=True)
    npm = str(node.parent / "npm") if (node.parent / "npm").exists() else "npm"
    packages: list[str] = list(PACKAGES)
    if platform.system() == "Darwin" and platform.machine() == "arm64":
        packages.append(MLX_PACKAGE)
    try:
        package_json = tool_dir / "package.json"
        if not package_json.exists():
            # Not `npm init -y`: it derives the name from the directory, and npm
            # rejects the leading dot in ".audiobook-kit-align".
            package_json.write_text(json.dumps({"name": "audiobook-kit-align", "private": True}))
        subprocess.run([npm, "install", *packages], cwd=tool_dir, check=True)
    except (OSError, subprocess.CalledProcessError) as e:
        raise AlignError(f"install failed: {e}") from e
    return align_bin


def build_command(
    align_bin: Path, audio_dir: Path, epub: Path, out: Path, opts: AlignOptions
) -> list[str]:
    """Build the ``align`` command line.

    ``-u`` upgrades EPUB 2 to EPUB 3, ``--time`` prints timings. An explicit
    engine selects transcription-based alignment; otherwise the CTC aligner
    (which also accepts ``--device``) is used.
    """
    cmd: list[str] = [
        str(align_bin),
        "--audiobook",
        str(audio_dir),
        "--epub",
        str(epub),
        "--output",
        str(out),
        "-g",
        opts.granularity,
        "--language",
        opts.language,
        "-u",
        "--time",
        "--log-level",
        "info",
    ]
    if opts.engine:
        cmd += ["--engine", opts.engine]
    else:
        cmd.append("--ctc")
        if opts.device:
            cmd += ["--device", opts.device]
    if opts.model:
        cmd += ["--model", opts.model]
    if opts.codec:
        cmd += ["--codec", opts.codec]
    if not opts.progress:
        cmd.append("--no-progress")
    return cmd + list(opts.extra)


def _stream(cmd: Sequence[str], log_path: Path, env: dict[str, str]) -> int:
    """Run ``cmd``, echoing each output line to the console and ``log_path``; return its exit code."""
    with (
        log_path.open("w", encoding="utf-8") as log,
        subprocess.Popen(
            args=cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env=env
        ) as proc,
    ):
        assert proc.stdout is not None
        for line in proc.stdout:
            log.write(line)
            console.print(line.rstrip("\n"), markup=False, highlight=False, soft_wrap=True)
        return proc.wait()


def align_book(
    audio: Path,
    epub: Path,
    out: Path | None = None,
    opts: AlignOptions | None = None
) -> Path:
    """Align ``audio`` (a file or a directory of audio files) with ``epub``.

    Args:
        audio: An ``.m4b``/``.mp3``/``.m4a`` file, or a directory of audio files.
        epub: The book's EPUB.
        out: Output EPUB; defaults to :func:`default_output`. Never overwritten.
        opts: Alignment settings.

    Returns:
        Path of the aligned EPUB.

    Raises:
        AlignError: On missing inputs, an existing output, a missing ``ffmpeg``
            or Node, a failed install, or a non-zero exit from ``align``.
    """
    opts = opts or AlignOptions()
    audio, epub = audio.resolve(), epub.resolve()
    out = (out or default_output(epub)).resolve()

    # Fail early: inputs must exist, the output must be a new .epub, ffmpeg must exist.
    if not audio.exists():
        raise AlignError(f"audiobook not found: {audio}")
    if not epub.is_file():
        raise AlignError(f"epub not found: {epub}")
    if out.suffix != ".epub":
        raise AlignError(f"output must end in .epub: {out}")
    if out.exists():
        raise AlignError(f"output already exists, refusing to overwrite: {out}")
    if shutil.which("ffmpeg") is None:
        raise AlignError("ffmpeg missing (brew install ffmpeg)")

    node: Path = find_node()
    align_bin: Path = ensure_installed(node)
    # Put the chosen node first on PATH so the aligner's scripts use it.
    env = {**os.environ, "PATH": f"{node.parent}{os.pathsep}{os.environ.get('PATH', '')}"}

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOG_DIR / f"{epub.stem}-{datetime.now():%Y%m%d-%H%M%S}.log"
    node_version = subprocess.run([str(node), "-v"], capture_output=True, text=True).stdout.strip()
    logger.info(f"node:   {node} ({node_version})")
    logger.info(f"output: {out}")
    logger.info(f"log:    {log_path}")

    # `align --audiobook` requires a directory: symlink a single file into a temp dir.
    with tempfile.TemporaryDirectory() as stage:
        if audio.is_dir():
            audio_dir = audio
        else:
            (Path(stage) / audio.name).symlink_to(audio)
            audio_dir = Path(stage)
        cmd: list[str] = build_command(align_bin, audio_dir, epub, out, opts)
        logger.debug(f"running: {' '.join(cmd)}")
        rc = _stream(cmd, log_path, env)

    if rc != 0:
        raise AlignError(f"align failed (exit {rc}) — see {log_path}")
    if not out.is_file():
        raise AlignError(f"align exited cleanly but produced no output — see {log_path}")
    return out
