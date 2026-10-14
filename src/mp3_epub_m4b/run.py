"""
Resumable MP3/M4A (+ EPUB) -> chaptered M4B pipeline.

Drive it through the ``mp3-epub-m4b`` CLI (:mod:`mp3_epub_m4b.cli`), or
call :meth:`Pipeline.run` repeatedly until ``state["stage"] == "done"``. Each
call does a bounded slice of work and checkpoints to
``<work_dir>/state.json``, so an interrupted run resumes where it left off.

Stages: extract -> scan -> align -> assemble -> deliver [-> readaloud]

The optional ``readaloud`` stage (enabled by passing :class:`AlignOptions`) aligns the
delivered M4B with the EPUB into an EPUB 3 with Media Overlays via
:mod:`mp3_epub_m4b.stalign`.

Why this approach (learned the hard way on a real 24-hour audiobook):
- Don't derive chapter boundaries from a book-seller's displayed
    chapter durations (they're rounded to the second and drift by 1-2
    minutes over a long book) and don't anchor purely on EPUB word counts
    either (local narration pace varies enough -- dialogue-heavy vs
    descriptive passages -- to be off by several minutes in places).
- Many professionally narrated audiobooks mark a chapter transition with a
    clean ~3.5s+ silence, usually followed 3-9s later by a second, shorter
    ~1.5-3s "companion" gap (some kind of announcement/sting before the actual
    chapter content starts) that is NOT the real boundary -- picking the wrong
    one of that pair is what causes chapters to start mid-sentence.
- Some recordings have no usable silence at all: chapters joined by music, a
    spoken "Part 2 of 3", or nothing (a 20-hour recording of 40 chapters had
    only 59 pauses, most nowhere near a chapter start). For those,
    ``align_by="transcribe"`` transcribes a window around where each chapter
    should start (estimated from word counts since the last confirmed
    boundary) and finds the chapter's opening words in it. An epigraph file
    before the numbered chapter counts as part of the chapter, because the
    narrator reads it first. ``"auto"`` tries silence and switches to the
    transcript when silence fails or looks unreliable.
- The robust way to find real boundaries: chain forward from the
    previous chapter's *actual detected* boundary, not from any computed
    estimate. Take the first qualifying gap after it. This can't silently
    skip a chapter or drift, because each step's anchor is ground truth
    from the audio itself, not a prediction.
- EPUB word counts are still useful -- to reject a candidate gap that
    falls implausibly early (a long pause *inside* a chapter would otherwise
    shift every later chapter), as a sanity check afterward (flag any
    chapter whose duration deviates wildly from what its word count
    implies), and to catch an EPUB/audiobook mismatch.
- The audio is scanned for silences once (``scan`` stage) and cached in
    ``silences.json``; boundary search is then an in-memory lookup.
- The final M4B is encoded in a single pass with chapter markers, so there
    are no gaps or clicks at chapter joins.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import time
from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import Any

from loguru import logger

from mp3_epub_m4b.align import AlignOptions, align_book
from mp3_epub_m4b.chapters import (
    COMPANION_MIN_SILENCE,
    FALLBACK_SPAN,
    MIN_EARLY_FRACTION,
    SilenceIndex,
    build_ffmetadata,
    build_segments,
    deviating_segments,
    silence_doubts,
)
from mp3_epub_m4b.epub_extract import EpubResult, extract_epub
from mp3_epub_m4b.ffmpeg_tools import FFmpegError, ffprobe_duration, scan_silences
from mp3_epub_m4b.state import (
    STAGES,
    AlignProgress,
    PipelineState,
    Segment,
    StateMismatchError,
    check_fingerprint,
    read_state,
    write_state,
)
from mp3_epub_m4b.status import PROGRESS_FILE, ProgressWriter, StatusFn
from mp3_epub_m4b.transcribe import (
    CONFIDENT_SCORE,
    WIDEN_STEPS,
    WhisperError,
    boundary_in_gap,
    estimate_rate,
    expected_start,
    find_opening,
    find_whisper,
    search_window,
    transcribe_window,
)

SCAN_NOISE_DB = -30.0  # silence threshold used for the whole-file scan
CHAPTER_ONE_WINDOW = 300.0  # how far into the file (s) to look for chapter 1's opening words
ALIGN_BY_CHOICES = ("silence", "transcribe", "auto")
AUDIO_CHANNELS = 1  # output is mono ...
AUDIO_SAMPLE_RATE = 32000  # ... at 32 kHz, which is plenty for narrated speech

ProgressFn = Callable[[float, float], None]


def slugify(s: str) -> str:
    """Reduce ``s`` to an alphanumeric/underscore slug of at most 80 characters.

    Runs of other characters become a single underscore; leading and trailing
    underscores are stripped.
    """
    return re.sub(r"[^A-Za-z0-9]+", "_", s).strip("_")[:80]


def work_name(stem: str) -> str:
    """Name of a book's folder inside the work directory.

    This is :func:`slugify` of ``stem``, but a stem with nothing slug-worthy
    (for example only non-ASCII characters) gets a short hash instead of an
    empty name, which would resolve to the work directory itself.
    """
    slug = slugify(stem)
    return slug or "book_" + hashlib.sha1(stem.encode()).hexdigest()[:10]


def safe_filename(s: str, fallback: str = "audiobook") -> str:
    """Make ``s`` usable as a filename while keeping it readable.

    Path separators and characters illegal on common filesystems become
    spaces; whitespace is collapsed and the result is capped at 120 characters.
    """
    cleaned = " ".join(re.sub(r'[\\/:*?"<>|\x00-\x1f]+', " ", s).split())[:120].strip(" .")
    return cleaned or fallback


def _same_filesystem(a: Path, b: Path) -> bool:
    """Whether two existing paths live on the same filesystem (so a rename is atomic)."""
    return a.stat().st_dev == b.stat().st_dev


def _run_with_progress(cmd: list[str], total: float, on_progress: ProgressFn | None) -> None:
    """Run an ffmpeg command that was given ``-progress pipe:1``, reporting progress.

    Args:
        cmd: Command line; must include ``-progress pipe:1``.
        total: Expected output duration in seconds (for progress scaling).
        on_progress: Called as ``(seconds_done, total)``.

    Raises:
        FFmpegError: If ffmpeg is missing or exits non-zero (message has its output).
    """
    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1
        )
    except FileNotFoundError as e:
        raise FFmpegError(f"{cmd[0]} not found on PATH") from e
    assert proc.stdout is not None
    tail: deque[str] = deque(maxlen=8)
    for line in proc.stdout:
        line = line.strip()
        if line.startswith("out_time_us="):
            if on_progress is not None and line[12:].lstrip("-").isdigit():
                on_progress(min(int(line[12:]) / 1e6, total), total)
        elif line and not ("=" in line and " " not in line):
            tail.append(line)
    if proc.wait() != 0:
        raise FFmpegError(f"{cmd[0]} exited with {proc.returncode}:\n" + "\n".join(tail))


class Pipeline:
    """Resumable audiobook + EPUB to chaptered M4B pipeline.

    Each ``stage_*`` method does a bounded amount of work, checkpoints to
    ``state.json`` in the work directory, and returns whether the stage
    finished. :meth:`run` dispatches to the current stage, so repeated calls
    walk the pipeline to completion.

    Attributes:
        mp3: Source audio file.
        epub: Companion EPUB used for chapter titles.
        out_dir: Directory the final ``.m4b`` is delivered to.
        work_dir: Scratch directory holding state and intermediates.
        min_gap: Minimum silence (s) counted as a chapter transition.
        intro_title: Chapter title for a detected opening segment.
        outro_title: Chapter title for a detected closing segment.
        outro_max_search: How far past the last chapter's start to look for an outro (s).
        bitrate: AAC bitrate of the output.
        detect_intro: Whether to look for an opening-credits segment.
        intro_max_len: Longest accepted intro, in seconds.
        detect_outro: Whether to look for an end-credits segment.
        outro_max_tail: Longest accepted outro tail, in seconds.
        overwrite: Replace an existing output file instead of raising.
        readaloud: Alignment settings; when set, a ``readaloud`` stage runs after delivery.
        align_by: How chapters are placed: ``"silence"`` or ``"transcribe"``.
        whisper_model: GGML model used by ``align_by="transcribe"``.
        on_progress: Called as ``(done, total)`` during long single-step work
            (the scan and assemble stages).
        state_path: Location of the JSON checkpoint.
        silences_path: Location of the cached silence scan.
        state: In-memory checkpoint, loaded from ``state_path`` if it exists.
    """

    def __init__(
        self,
        mp3: Path,
        epub: Path,
        out_dir: Path,
        work_dir: Path,
        min_gap: float,
        intro_title: str,
        outro_title: str,
        outro_max_search: float,
        bitrate: str,
        detect_intro: bool = False,
        intro_max_len: float = 300.0,
        detect_outro: bool = False,
        outro_max_tail: float = 300.0,
        *,
        overwrite: bool = False,
        readaloud: AlignOptions | None = None,
        align_by: str = "silence",
        whisper_model: Path | None = None,
        on_progress: ProgressFn | None = None,
    ) -> None:
        """Create a pipeline and load (or validate) any existing checkpoint.

        Args:
            mp3: Source audio file (mp3 or m4a).
            epub: Companion EPUB.
            out_dir: Directory for the final ``.m4b``.
            work_dir: Scratch directory; created if missing.
            min_gap: Minimum silence (s) counted as a chapter transition.
            intro_title: Title for a detected opening segment.
            outro_title: Title for a detected closing segment.
            outro_max_search: Outro search span past the last chapter's start (s).
            bitrate: AAC bitrate, e.g. ``"96k"``.
            detect_intro: Look for an opening-credits segment before chapter 1.
            intro_max_len: Longest accepted intro (s).
            detect_outro: Look for an end-credits segment after the last chapter.
            outro_max_tail: Longest accepted outro tail (s).
            overwrite: Replace an existing output file instead of raising.
            readaloud: Alignment settings; ``None`` skips the readaloud stage.
            align_by: ``"silence"`` places chapters at detected silences;
                ``"transcribe"`` finds each chapter's opening words in a transcript.
            whisper_model: GGML model for ``align_by="transcribe"`` (default: found automatically).
            on_progress: Progress sink for the scan and assemble stages.

        Raises:
            ValueError: If ``align_by`` is not a known mode.
            StateMismatchError: If ``work_dir`` holds a checkpoint made for
                different files or options.
        """
        self.mp3 = mp3
        self.epub = epub
        self.out_dir = out_dir
        self.work_dir = work_dir
        self.min_gap = min_gap
        self.intro_title = intro_title
        self.outro_title = outro_title
        self.outro_max_search = outro_max_search
        self.bitrate = bitrate
        # Auto-detecting an intro/outro is inherently ambiguous (a false
        # positive silently shifts every chapter after it). Both default to
        # OFF; when enabled, a candidate is only accepted if it's short
        # relative to a real chapter (intro) or the remaining tail after it
        # is short (outro) -- otherwise we've likely just found an ordinary
        # pause inside real chapter content, and safely fall back to no
        # intro/outro rather than mislabeling the whole book.
        self.detect_intro = detect_intro
        self.intro_max_len = intro_max_len
        self.detect_outro = detect_outro
        self.outro_max_tail = outro_max_tail
        self.overwrite = overwrite
        self.readaloud = readaloud
        if align_by not in ALIGN_BY_CHOICES:
            raise ValueError(f"align_by must be one of {ALIGN_BY_CHOICES}, not {align_by!r}")
        self.align_by = align_by
        self.whisper_model = whisper_model
        self.on_progress = on_progress
        self.on_status: StatusFn | None = None

        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.progress = ProgressWriter(self.work_dir / PROGRESS_FILE)
        self.state_path = self.work_dir / "state.json"
        self.silences_path = self.work_dir / "silences.json"
        self._silences: SilenceIndex | None = None
        self.state: PipelineState = self._load_state()

    # ------------------------------------------------------------------ state
    def _fingerprint(self) -> dict[str, Any]:
        """Identify the inputs and options a checkpoint belongs to."""
        fingerprint: dict[str, Any] = {
            "mp3": self.mp3.name,
            "mp3_size": self.mp3.stat().st_size,
            "epub": self.epub.name,
            "epub_size": self.epub.stat().st_size,
            "min_gap": self.min_gap,
            "intro_title": self.intro_title,
            "outro_title": self.outro_title,
            "outro_max_search": self.outro_max_search,
            "bitrate": self.bitrate,
            "detect_intro": self.detect_intro,
            "intro_max_len": self.intro_max_len,
            "detect_outro": self.detect_outro,
            "outro_max_tail": self.outro_max_tail,
        }
        # added only when non-default, so checkpoints from before this option still resume
        # (auto starts as silence, so it fingerprints like silence)
        if self.align_by == "transcribe":
            fingerprint["align_by"] = self.align_by
            if self.whisper_model is not None:
                fingerprint["whisper_model"] = self.whisper_model.name
        return fingerprint

    def _load_state(self) -> PipelineState:
        """Load the checkpoint (migrating older ones), or start at the ``extract`` stage.

        Raises:
            StateMismatchError: If the checkpoint's fingerprint differs from this run's.
        """
        fingerprint = self._fingerprint()
        if not self.state_path.exists():
            state: PipelineState = {"stage": "extract", "inputs": fingerprint}
            self.state = state
            self._save()
            return state

        state = read_state(self.state_path)
        saved = state.get("inputs")
        if saved is not None:
            check_fingerprint(self.state_path, saved, fingerprint)
        else:  # checkpoint from an older version: adopt it
            state["inputs"] = fingerprint

        # Older versions encoded per-chapter files and searched windows per chapter.
        if state["stage"] == "encode":
            state["stage"] = "assemble"
        if state["stage"] == "align" and not self.silences_path.exists():
            state["stage"] = "scan"
        # A book converted earlier can still get its readaloud later.
        if (
            state["stage"] == "done"
            and self.readaloud is not None
            and "final_output" in state
            and "readaloud_output" not in state
        ):
            state["stage"] = "readaloud"
        self.state = state
        self._save()
        return state

    @property
    def stages(self) -> tuple[str, ...]:
        """The stages this pipeline will run (``readaloud`` only when enabled)."""
        return STAGES if self.readaloud is not None else STAGES[:-1]

    def _report(self, done: float, total: float) -> None:
        """Forward timed-stage progress (seconds of audio) to the caller and ``progress.json``."""
        if self.on_progress is not None:
            self.on_progress(done, total)
        self.progress.update(self.state["stage"], done=done, total=total, unit="audio")

    def _status(
        self,
        detail: str,
        done: float | None = None,
        total: float | None = None,
        *,
        unit: str | None = None,
        force: bool = False,
    ) -> None:
        """Say what the pipeline is doing now: to the caller's ``on_status`` and ``progress.json``."""
        if self.on_status is not None:
            self.on_status(detail, done, total)
        self.progress.update(
            self.state["stage"], detail=detail, done=done, total=total, unit=unit, force=force
        )

    def _save(self) -> None:
        """Atomically write the in-memory state to ``state_path``."""
        write_state(self.state_path, self.state)

    # --------------------------------------------------------- typed accessors
    def _epub(self) -> EpubResult:
        """The extracted EPUB data; raises if the extract stage hasn't run."""
        epub = self.state.get("epub")
        if epub is None:
            raise RuntimeError(f"{self.state_path} has no EPUB data (extract stage not done)")
        return epub

    def _duration(self) -> float:
        """The source audio duration (s); raises if the extract stage hasn't run."""
        duration = self.state.get("mp3_duration")
        if duration is None:
            raise RuntimeError(f"{self.state_path} has no audio duration (extract stage not done)")
        return duration

    def _align(self) -> AlignProgress:
        """The align-stage progress; raises if the extract stage hasn't run."""
        align = self.state.get("align")
        if align is None:
            raise RuntimeError(f"{self.state_path} has no align data (extract stage not done)")
        return align

    def _segments(self) -> list[Segment]:
        """The built chapter segments; raises if the align stage hasn't finished."""
        segments = self.state.get("segments")
        if segments is None:
            raise RuntimeError(f"{self.state_path} has no segments (align stage not done)")
        return segments

    # ---------------------------------------------------------------- extract
    def stage_extract(self, budget: float) -> bool:
        """Extract EPUB metadata and chapters and probe the audio duration.

        Args:
            budget: Wall-clock budget in seconds (unused; this stage is one step).

        Returns:
            ``True`` always; advances the stage to ``scan``.

        Raises:
            ValueError: If no chapters could be found in the EPUB.
        """
        self._status("reading the EPUB and probing the audio", force=True)
        result = extract_epub(self.epub, self.work_dir / "epub")
        if not result["chapters"]:
            raise ValueError(
                f"no chapters found in {self.epub.name}; the EPUB may use an unusual structure"
            )
        duration = ffprobe_duration(self.mp3)
        self.state["epub"] = result
        self.state["mp3_duration"] = duration
        self.state["stage"] = "scan"
        self.state["align"] = {
            "boundaries": {},
            "next": 1,
            "low_confidence": [],
            "fallback": [],
        }
        self._save()
        n = len(result["chapters"])
        logger.info(
            f"extracted: {result['meta']['title']} by {result['meta']['author']}, "
            f"{n} chapters, cover={'yes' if result['cover'] else 'no'}"
        )
        avg = duration / n
        if avg < 90 or avg > 2700:
            logger.warning(
                f"{n} chapters over {duration / 3600:.1f} h averages {avg / 60:.1f} min per "
                "chapter -- is this the right EPUB for this audio?"
            )
        return True

    # ------------------------------------------------------------------- scan
    def stage_scan(self, budget: float) -> bool:
        """Scan the whole audio file once for silences and cache the result.

        Skipped when a matching ``silences.json`` already exists.

        Args:
            budget: Wall-clock budget in seconds (unused; the scan is one pass).

        Returns:
            ``True`` always; advances the stage to ``align``.
        """
        min_silence = min(self.min_gap, COMPANION_MIN_SILENCE)
        total = self._duration()
        cached = self._read_silences_file()
        if (
            cached is None
            or cached["min_silence"] != min_silence
            or cached.get("noise_db") != SCAN_NOISE_DB
        ):
            silences = scan_silences(
                self.mp3, total, SCAN_NOISE_DB, min_silence, on_progress=self._report
            )
            tmp = self.silences_path.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(
                    {"noise_db": SCAN_NOISE_DB, "min_silence": min_silence, "silences": silences}
                )
            )
            tmp.replace(self.silences_path)
            self._silences = None
            logger.info(f"scan: found {len(silences)} silences")
        self.state["stage"] = "align"
        self._save()
        return True

    def _read_silences_file(self) -> dict[str, Any] | None:
        """Read ``silences.json``; ``None`` if missing or unreadable."""
        try:
            data: dict[str, Any] = json.loads(self.silences_path.read_text())
            return data
        except (OSError, ValueError):
            return None

    def _get_silences(self) -> SilenceIndex:
        """Return the cached scan as a searchable index (loading it on first use)."""
        if self._silences is None:
            data = self._read_silences_file()
            if data is None:
                raise FileNotFoundError(f"{self.silences_path} is missing; re-run the scan stage")
            self._silences = SilenceIndex(
                [(float(s), float(e)) for s, e in data["silences"]], self.min_gap
            )
        return self._silences

    # ------------------------------------------------------------------ align
    def stage_align(self, budget: float) -> bool:
        """Locate chapter boundaries: by silence, by transcript, or silence first then transcript.

        ``align_by="silence"`` chains silence lookups forward;
        ``"transcribe"`` finds each chapter's opening words in a transcript;
        ``"auto"`` starts with silence and switches to the transcript when
        silence fails or looks unreliable (see :func:`silence_doubts`). The
        method in use is kept in the checkpoint, so a resumed run continues it.

        Args:
            budget: Wall-clock seconds to spend before checkpointing and returning.

        Returns:
            ``True`` once every boundary (and the outro decision) is done and
            the stage has advanced to ``assemble``; ``False`` if more calls are needed.

        Raises:
            RuntimeError: If the audio runs out before every chapter has a start
                (the EPUB probably has more chapters than the audiobook), and
                transcript alignment is not an option.
            StateMismatchError: If ``align_by="silence"`` meets a checkpoint
                already aligned by transcript.
        """
        align = self._align()
        method = align.get("method")
        if self.align_by == "silence" and method == "transcribe":
            raise StateMismatchError(
                f"{self.state_path} was aligned by transcript; use --fresh to align by silence"
            )
        if self.align_by == "transcribe" or method == "transcribe":
            return self._align_by_transcript(budget)
        n_chapters = len(self._epub()["chapters"])
        try:
            finished = self._align_by_silence(budget)
        except RuntimeError as e:
            if self.align_by == "auto":
                return self._switch_to_transcript(str(e), cause=e)
            raise RuntimeError(
                f"{e}; if the recording has no pause between chapters, try --align-by transcribe"
            ) from e
        if not finished:
            return False
        if self.align_by == "auto":
            reason = self._silence_doubts(align)
            if reason:
                return self._switch_to_transcript(reason)
        return self._finish_align(align, n_chapters, self._duration())

    def _silence_doubts(self, align: AlignProgress) -> str | None:
        """Why the finished silence placement looks unreliable, or ``None`` (see :func:`silence_doubts`)."""
        chapters = self._epub()["chapters"]
        segments = build_segments(
            chapters,
            align["boundaries"],
            total_dur=self._duration(),
            outro_start=None,
            intro_title=self.intro_title,
            outro_title=self.outro_title,
        )
        return silence_doubts(len(chapters), align["fallback"], len(deviating_segments(segments)))

    def _switch_to_transcript(self, reason: str, cause: RuntimeError | None = None) -> bool:
        """Discard the silence placement and start over by transcript (``align_by="auto"``).

        Without whisper.cpp there is nothing to switch to: a silence failure is
        re-raised with a hint, and a finished-but-doubtful silence placement is
        kept with a warning, so such users see what they always saw.

        Args:
            reason: Why silence is not trusted, for the log.
            cause: The silence error being handled, if it failed outright.

        Returns:
            ``False`` (the transcript pass runs on the next call) or, when whisper
            is missing and silence had finished, the result of finishing it.

        Raises:
            RuntimeError: Re-raising ``cause`` with a hint, when whisper is missing.
        """
        align = self._align()
        try:
            find_whisper(self.whisper_model)
        except WhisperError as missing:
            if cause is not None:
                raise RuntimeError(
                    f"{cause}; transcript alignment could not be tried ({missing})"
                ) from cause
            logger.warning(
                f"{reason}; transcript alignment is not available ({missing}), "
                "so the silence placement is kept"
            )
            return self._finish_align(align, len(self._epub()["chapters"]), self._duration())
        logger.warning(f"{reason}; switching to transcript alignment")
        align["boundaries"] = {}
        align["next"] = 1
        align["low_confidence"] = []
        align["fallback"] = []
        align["unmatched"] = []
        align["method"] = "transcribe"
        align.pop("outro_start", None)
        self.state.pop("segments", None)
        self._save()
        self._status(f"switching to transcript alignment: {reason}", 0, None, force=True)
        return False

    def _align_by_silence(self, budget: float) -> bool:
        """Chain silence lookups forward from each chapter's detected start.

        Args:
            budget: Wall-clock seconds to spend before checkpointing and returning.

        Returns:
            Whether every chapter now has a start.

        Raises:
            RuntimeError: If the audio runs out before every chapter has a start.
        """
        chapters = self._epub()["chapters"]
        n_chapters = len(chapters)
        total_dur = self._duration()
        align = self._align()

        total_words = sum(c["word_count"] for c in chapters)
        secs_per_word = total_dur / total_words if total_words else 0.0

        align["method"] = "silence"
        if "1" not in align["boundaries"]:
            actual = 0.0
            if self.detect_intro:
                # Only trust this as "end of intro" if it's early (a real
                # intro/credits segment is short) -- otherwise a book with
                # no intro at all would have chapter 1's own internal
                # transition gap mistaken for "end of intro", silently
                # shifting every chapter label by one for the whole book.
                found, _conf, _skipped = self._get_silences().find_gap(
                    0.0, total_dur, max_span=600.0
                )
                if found is not None and found <= self.intro_max_len:
                    actual = found
            align["boundaries"]["1"] = actual
            align["next"] = 2
            self._save()

        t0 = time.monotonic()
        while align["next"] <= n_chapters and (time.monotonic() - t0) < budget:
            i = align["next"]
            prev_actual = align["boundaries"][str(i - 1)]
            # Chapter i-1 spans prev_actual..(this boundary); a gap far shorter
            # than its word count implies is a pause inside the chapter.
            min_offset = chapters[i - 2]["word_count"] * secs_per_word * MIN_EARLY_FRACTION
            actual_or_none, confidence, skipped = self._get_silences().find_gap(
                prev_actual, total_dur, min_offset=min_offset
            )
            if skipped:
                logger.info(
                    f"chapter {i}: ignored {skipped} gap(s) earlier than "
                    f"{min_offset:.0f}s after the previous chapter (scene break?)"
                )
            if actual_or_none is None:
                if prev_actual >= total_dur - 1.0:
                    raise RuntimeError(
                        f"ran out of audio before chapter {i} ({chapters[i - 1]['title']!r}): "
                        f"the EPUB has {n_chapters} chapters but the audio ends too soon"
                    )
                actual = min(prev_actual + FALLBACK_SPAN, total_dur)
                align["fallback"].append(i)
                logger.warning(
                    f"chapter {i}: no chapter gap found ({confidence}); placed at {actual:.0f}s"
                )
            else:
                actual = actual_or_none
                if confidence == "low":
                    align["low_confidence"].append(i)
            if actual >= total_dur - 1.0:
                raise RuntimeError(
                    f"chapter {i} ({chapters[i - 1]['title']!r}) would start at the very end of "
                    f"the audio ({actual:.0f}s of {total_dur:.0f}s); the EPUB and audio don't match"
                )
            align["boundaries"][str(i)] = actual
            align["next"] = i + 1
            self._save()
            self._status(
                f"chapter {i}/{n_chapters} placed at {actual:.0f}s ({chapters[i - 1]['title']})",
                i,
                n_chapters,
                unit="chapters",
            )

        return align["next"] > n_chapters

    def _finish_align(self, align: AlignProgress, n_chapters: int, total_dur: float) -> bool:
        """Decide the outro and, once every chapter is placed, build the segments.

        Returns:
            ``True`` once the stage has advanced to ``assemble``; ``False`` if
            chapters remain to be located.
        """
        done = align["next"] > n_chapters
        if done and "outro_start" not in align:
            outro_start = None
            if self.detect_outro:
                last_actual = align["boundaries"][str(n_chapters)]
                candidate, _conf, _skipped = self._get_silences().find_gap(
                    last_actual, total_dur, max_span=self.outro_max_search
                )
                # Only trust it as "start of outro" if what's left after it
                # is short -- a real outro (end credits) is brief. A long
                # remainder means we probably just found an ordinary pause
                # inside the last chapter's own content.
                if candidate is not None and (total_dur - candidate) <= self.outro_max_tail:
                    outro_start = candidate
            align["outro_start"] = outro_start  # None -> no separate outro segment
            self._save()

        logger.debug(
            f"align: {align['next'] - 1}/{n_chapters} chapters "
            f"(low_confidence={len(align['low_confidence'])} fallback={len(align['fallback'])})"
        )

        if done and "outro_start" in align:
            self.state["stage"] = "assemble"
            self._save()
            self._build_segments()
            return True
        return False

    def _align_by_transcript(self, budget: float) -> bool:
        """Locate chapters by finding their opening words in a transcript.

        For each chapter, estimates its start from the last confirmed boundary
        and the word counts since, transcribes a window around that estimate
        and matches the EPUB's opening words against it. A chapter that cannot
        be matched is recorded in ``unmatched`` and placed by interpolation
        once its neighbours are known.

        Args:
            budget: Wall-clock seconds to spend before checkpointing and returning.

        Returns:
            ``True`` once every chapter is placed and the stage has advanced to
            ``assemble``; ``False`` if more calls are needed.

        Raises:
            WhisperError: If whisper.cpp or its model is missing or fails.
        """
        chapters = self._epub()["chapters"]
        n_chapters = len(chapters)
        total_dur = self._duration()
        align = self._align()
        words = [c["word_count"] for c in chapters]
        default_rate = total_dur / sum(words) if sum(words) else 0.0
        unmatched = align.setdefault("unmatched", [])
        align["method"] = "transcribe"
        binary, model = find_whisper(self.whisper_model)
        scratch = self.work_dir / "whisper"

        t0 = time.monotonic()
        while align["next"] <= n_chapters and (time.monotonic() - t0) < budget:
            i = align["next"]
            confirmed = {
                int(k): v for k, v in align["boundaries"].items() if int(k) not in unmatched
            }
            anchor = max(confirmed, default=1)
            anchor_start = confirmed.get(anchor, 0.0)
            rate = estimate_rate(confirmed, words, default_rate)
            expected = expected_start(anchor, anchor_start, i, words, rate)

            hit, used = None, ""
            for widen in WIDEN_STEPS:
                if i == 1:
                    lo, hi = 0.0, min(total_dur, CHAPTER_ONE_WINDOW * widen)
                else:
                    lo, hi = search_window(expected, anchor_start, total_dur, widen)
                self._status(
                    f"chapter {i}/{n_chapters} ({chapters[i - 1]['title']}): listening "
                    f"{lo:.0f}-{hi:.0f}s, attempt {WIDEN_STEPS.index(widen) + 1}/{len(WIDEN_STEPS)}",
                    i - 1,
                    n_chapters,
                    unit="chapters",
                    force=True,
                )
                heard = transcribe_window(
                    self.mp3, lo, hi - lo, binary=binary, model=model, scratch=scratch
                )
                chapter = chapters[i - 1]
                candidates = (
                    ("opening", chapter.get("opening", "")),
                    ("body_opening", chapter.get("body_opening", "")),
                )
                for key, text in candidates:
                    if text and (hit := find_opening(heard, text)):
                        used = key
                        break
                if hit:
                    break

            if hit is None:
                unmatched.append(i)
                logger.warning(
                    f"chapter {i} ({chapters[i - 1]['title']!r}): opening words not heard "
                    f"near {expected:.0f}s; placing it by interpolation"
                )
            else:
                boundary = round(boundary_in_gap(hit.prev_end, hit.start), 3)
                align["boundaries"][str(i)] = boundary
                if hit.score < CONFIDENT_SCORE or used == "body_opening":
                    align["low_confidence"].append(i)
                logger.info(
                    f"chapter {i}: heard at {boundary:.0f}s "
                    f"(expected {expected:.0f}s, score {hit.score:.2f}, {used})"
                )
            align["next"] = i + 1
            self._save()
            self._status(
                f"chapter {i}/{n_chapters} "
                + (
                    "not heard; will be interpolated"
                    if hit is None
                    else f"heard at {boundary:.0f}s"
                ),
                i,
                n_chapters,
                unit="chapters",
                force=True,
            )

        if align["next"] > n_chapters and unmatched:
            self._interpolate_unmatched(align, words, total_dur)
        return self._finish_align(align, n_chapters, total_dur)

    @staticmethod
    def _interpolate_unmatched(align: AlignProgress, words: list[int], total_dur: float) -> None:
        """Place unmatched chapters between their matched neighbours, by word count."""
        unmatched = set(align.get("unmatched", []))
        done = {int(k) for k in align["boundaries"]}
        if unmatched <= done:  # already interpolated on an earlier call
            return
        known: dict[int, float] = {int(k): v for k, v in align["boundaries"].items()}
        known = {k: v for k, v in known.items() if k not in unmatched}
        known.setdefault(1, 0.0)  # chapter 1 unmatched: assume the audio starts with it
        known[len(words) + 1] = total_dur  # virtual end, bounds trailing unmatched chapters
        order = sorted(known)
        for i in sorted(unmatched):
            if i == 1:
                align["boundaries"]["1"] = 0.0
                continue
            lo = max(k for k in order if k < i)
            hi = min(k for k in order if k > i)
            span = sum(words[lo - 1 : hi - 1]) or 1
            frac = sum(words[lo - 1 : i - 1]) / span
            align["boundaries"][str(i)] = round(known[lo] + frac * (known[hi] - known[lo]), 3)

    def _build_segments(self) -> None:
        """Turn the detected boundaries into ``state["segments"]``.

        Adds optional intro and outro segments, records each duration, and
        warns about any segment whose length deviates more than 30% from what
        its EPUB word count predicts.
        """
        align = self._align()
        segments = build_segments(
            self._epub()["chapters"],
            align["boundaries"],
            total_dur=self._duration(),
            outro_start=align.get("outro_start"),
            intro_title=self.intro_title,
            outro_title=self.outro_title,
            low_confidence=align["low_confidence"],
            fallback=align["fallback"],
            unmatched=align.get("unmatched", ()),
        )
        self.state["segments"] = segments
        self._save()

        flagged = deviating_segments(segments)
        if flagged:
            detail = "; ".join(f"#{n} {t!r}: {d}s vs ~{e}s" for n, t, d, e in flagged)
            logger.warning(
                f"{len(flagged)} segment(s) deviate >30% from the word-count estimate "
                f"(possible merged/skipped chapter) -- review before trusting output: {detail}"
            )

    # --------------------------------------------------------------- assemble
    def stage_assemble(self, budget: float) -> bool:
        """Encode the audio once to AAC and mux the M4B with chapters and cover.

        Args:
            budget: Wall-clock budget in seconds (unused; this stage is one step).

        Returns:
            ``True`` always; advances the stage to ``deliver``.

        Raises:
            FFmpegError: If ffmpeg fails.
        """
        segments = self._segments()
        meta = self._epub()["meta"]
        cover = self._epub()["cover"]
        total = self._duration()

        meta_path = self.work_dir / "chapters.ffmeta"
        meta_path.write_text(build_ffmetadata(meta, segments), encoding="utf-8")

        final_path = self.work_dir / (safe_filename(meta["title"] or self.epub.stem) + ".m4b")
        tmp_path = final_path.with_name(final_path.name + ".tmp")
        cmd = [
            "ffmpeg",
            "-y",
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostats",
            "-progress",
            "pipe:1",
            "-i",
            str(self.mp3),
        ]
        if cover:
            cmd += ["-i", cover]
        cmd += ["-i", str(meta_path)]
        meta_idx = 2 if cover else 1
        cmd += ["-map", "0:a", "-map_metadata", str(meta_idx), "-map_chapters", str(meta_idx)]
        if cover:
            cmd += [
                "-map",
                "1:v",
                "-c:v",
                "mjpeg",
                "-disposition:v",
                "attached_pic",
                "-metadata:s:v",
                "title=Album cover",
                "-metadata:s:v",
                "comment=Cover (front)",
            ]
        cmd += [
            "-c:a",
            "aac",
            "-b:a",
            self.bitrate,
            "-ac",
            str(AUDIO_CHANNELS),
            "-ar",
            str(AUDIO_SAMPLE_RATE),
            "-f",
            "mp4",
            str(tmp_path),
        ]
        logger.info("encoding the m4b (single pass)...")
        _run_with_progress(cmd, total, self._report)
        tmp_path.replace(final_path)

        out_dur = ffprobe_duration(final_path)
        if abs(out_dur - total) > 3.0:
            logger.warning(
                f"output is {out_dur:.1f}s but the source is {total:.1f}s -- check the result"
            )

        self.state["assembled_path"] = str(final_path)
        self.state["stage"] = "deliver"
        self.state["deliver_bytes_done"] = 0
        self._save()
        logger.info(f"assembled: {final_path} ({final_path.stat().st_size / 1e6:.0f} MB)")
        return True

    # ---------------------------------------------------------------- deliver
    def stage_deliver(self, budget: float, chunk_mb: int = 64) -> bool:
        """Move or copy the assembled M4B into ``out_dir``, resumably and atomically.

        On the same filesystem the file is renamed. Otherwise it is copied in
        chunks to ``<name>.part`` (resumable) and renamed when complete, so a
        partial file never sits under the final name.

        Args:
            budget: Wall-clock seconds to spend before checkpointing and returning.
            chunk_mb: Copy chunk size in MiB.

        Returns:
            ``True`` once the file is in place (stage becomes ``done``); ``False``
            if more calls are needed.

        Raises:
            FileExistsError: If the destination exists and ``overwrite`` is false.
        """
        assembled = self.state.get("assembled_path")
        if assembled is None:
            raise RuntimeError(f"{self.state_path} has no assembled file (assemble stage not done)")
        src = Path(assembled)
        dst = self.out_dir / src.name
        part = dst.with_name(dst.name + ".part")
        self.out_dir.mkdir(parents=True, exist_ok=True)

        if not src.exists():
            # Crashed after the move but before the checkpoint was saved.
            if dst.exists():
                return self._finish_delivery(dst, dst.stat().st_size)
            raise FileNotFoundError(f"{src} is gone and {dst} was never delivered")
        total = src.stat().st_size
        done = self.state.get("deliver_bytes_done", 0)

        if not part.exists() and dst.exists() and done >= total and dst.stat().st_size == total:
            # Crashed after the copy was promoted but before ``src`` was removed.
            src.unlink()
            return self._finish_delivery(dst, total)
        if dst.exists() and not self.overwrite:
            raise FileExistsError(f"{dst} already exists (use overwrite to replace it)")

        if part.exists():
            done = min(done, part.stat().st_size)
        else:
            done = 0
            if _same_filesystem(src, self.out_dir):
                os.replace(src, dst)  # same filesystem: instant and atomic
                return self._finish_delivery(dst, total)

        chunk = chunk_mb * 1024 * 1024
        t0 = time.monotonic()
        with open(src, "rb") as fsrc, open(part, "r+b" if part.exists() else "wb") as fdst:
            fsrc.seek(done)
            fdst.seek(done)
            fdst.truncate(done)
            while done < total and (time.monotonic() - t0) < budget:
                buf = fsrc.read(chunk)
                if not buf:
                    break
                fdst.write(buf)
                done += len(buf)
                self.state["deliver_bytes_done"] = done
                self._save()
            fdst.flush()
            os.fsync(fdst.fileno())

        logger.debug(f"deliver: {done / 1e6:.0f}/{total / 1e6:.0f} MB -> {dst}")
        if done >= total:
            os.replace(part, dst)
            src.unlink()
            return self._finish_delivery(dst, total)
        return False

    def _finish_delivery(self, dst: Path, total: int) -> bool:
        """Record a completed delivery and mark the pipeline done."""
        self.state["deliver_bytes_done"] = total
        self.state["stage"] = "readaloud" if self.readaloud is not None else "done"
        self.state["final_output"] = str(dst)
        self._save()
        return True

    # -------------------------------------------------------------- readaloud
    def stage_readaloud(self, budget: float) -> bool:
        """Align the delivered M4B with the EPUB into an EPUB 3 with Media Overlays.

        The aligner runs to completion in one call (it can take a long time) and
        writes to a ``.partial.epub`` file that is renamed only on success, so an
        interrupted run never leaves a truncated result under the final name.

        Args:
            budget: Wall-clock budget in seconds (unused; alignment is one step).

        Returns:
            ``True`` always; advances the stage to ``done``.

        Raises:
            FileExistsError: If the output exists and ``overwrite`` is false.
            AlignError: If alignment fails (the M4B is already delivered, so
                re-running resumes at this stage).
        """
        final = self.state.get("final_output")
        if final is None:
            raise RuntimeError(f"{self.state_path} has no delivered file (deliver stage not done)")
        m4b = Path(final)
        out = m4b.with_name(f"{self.epub.stem} (aligned).epub")
        if out.exists() and not self.overwrite:
            raise FileExistsError(f"{out} already exists (use overwrite to replace it)")
        partial = out.with_name(f"{out.stem}.partial.epub")
        partial.unlink(missing_ok=True)  # leftover from an interrupted run

        logger.info(f"aligning {m4b.name} with {self.epub.name} (readaloud)...")
        align_book(m4b, self.epub, partial, self.readaloud)
        partial.replace(out)

        self.state["readaloud_output"] = str(out)
        self.state["stage"] = "done"
        self._save()
        logger.info(f"readaloud: {out} ({out.stat().st_size / 1e6:.0f} MB)")
        return True

    def run(self, budget: float) -> bool:
        """Run one bounded slice of the current stage.

        Args:
            budget: Wall-clock seconds the stage may spend.

        Returns:
            Whether the stage finished (``True`` also when already ``done``).

        Raises:
            ValueError: If the checkpoint names an unknown stage.
        """
        stage = self.state["stage"]
        if stage == "done":
            return True
        self.progress.update(stage, force=True)
        methods: dict[str, Callable[[float], bool]] = {
            "extract": self.stage_extract,
            "scan": self.stage_scan,
            "align": self.stage_align,
            "assemble": self.stage_assemble,
            "deliver": self.stage_deliver,
            "readaloud": self.stage_readaloud,
        }
        method = methods.get(stage)
        if method is None:
            raise ValueError(f"unknown pipeline stage {stage!r} in {self.state_path}")
        return method(budget)
