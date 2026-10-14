"""iCloud Drive helpers (macOS): fetch an evicted file before use, evict it afterwards.

Everything degrades to a no-op on other platforms or when ``brctl`` is missing,
so the rest of the tool never needs to know whether iCloud is involved.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import time
from pathlib import Path

from loguru import logger

DOWNLOAD_TIMEOUT = 3600.0  # give up waiting for a download after an hour
POLL_SECONDS = 15.0


def is_local(path: Path) -> bool:
    """Whether the file's data is fully on local disk (not an evicted iCloud placeholder)."""
    st = path.stat()
    return st.st_blocks * 512 >= st.st_size


def in_icloud_drive(path: Path) -> bool:
    """Whether ``path`` lives under iCloud Drive's ``Mobile Documents`` folder."""
    return "Mobile Documents" in path.resolve().parts


def _brctl() -> str | None:
    """Path of the ``brctl`` tool, or ``None`` when iCloud tooling isn't available."""
    return shutil.which("brctl") if sys.platform == "darwin" else None


def ensure_local(path: Path, timeout: float = DOWNLOAD_TIMEOUT) -> None:
    """Make sure ``path`` is fully downloaded, waiting for iCloud if it was evicted.

    Does nothing for files that are already local, or when ``brctl`` is unavailable.

    Args:
        path: An existing file.
        timeout: Longest wait (s) for the download.

    Raises:
        TimeoutError: If the file is still not local after ``timeout`` seconds.
    """
    if is_local(path):
        return
    brctl = _brctl()
    if brctl is None:
        return
    logger.info(f"{path.name} is evicted from local disk; downloading from iCloud...")
    subprocess.run([brctl, "download", str(path)], capture_output=True)
    deadline = time.monotonic() + timeout
    while not is_local(path):
        if time.monotonic() >= deadline:
            raise TimeoutError(f"timed out waiting for iCloud to download {path}")
        time.sleep(POLL_SECONDS)


def evict(path: Path) -> bool:
    """Evict ``path`` from local disk (it stays in iCloud). Returns whether it was evicted."""
    brctl = _brctl()
    if brctl is None or not in_icloud_drive(path):
        return False
    proc = subprocess.run([brctl, "evict", str(path)], capture_output=True)
    return proc.returncode == 0
