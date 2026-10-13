"""
Resumable MP3/M4A (+ EPUB) -> chaptered M4B pipeline.

Drive it through the ``audiobook-kit`` CLI (:mod:`audiobook_kit.cli`), or
call :meth:`Pipeline.run` repeatedly until ``state["stage"] == "done"``. Each
call does a bounded slice of work and checkpoints to
``<work_dir>/state.json``, so an interrupted run resumes where it left off.

Stages: extract -> scan -> align -> assemble -> deliver [-> readaloud]

The optional ``readaloud`` stage (enabled by passing :class:`AlignOptions`) aligns the
delivered M4B with the EPUB into an EPUB 3 with Media Overlays via
:mod:`audiobook_kit.stalign`.

Why this approach (learned the hard way on "Mark of the Fool 10"):
- Don't derive chapter boundaries from a book-seller's displayed
    chapter durations (they're rounded to the second and drift by 1-2
    minutes over a long book) and don't anchor purely on EPUB word counts
    either (local narration pace varies enough -- dialogue-heavy vs
    descriptive passages -- to be off by several minutes in places).
- Every chapter transition in a professionally narrated audiobook has a
    real, clean ~3.5s+ silence. It is reliably followed 3-9s later by a
    second, shorter ~1.5-3s "companion" gap (some kind of
    announcement/sting before the actual chapter content starts) that is
    NOT the real boundary -- picking the wrong one of that pair is what
    causes chapters to start mid-sentence.
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

import bisect
import json
import os
import re
import subprocess
import time
from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import Any, NotRequired, TypedDict

from loguru import logger

from audiobook_kit.align_chapters import FFmpegError, ffprobe_duration, scan_silences
from audiobook_kit.epub_extract import EpubMeta, EpubResult, extract_epub
from audiobook_kit.stalign import AlignOptions, align_book

STAGES = ("extract", "scan", "align", "assemble", "deliver", "readaloud")

SCAN_NOISE_DB = -30.0  # silence threshold used for the whole-file scan
COMPANION_MIN_SILENCE = 1.5  # length (s) of the short gap that follows a chapter gap
COMPANION_WINDOW = 10.0  # how soon (s) after a chapter gap the companion appears
MIN_EARLY_FRACTION = 0.4  # a gap earlier than this share of the word-count estimate is rejected
AUDIO_CHANNELS = 1  # output is mono ...
AUDIO_SAMPLE_RATE = 32000  # ... at 32 kHz, which is plenty for narrated speech

ProgressFn = Callable[[float, float], None]


class Segment(TypedDict):
    """One output chapter: a time range of the source audio."""

    n: int
    title: str
    start: float
    end: float
    duration: float


class AlignProgress(TypedDict):
    """Checkpointed progress of the align stage."""

    boundaries: dict[str, float]  # chapter number (as str, JSON keys) -> start in seconds
    next: int  # next chapter number to locate
    low_confidence: list[int]  # chapters whose boundary lacked a companion gap
    fallback: list[int]  # chapters placed by a fixed offset (no gap found)
    outro_start: NotRequired[float | None]  # set once the outro search has run


class PipelineState(TypedDict):
    """Checkpoint persisted to ``state.json``; keys appear as stages complete."""

    stage: str  # one of STAGES, or "done"
    inputs: NotRequired[dict[str, Any]]  # fingerprint of the files/options this state belongs to
    epub: NotRequired[EpubResult]
    mp3_duration: NotRequired[float]
    align: NotRequired[AlignProgress]
    segments: NotRequired[list[Segment]]
    assembled_path: NotRequired[str]
    deliver_bytes_done: NotRequired[int]
    final_output: NotRequired[str]
    readaloud_output: NotRequired[str]  # the aligned EPUB 3, once the readaloud stage is done


class StateMismatchError(RuntimeError):
    """A saved checkpoint was made for different input files or options."""


def _segment(n: int, title: str, start: float, end: float) -> Segment:
    """Build a :class:`Segment`, rounding times to milliseconds and computing its duration."""
    start, end = round(start, 3), round(end, 3)
    return {"n": n, "title": title, "start": start, "end": end, "duration": round(end - start, 3)}


def slugify(s: str) -> str:
    """Reduce ``s`` to an alphanumeric/underscore slug of at most 80 characters.

    Runs of other characters become a single underscore; leading and trailing
    underscores are stripped.
    """
    return re.sub(r"[^A-Za-z0-9]+", "_", s).strip("_")[:80]


def safe_filename(s: str, fallback: str = "audiobook") -> str:
    """Make ``s`` usable as a filename while keeping it readable.

    Path separators and characters illegal on common filesystems become
    spaces; whitespace is collapsed and the result is capped at 120 characters.
    """
    cleaned = " ".join(re.sub(r'[\\/:*?"<>|\x00-\x1f]+', " ", s).split())[:120].strip(" .")
    return cleaned or fallback


def ffmeta_escape(s: str) -> str:
    """Escape a value for an ``FFMETADATA1`` file (``\\``, ``=``, ``;``, ``#`` and newlines)."""
    s = s.replace("\r", "").replace("\\", "\\\\")
    for char in "=;#":
        s = s.replace(char, "\\" + char)
    return s.replace("\n", "\\\n")


def build_ffmetadata(meta: EpubMeta, segments: list[Segment]) -> str:
    """Build the ``FFMETADATA1`` text: book tags plus one ``[CHAPTER]`` per segment.

    Args:
        meta: Book metadata from the EPUB.
        segments: Contiguous segments in order; times are seconds from the start of the audio.

    Returns:
        The file contents (newline-terminated).
    """
    lines = [
        ";FFMETADATA1",
        f"title={ffmeta_escape(meta['title'])}",
        f"artist={ffmeta_escape(meta['author'])}",
        f"album={ffmeta_escape(meta['title'])}",
        f"album_artist={ffmeta_escape(meta['author'])}",
        "genre=Audiobook",
    ]
    if meta.get("year"):
        lines.append(f"date={ffmeta_escape(meta['year'])}")
    if meta.get("series"):
        lines.append(f"show={ffmeta_escape(meta['series'])}")
        lines.append(f"episode_id={ffmeta_escape(meta['series_index'])}")
    for s in segments:
        lines += [
            "[CHAPTER]",
            "TIMEBASE=1/1000",
            f"START={round(s['start'] * 1000)}",
            f"END={round(s['end'] * 1000)}",
            f"title={ffmeta_escape(s['title'])}",
        ]
    return "\n".join(lines) + "\n"


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
            on_progress: Progress sink for the scan and assemble stages.

        Raises:
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
        self.on_progress = on_progress

        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.state_path = self.work_dir / "state.json"
        self.silences_path = self.work_dir / "silences.json"
        self._silences: list[tuple[float, float]] | None = None
        self._silence_ends: list[float] = []
        self.state: PipelineState = self._load_state()

    # ------------------------------------------------------------------ state
    def _fingerprint(self) -> dict[str, Any]:
        """Identify the inputs and options a checkpoint belongs to."""
        return {
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

        state = json.loads(self.state_path.read_text())
        saved = state.get("inputs")
        if saved is not None:
            changed = sorted(k for k in fingerprint if saved.get(k) != fingerprint[k])
            if changed:
                raise StateMismatchError(
                    f"{self.state_path} was made for different input/options "
                    f"({', '.join(changed)}). Re-run with the original settings, or "
                    "start over (delete the work directory / use --fresh)."
                )
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

    def _save(self) -> None:
        """Atomically write the in-memory state to ``state_path``."""
        tmp = self.state_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.state, indent=2, ensure_ascii=False))
        tmp.replace(self.state_path)

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
            f"{n} chapters, cover={'yes' if result['cover'] else 'no'}")
        avg = duration / n
        if avg < 90 or avg > 2700:
            logger.warning(
                f"{n} chapters over {duration / 3600:.1f} h averages {avg / 60:.1f} min per "
                "chapter -- is this the right EPUB for this audio?")
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
        if cached is None or cached["min_silence"] != min_silence:
            silences = scan_silences(
                self.mp3, total, SCAN_NOISE_DB, min_silence, on_progress=self.on_progress
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

    def _get_silences(self) -> list[tuple[float, float]]:
        """Return the cached scan (loading it on first use)."""
        if self._silences is None:
            data = self._read_silences_file()
            if data is None:
                raise FileNotFoundError(f"{self.silences_path} is missing; re-run the scan stage")
            self._silences = [(float(s), float(e)) for s, e in data["silences"]]
            self._silence_ends = [e for _s, e in self._silences]
        return self._silences

    # ------------------------------------------------------------------ align
    def _find_gap(
        self,
        after: float,
        total_dur: float,
        max_span: float = 4200.0,
        min_offset: float = 0.0,
    ) -> tuple[float | None, str, int]:
        """Find the first chapter-transition silence after ``after``.

        Looks at silences starting 3 s after ``after`` and within ``max_span``
        seconds of that. The midpoint of the first silence of at least
        ``min_gap`` seconds (measured inside that range) whose midpoint is at
        least ``min_offset`` after ``after`` is returned. Confidence is
        ``"high"`` if a companion gap follows within 10 s, else ``"low"``.

        Args:
            after: Position (s) of the previous boundary to search from.
            total_dur: Total audio duration (s).
            max_span: Longest stretch (s) to search.
            min_offset: Skip gaps whose midpoint is closer than this to ``after``
                (used to reject pauses inside a chapter).

        Returns:
            ``(position, confidence, skipped)``: confidence is ``"high"`` or
            ``"low"``; ``skipped`` counts qualifying gaps rejected as too early.
            ``(None, "eof", 0)`` if the search started at end of file, or
            ``(None, "fallback", skipped)`` if no gap was found.
        """
        clip_start = after + 3.0
        if clip_start >= total_dur:
            return None, "eof", 0
        clip_end = min(clip_start + max_span, total_dur)
        silences = self._get_silences()
        skipped = 0
        k = bisect.bisect_right(self._silence_ends, clip_start)
        while k < len(silences):
            s, e = silences[k]
            if s >= clip_end:
                break
            s, e_clamped = max(s, clip_start), min(e, clip_end)
            if e_clamped - s >= self.min_gap:
                mid = (s + e_clamped) / 2
                if mid - after < min_offset:
                    skipped += 1
                else:
                    return mid, ("high" if self._has_companion(k, e_clamped) else "low"), skipped
            k += 1
        return None, "fallback", skipped

    def _has_companion(self, k: int, gap_end: float) -> bool:
        """Whether a short companion silence follows silence ``k`` within 10 s of ``gap_end``."""
        silences = self._get_silences()
        window_end = gap_end + COMPANION_WINDOW
        for s2, e2 in silences[k + 1 :]:
            if s2 >= window_end:
                break
            if min(e2, window_end) - max(s2, gap_end) >= COMPANION_MIN_SILENCE:
                return True
        return False

    def stage_align(self, budget: float) -> bool:
        """Locate chapter boundaries by chaining silence lookups forward.

        Args:
            budget: Wall-clock seconds to spend before checkpointing and returning.

        Returns:
            ``True`` once every boundary (and the outro decision) is done and
            the stage has advanced to ``assemble``; ``False`` if more calls are needed.

        Raises:
            RuntimeError: If the audio runs out before every chapter has a start
                (the EPUB probably has more chapters than the audiobook).
        """
        chapters = self._epub()["chapters"]
        n_chapters = len(chapters)
        total_dur = self._duration()
        align = self._align()

        total_words = sum(c["word_count"] for c in chapters)
        secs_per_word = total_dur / total_words if total_words else 0.0

        if "1" not in align["boundaries"]:
            actual = 0.0
            if self.detect_intro:
                # Only trust this as "end of intro" if it's early (a real
                # intro/credits segment is short) -- otherwise a book with
                # no intro at all would have chapter 1's own internal
                # transition gap mistaken for "end of intro", silently
                # shifting every chapter label by one for the whole book.
                found, _conf, _skipped = self._find_gap(0.0, total_dur, max_span=600.0)
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
            actual_or_none, confidence, skipped = self._find_gap(
                prev_actual, total_dur, min_offset=min_offset
            )
            if skipped:
                logger.info(
                    f"chapter {i}: ignored {skipped} gap(s) earlier than "
                    f"{min_offset:.0f}s after the previous chapter (scene break?)")
            if actual_or_none is None:
                if prev_actual >= total_dur - 1.0:
                    raise RuntimeError(
                        f"ran out of audio before chapter {i} ({chapters[i - 1]['title']!r}): "
                        f"the EPUB has {n_chapters} chapters but the audio ends too soon"
                    )
                actual = min(prev_actual + 900.0, total_dur)
                align["fallback"].append(i)
                logger.warning(
                    f"chapter {i}: no chapter gap found ({confidence}); placed at {actual:.0f}s")
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

        done = align["next"] > n_chapters
        if done and "outro_start" not in align:
            outro_start = None
            if self.detect_outro:
                last_actual = align["boundaries"][str(n_chapters)]
                candidate, _conf, _skipped = self._find_gap(
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
            f"(low_confidence={len(align['low_confidence'])} fallback={len(align['fallback'])})")

        if done and "outro_start" in align:
            self.state["stage"] = "assemble"
            self._save()
            self._build_segments()
            return True
        return False

    def _build_segments(self) -> None:
        """Turn the detected boundaries into ``state["segments"]``.

        Adds optional intro and outro segments, records each duration, and
        warns about any segment whose length deviates more than 30% from what
        its EPUB word count predicts.
        """
        chapters = self._epub()["chapters"]
        align = self._align()
        total_dur = self._duration()
        segments: list[Segment] = []
        words: list[int | None] = []  # parallel to segments; None for intro/outro
        idx = 0
        if align["boundaries"]["1"] > 5.0:  # meaningful intro exists
            segments.append(_segment(idx, self.intro_title, 0.0, align["boundaries"]["1"]))
            words.append(None)
            idx += 1
        for i, c in enumerate(chapters, start=1):
            start = align["boundaries"][str(i)]
            if i < len(chapters):
                end = align["boundaries"][str(i + 1)]
            else:
                end = align.get("outro_start") or total_dur
            segments.append(_segment(idx, c["title"], start, end))
            words.append(c["word_count"])
            idx += 1
        outro_start = align.get("outro_start")
        if outro_start and (total_dur - outro_start) > 2.0:
            segments.append(_segment(idx, self.outro_title, outro_start, total_dur))
            words.append(None)
        self.state["segments"] = segments
        self._save()

        # sanity check against word counts
        total_words = sum(w for w in words if w)
        speech = sum(s["duration"] for s, w in zip(segments, words, strict=True) if w is not None)
        rate = speech / total_words if total_words else 0
        flagged = []
        for s, wc in zip(segments, words, strict=True):
            if wc is None:
                continue
            exp = wc * rate
            if exp > 0 and abs(s["duration"] - exp) / exp > 0.30:
                flagged.append((s["n"], s["title"], round(s["duration"], 1), round(exp, 1)))
        if flagged:
            detail = "; ".join(f"#{n} {t!r}: {d}s vs ~{e}s" for n, t, d, e in flagged)
            logger.warning(
                f"{len(flagged)} segment(s) deviate >30% from the word-count estimate "
                f"(possible merged/skipped chapter) -- review before trusting output: {detail}")

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
        _run_with_progress(cmd, total, self.on_progress)
        tmp_path.replace(final_path)

        out_dur = ffprobe_duration(final_path)
        if abs(out_dur - total) > 3.0:
            logger.warning(
                f"output is {out_dur:.1f}s but the source is {total:.1f}s -- check the result")

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
