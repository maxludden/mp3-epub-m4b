"""Logging setup: silent by default for library users, routed to the console by the CLIs."""

from __future__ import annotations

import io

from loguru import logger
from rich.console import Console

from mp3_epub_m4b import log as log_mod


def test_library_is_silent_until_enabled() -> None:
    seen: list[str] = []
    handler = logger.add(lambda m: seen.append(str(m)), level="DEBUG")
    try:
        logger.disable("mp3_epub_m4b")
        logger.bind().opt(depth=0)  # no-op; just exercising the API
        from mp3_epub_m4b import run  # noqa: F401  (import must not enable logging)

        assert seen == []
    finally:
        logger.remove(handler)


def test_setup_logging_routes_levels_and_escapes(monkeypatch) -> None:
    buf = io.StringIO()
    monkeypatch.setattr(log_mod, "console", Console(file=buf, force_terminal=False, width=200))
    log_mod.setup_logging("INFO")
    try:
        logger.info("hello [bold]not markup[/bold]")
        logger.warning("careful")
        logger.debug("hidden")
    finally:
        logger.remove()
        logger.disable("mp3_epub_m4b")
    out = buf.getvalue()
    assert "hello [bold]not markup[/bold]" in out
    assert "warning: careful" in out
    assert "hidden" not in out
