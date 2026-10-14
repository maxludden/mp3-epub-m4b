"""Logging setup: silent by default for library users, routed to the console by the CLIs."""

from __future__ import annotations

import io
import re

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


def styled(text: str) -> dict[str, str]:
    """Map each highlighted substring of ``text`` to the style it received."""
    out = log_mod.highlighted(text)
    assert out.plain == text  # highlighting never changes the words
    return {out.plain[s.start : s.end]: str(s.style) for s in out.spans}


def test_highlighting_marks_numbers_times_scores_quotes_flags_and_paths() -> None:
    found = styled(
        "chapter 12 ('37') heard at 6:32:43 (score 0.97, expected 23061s); "
        "try --align-by transcribe for /Users/me/Mistborn/1 - The Finale Empire.mp3"
    )
    assert found["12"] == "magenta"
    assert found["23061s"] == "magenta"
    assert found["6:32:43"] == "bold magenta"
    assert found["0.97"] == "bold magenta"
    assert found["'37'"] == "green"
    assert found["heard"] == "bold green"
    assert found["--align-by"] == "bold yellow"
    assert found["/Users/me/Mistborn/1 - The Finale Empire.mp3"] == "underline cyan"


def test_highlighting_covers_a_whole_path_with_spaces_in_its_folders() -> None:
    path = "/Users/me/Brandon Sanderson/Mistborn/1 - The Finale Empire.mp3"
    found = styled(f"Done: {path} and then /tmp/x.epub")
    assert found[path] == "underline cyan"  # not swallowing " and then", not split at the spaces
    assert found["/tmp/x.epub"] == "underline cyan"


def test_highlighting_flags_problems_and_actions_and_keeps_plain_text_plain() -> None:
    found = styled(
        "chapter 13 not heard; switching to transcript alignment, 2 unmatched, 9 guessed"
    )
    assert found["not heard"] == "bold #ff8700" and found["unmatched"] == "bold #ff8700"
    assert found["guessed"] == "bold #ff8700" and found["switching"] == "bold #00aaff"
    assert found["transcript"] == "italic"
    assert styled("nothing to see here") == {}


def test_a_log_line_keeps_its_level_colour_and_adds_highlights() -> None:
    buf = io.StringIO()
    console = Console(file=buf, force_terminal=True, color_system="truecolor", width=200)
    console.print(log_mod.highlighted("chapter 5 heard", "#00aaff"), highlight=False)
    out = buf.getvalue()
    assert "\x1b[" in out and "chapter" in out and "heard" in out
    assert "\x1b[38;2;0;170;255m" in out  # the level's base colour is still applied
    assert "\x1b[1;32mheard" in out  # bold green highlight on the word itself


def test_highlighted_log_lines_survive_no_color(monkeypatch) -> None:
    buf = io.StringIO()
    monkeypatch.setattr(log_mod, "console", Console(file=buf, force_terminal=False, width=200))
    log_mod.setup_logging("INFO")
    try:
        logger.info("chapter 5 heard at 0:01:02 in '/tmp/a b.mp3'")
    finally:
        logger.remove()
        logger.disable("mp3_epub_m4b")
    line = buf.getvalue().strip()
    assert re.fullmatch(r"\d\d:\d\d:\d\d ℹ chapter 5 heard at 0:01:02 in '/tmp/a b.mp3'", line)


def _capture(monkeypatch, **kwargs) -> io.StringIO:
    """Route the log to a buffer (plain, 60 wide) and set logging up with ``kwargs``."""
    buf = io.StringIO()
    monkeypatch.setattr(log_mod, "console", Console(file=buf, force_terminal=False, width=60))
    log_mod.setup_logging("DEBUG", **kwargs)
    return buf


def _teardown() -> None:
    logger.remove()
    logger.disable("mp3_epub_m4b")


def test_each_level_gets_its_icon(monkeypatch) -> None:
    buf = _capture(monkeypatch, timestamps=False)
    try:
        logger.info("a")
        logger.success("b")
        logger.warning("c")
        logger.error("d")
    finally:
        _teardown()
    lines = [ln.rstrip() for ln in buf.getvalue().splitlines()]
    assert lines == ["ℹ a", "✔ b", "⚠ warning: c", "✖ error: d"]


def test_timestamps_can_be_switched_off_by_environment(monkeypatch) -> None:
    monkeypatch.setenv("MP3_EPUB_M4B_LOG_TIME", "off")
    buf = _capture(monkeypatch)
    try:
        logger.info("quiet clock")
    finally:
        _teardown()
    assert buf.getvalue().strip() == "ℹ quiet clock"


def test_wrapped_messages_keep_a_hanging_indent(monkeypatch) -> None:
    buf = _capture(monkeypatch, timestamps=False)
    try:
        logger.info("word " * 30)
    finally:
        _teardown()
    first, *rest = buf.getvalue().splitlines()
    assert first.startswith("ℹ word")
    assert rest and all(ln.startswith("  word") for ln in rest if ln.strip())


def test_logged_exceptions_print_a_rich_traceback(monkeypatch) -> None:
    buf = _capture(monkeypatch, timestamps=False)
    try:
        try:
            raise ValueError("boom")
        except ValueError:
            logger.exception("it broke")
    finally:
        _teardown()
    out = buf.getvalue()
    assert "it broke" in out and "ValueError" in out and "boom" in out


def test_result_panel_frames_rows_and_marks_outcome() -> None:
    buf = io.StringIO()
    console = Console(file=buf, force_terminal=False, width=80)
    console.print(log_mod.result_panel("Done", [("output", "/tmp/x.m4b"), ("size", "12 MB")]))
    console.print(log_mod.result_panel("Failed", [("error", "no space")], ok=False))
    out = buf.getvalue()
    assert "✔ Done" in out and "/tmp/x.m4b" in out and "12 MB" in out
    assert "✖ Failed" in out and "no space" in out
