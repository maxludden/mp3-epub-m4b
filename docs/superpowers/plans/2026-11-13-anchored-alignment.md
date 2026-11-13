# Anchored Alignment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A new default alignment mode, `anchored`, that finds and confirms the start and end of the book by transcript, length-checks every chapter (±10% or ±15 s), and asks the person (terminal or Claude) whenever a check fails. Nothing is encoded until every chapter passes.

**Architecture:** The pure matching/placement logic lives in a new `anchors.py`. Questions and answers live in `questions.py` as pure state changes. `Pipeline` gets a third align method, `_align_anchored`, which raises `NeedsInput` when a person must decide. The terminal path (`converter.py` + `prompt.py`) answers inline. The unattended path (batch, MCP job) leaves the question in `state.json`. `resolve` and the MCP `answer_question` tool apply the answer later and resume.

**Tech Stack:** Python 3.12, Typer, Rich, loguru, whisper.cpp (via `transcribe.py`), ffmpeg, FastMCP (`mcp`), pytest, mypy, ruff.

**Spec:** `docs/superpowers/specs/2026-11-13-anchored-alignment-design.md`

## Global Constraints

- Work on a branch: `git checkout -b feat/anchored-alignment` before Task 1.
- Default `--align-by` becomes `anchored` for `convert`, `batch`, and MCP `start_conversion`. `silence`, `transcribe` and `auto` keep working unchanged.
- Listen window: **10 s** from each clip's start. Edge clips: **first 5 and last 5**.
- Length check: a chapter is low confidence only if `|actual − expected| > 0.10 × expected` **and** `|actual − expected| > 15 s`.
- Sections under **50 words** between the anchors are merged into the following section.
- Start/end confirmation: the next (or previous) narrated section must match one of the **3** clips after (or before) the anchor clip, or the clip nearest that section's expected start.
- Encoding (`assemble`) never starts while `state["pending_question"]` is set or any chapter is low confidence.
- Unattended runs never block on input. `convert` exits with code **3** when it stops waiting for input.
- whisper.cpp + model required for `anchored`. If missing, fail at startup: `typer.BadParameter` with the `find_whisper` message plus "or pass --align-by silence".
- Existing match helpers are reused (`transcribe.find_opening`, `MIN_SCORE = 0.62`).
- All checks pass: `uv run pytest && uv run mypy src tests && uv run ruff check . && uv run ruff format --check .`
- Commit messages end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## Review Focus

1. **Last clip is mid-chapter** (the final chapter has more than 5 scene breaks, so none of the last 5 clips starts a section). The end should still be found via the clip nearest the final section's expected start, without asking. Test in Task 2.
2. **Opening credits occupy clip 0** ("This is Audible. Brandon Sanderson, The Way of Kings…"). The start should be clip 1 ↔ Chapter 1, and clip 0 becomes the intro (with `--detect-intro`) or folds into chapter 1. Test in Task 2.
3. **A person's answer that conflicts with placement** (they pick a section that leaves too little audio for the chapters between). This should re-place and ask about the new outlier rather than crash or loop forever. Test in Task 3 (apply_answer) and Task 4 (pipeline re-asks).
4. **Resuming after an interrupt mid-transcription** should reuse cached `heard` transcripts and not re-transcribe. Test in Task 4.
5. **Non-TTY `convert`** (piped, or launched by MCP with `stdin=/dev/null`) must not call `input()`. It exits 3 with the question saved. Test in Task 5.

---

### Task 1: Every EPUB section, with kind and opening

**Files:**
- Modify: `src/mp3_epub_m4b/epub_extract.py` (add `Section`, `_extract_sections`; call it from `extract_epub`; add `sections` to `EpubResult`)
- Modify: `tests/conftest.py` (add `make_epub_with_matter`, `ANCHORED_CHAPTERS` openings)
- Test: `tests/test_epub.py`

**Interfaces:**
- Produces:
  ```python
  class Section(TypedDict):
      index: int            # 0-based spine-order position among sections
      title: str            # TOC label, else first h1-h3, else ""
      kind: Literal["front", "body", "back"]
      word_count: int
      opening: str          # first OPENING_WORDS words of body text, headings removed
  class EpubResult(TypedDict):  # adds:
      sections: NotRequired[list[Section]]
  def _extract_sections(base: Path, manifest: dict[str, ManifestItem], spine: list[str], root: etree._Element) -> list[Section]
  ```
  `make_epub_with_matter(path: Path) -> None` in conftest writes: "Title Page" (8 words), "Copyright" (30 words), the 3 `CHAPTERS` (each body = a distinct 12-word opening sentence from `ANCHORED_OPENINGS[i]` followed by `"word " * (seconds*10 - 12)`), "About the Author" (60 words). The TOC lists all six.

- [ ] **Step 1: Write the failing tests**

```python
def test_sections_include_front_and_back_matter(tmp_path: Path) -> None:
    epub = tmp_path / "b.epub"; make_epub_with_matter(epub)
    res = extract_epub(epub, tmp_path / "x")
    secs = res["sections"]
    assert [s["title"] for s in secs] == ["Title Page", "Copyright", "First Light", "Tom & Jerry", "Fate = Doom; #3", "About the Author"]
    assert [s["kind"] for s in secs] == ["front", "front", "body", "body", "body", "back"]
    assert secs[2]["opening"].startswith(ANCHORED_OPENINGS[0][:20])
    assert secs[3]["word_count"] == 600
    assert len(res["chapters"]) == 3  # existing chapter list is unchanged

def test_sections_without_toc_fall_back_to_spine_files(tmp_path: Path) -> None:
    # same book with nav removed: one section per spine file, titles from <h1>
    ...
    assert [s["title"] for s in secs][2] == "First Light"
```

- [ ] **Step 2: Run them, expect FAIL** (`KeyError: 'sections'`). Run: `uv run pytest tests/test_epub.py -k sections -v`
- [ ] **Step 3: Implement `_extract_sections`**

A section spans the spine files from its TOC entry (all entries from `_toc_sources`, numbered or not, deduped by spine index with the first entry winning, and fragments ignored) up to the next entry. Spine files before the first entry are one section each. Kind: front/back-matter headings (`_is_front_back_matter`) before the first non-matter section are `front`, those after the last non-matter section are `back`, and everything else is `body`. Use `_chapter_word_count` and `_opening_text` for counts and openings.
- [ ] **Step 4: Run tests, expect PASS** (the whole `tests/test_epub.py` too)
- [ ] **Step 5: Commit** `feat: extract every EPUB section with kind and opening`

---

### Task 2: Pure anchoring: clips, start/end matching, confirmation

**Files:**
- Create: `src/mp3_epub_m4b/anchors.py`
- Modify: `src/mp3_epub_m4b/chapters.py` (add `SilenceIndex.chapter_gaps() -> list[float]`: midpoints of silences ≥ `min_gap`)
- Test: `tests/test_anchors.py`

**Interfaces:**
- Consumes: `Section` (Task 1), `transcribe.Word`, `transcribe.find_opening`.
- Produces:
  ```python
  LISTEN_SECONDS = 10.0; EDGE_CLIPS = 5; EDGE_SECTIONS = 10; CONFIRM_CLIPS = 3
  class Clip(NamedTuple): index: int; start: float; end: float
  @dataclass(frozen=True)
  class Anchor: clip: int; section: int; score: float
  def clips_from_gaps(gaps: Sequence[float], total_dur: float) -> list[Clip]
  def edge_clips(clips: Sequence[Clip]) -> list[int]            # first 5 + last 5 indexes, deduped, ordered
  def section_score(heard: Sequence[Word], section: Section) -> float  # max find_opening score of opening and f"{title} {opening}"; 0.0 if no match
  def find_start(heard: Mapping[int, Sequence[Word]], clips: Sequence[Clip], sections: Sequence[Section]) -> Anchor | None
  def find_end(heard: Mapping[int, Sequence[Word]], clips: Sequence[Clip], sections: Sequence[Section], rate: float) -> Anchor | None
  def confirm_candidates(anchor: Anchor, direction: Literal[1, -1], clips: Sequence[Clip], expected_time: float) -> list[int]
  def confirms(heard: Mapping[int, Sequence[Word]], candidates: Sequence[int], section: Section) -> int | None  # clip that matches, or None
  def next_narrated(sections: Sequence[Section], i: int, direction: Literal[1, -1]) -> int | None  # skips sections < MIN_SECTION_WORDS
  def provisional_rate(sections: Sequence[Section], total_dur: float) -> float  # total_dur / words of body sections
  ```
  `find_start` walks the first 5 clips in order and, for each, the first `EDGE_SECTIONS` sections in order, returning the first pair scoring ≥ `MIN_SCORE`. `find_end` walks the last 5 clips from the end and, for each, the last `EDGE_SECTIONS` sections in reverse. If none match, it also tries the clip nearest `total_dur − words(section) × rate` for each of the last `EDGE_SECTIONS` sections (Review Focus 1). `confirm_candidates` returns the next 3 clips in `direction` plus the clip whose start is nearest `expected_time`, deduped and in range.

- [ ] **Step 1: Write the failing tests**, building transcripts with a `speak(text, start)` helper (copy it from `tests/test_transcribe.py`):

```python
def test_clips_from_gaps():
    assert clips_from_gaps([32.0, 48.8], 150.0) == [Clip(0, 0.0, 32.0), Clip(1, 32.0, 48.8), Clip(2, 48.8, 150.0)]

def test_find_start_skips_opening_credits():           # Review Focus 2
    heard = {0: speak("this is audible ann author fish and chips narrated by", 0), 1: speak(OPEN[0], 32)}
    a = find_start(heard, clips, SECTIONS)  # SECTIONS = title, copyright, ch1..3, about
    assert (a.clip, a.section) == (1, 2)

def test_find_start_none_when_nothing_matches(): assert find_start({0: speak("music", 0)}, clips, SECTIONS) is None

def test_find_end_matches_final_sections_in_reverse():
    heard = {5: speak(OPEN[2], 900)}
    assert (find_end(heard, clips6, SECTIONS, rate=0.1).section) == 4

def test_find_end_uses_expected_position_when_last_clips_are_mid_chapter():  # Review Focus 1
    # 8 clips, last 5 are scene breaks inside ch3; clip 2 starts ch3 near total_dur - 400*rate
    a = find_end(heard_with_ch3_at_clip2, clips8, SECTIONS, rate=0.1)
    assert (a.clip, a.section) == (2, 4)

def test_confirm_candidates_adds_expected_clip():
    assert confirm_candidates(Anchor(1, 2, .9), 1, clips10, expected_time=clips10[7].start) == [2, 3, 4, 7]

def test_confirms_and_next_narrated():
    assert confirms({3: speak(OPEN[1], 60)}, [2, 3], SECTIONS[3]) == 3
    assert next_narrated(SECTIONS_WITH_PART_PAGE, 2, 1) == 4   # 20-word "Part Two" at 3 is skipped
```

- [ ] **Step 2: Run, expect FAIL** (`ModuleNotFoundError: mp3_epub_m4b.anchors`). Run: `uv run pytest tests/test_anchors.py -v`
- [ ] **Step 3: Implement `anchors.py` and `chapter_gaps`** to the interfaces above.
- [ ] **Step 4: Run, expect PASS**
- [ ] **Step 5: Commit** `feat: pure start/end anchoring from edge-clip transcripts`

---

### Task 3: Pure placement, length check and questions

**Files:**
- Modify: `src/mp3_epub_m4b/anchors.py` (placement + length check)
- Create: `src/mp3_epub_m4b/questions.py`
- Test: `tests/test_anchors.py`, `tests/test_questions.py`

**Interfaces:**
- Consumes: Task 2, `chapters.SilenceIndex`, `chapters.MIN_EARLY_FRACTION`, `epub_extract.Chapter`, `state.Segment`.
- Produces (anchors.py):
  ```python
  MIN_SECTION_WORDS = 50; MAX_LENGTH_DEVIATION = 0.10; LENGTH_SLACK = 15.0
  def narrated_chapters(sections: Sequence[Section], start: int, end: int) -> list[Chapter]
      # sections[start..end], tiny ones merged into the next (words added, title of the larger one); n from 1
  def speaking_rate(chapters: Sequence[Chapter], start_time: float, end_time: float) -> float
  class Placement(NamedTuple): boundaries: dict[str, float]; low_confidence: list[int]; fallback: list[int]
  def place_chapters(chapters: Sequence[Chapter], silences: SilenceIndex, *, fixed: Mapping[str, float],
                     rejected: Sequence[float], total_dur: float, rate: float) -> Placement
      # fixed["1"] (start) and fixed[str(len(chapters))] (end) always present. For each other chapter: chain
      # find_gap from the previous boundary (min_offset = prev words × rate × MIN_EARLY_FRACTION), skipping gaps
      # within 0.5 s of a rejected time. A gap at or past the next fixed boundary means fallback: interpolate by
      # words between the surrounding fixed boundaries.
  def length_outliers(segments: Sequence[Segment], rate: float) -> list[int]   # chapter segment n's failing BOTH tolerances
  def closest_chapters(time: float, chapters: Sequence[Chapter], start_time: float, rate: float, k: int = 5) -> list[int]
      # chapter numbers whose expected start (start_time + cumulative words × rate) is nearest `time`
  ```
- Produces (questions.py):
  ```python
  QuestionKind = Literal["start", "end", "chapter"]
  class Choice(TypedDict): id: str; label: str; text: str   # id: "section:<i>" | "chapter:<n>" | "clip:next" | "clip:prev" | "not-a-boundary"
  class Question(TypedDict):
      kind: QuestionKind; prompt: str; clip: int; clip_start: float; clip_seconds: float
      clip_path: str; heard: str; choices: list[Choice]; suggested: str | None; chapter: NotRequired[int]
  class AnchoredProgress(TypedDict, total=False):
      heard: dict[str, list[list[Any]]]          # clip index -> [[start, end, text], ...]
      start: dict[str, Any] | None; end: dict[str, Any] | None   # {"clip", "section", "confirmed"}
      ask_clip: dict[str, int]                    # "start"/"end" -> clip currently being asked about
      fixed: dict[str, float]; rejected: list[float]
  class NeedsInput(RuntimeError):
      def __init__(self, question: Question) -> None
  def start_question(clip: Clip, heard: str, sections: Sequence[Section], suggested: int | None, clip_path: Path) -> Question   # first 5 sections + clip:next
  def end_question(clip: Clip, heard: str, sections: Sequence[Section], suggested: int | None, clip_path: Path) -> Question     # last 5 sections + clip:prev
  def chapter_question(n: int, clip: Clip, heard: str, chapters: Sequence[Chapter], candidates: Sequence[int], clip_path: Path) -> Question  # + not-a-boundary
  def apply_answer(progress: AnchoredProgress, question: Question, choice_id: str) -> None
      # start/end + section:i -> progress[kind] = {"clip": q.clip, "section": i, "confirmed": True}
      # start + clip:next / end + clip:prev -> ask_clip[kind] = q.clip ± 1 (progress[kind] cleared)
      # chapter + chapter:n -> fixed[str(n)] = q.clip_start ; not-a-boundary -> rejected.append(q.clip_start)
      # raises ValueError for an id not in question["choices"]
  ```
  Choice `text` is the section opening's first ~25 words. `label` is the title (or `"(untitled section N)"`). The prompt copy is: start: `"Where does the book start? Listen to the clip and pick the section the narrator is reading."`, end: `"Where does the last narrated section start? …"`, chapter: `f"Chapter {n} ({title}) is {actual} long but its text suggests about {expected}. Which section starts at {clock}?"`.

- [ ] **Step 1: Write the failing tests**

```python
def test_length_outliers_need_both_tolerances():
    # rate 0.1 s/word: expected 100 s, 100 s, 1000 s, 1000 s
    segs = [seg(1, dur=100, words=1000),    # exact
            seg(2, dur=112, words=1000),    # 12% off but only 12 s -> within slack
            seg(3, dur=1080, words=10000),  # 80 s off but only 8% -> within 10%
            seg(4, dur=1300, words=10000)]  # 30% and 300 s -> flagged
    assert length_outliers(segs, rate=0.1) == [4]
```
```python
def test_narrated_chapters_merges_part_pages(): assert [c["title"] for c in narrated_chapters(SECS_WITH_PART, 2, 6)] == ["First Light", "Tom & Jerry", "Fate = Doom; #3"]
def test_place_chapters_respects_fixed_and_rejected(): ...   # rejected 48.8 scene gap -> chapter 3 found at next gap
def test_place_chapters_interpolates_when_gap_overruns_fixed_end(): assert p.fallback == [3]
def test_apply_answer_start_section_confirms(): ... assert prog["start"] == {"clip": 1, "section": 2, "confirmed": True}
def test_apply_answer_next_clip_advances(): ... assert prog["ask_clip"]["start"] == 2 and prog.get("start") is None
def test_apply_answer_chapter_and_reject(): ... assert prog["fixed"]["3"] == 105.6; ... assert prog["rejected"] == [48.8]
def test_apply_answer_rejects_unknown_choice(): with pytest.raises(ValueError): apply_answer(prog, q, "section:99")
def test_question_round_trips_json(): assert json.loads(json.dumps(q)) == q
```

- [ ] **Step 2: Run, expect FAIL.** Run: `uv run pytest tests/test_anchors.py tests/test_questions.py -v`
- [ ] **Step 3: Implement** to the interfaces above.
- [ ] **Step 4: Run, expect PASS**
- [ ] **Step 5: Commit** `feat: anchored placement, ±10%/15 s length check and question model`

---

### Task 4: `Pipeline` anchored mode

**Files:**
- Modify: `src/mp3_epub_m4b/run.py` (`ALIGN_BY_CHOICES += ("anchored",)`, fingerprint, `stage_align` dispatch, `_align_anchored`, `answer`, `_ask`)
- Modify: `src/mp3_epub_m4b/state.py` (`AlignProgress.anchored: NotRequired[AnchoredProgress]`, `PipelineState.pending_question: NotRequired[Question]`, `PipelineState.job: NotRequired[dict[str, Any]]`)
- Modify: `src/mp3_epub_m4b/options.py` (`AlignMode.anchored`; `Options.align_by` default `"anchored"`; help text)
- Test: `tests/test_anchored_pipeline.py`

**Interfaces:**
- Consumes: Tasks 1–3.
- Produces:
  ```python
  class Pipeline:
      def answer(self, choice_id: str) -> None   # apply_answer on state["align"]["anchored"], pop pending_question, save
      # stage_align raises questions.NeedsInput (after saving state["pending_question"]) when a person must decide
  ```
  Fingerprint: add `align_by` (and `whisper_model`) for `"anchored"` as is done for `"transcribe"`.

`_align_anchored(budget)` order (each step checkpoints and respects `budget`, returning `False` to continue):
1. Clips from `self._get_silences().chapter_gaps()`. Transcribe `edge_clips` (10 s from each clip's start) into `anchored["heard"]`, skipping cached ones.
2. Start: use `anchored["start"]`, or `find_start`, or ask at `ask_clip["start"]` (default 0). If not confirmed: section `s1 = next_narrated(start.section, 1)`, `expected = clip.start + words(start.section) × provisional_rate`, transcribe `confirm_candidates` on demand, `confirms` → mark confirmed, else ask (suggest the found section).
3. End: the same with `find_end`, direction −1, `ask_clip["end"]` default last clip.
4. `chapters = narrated_chapters(sections, start.section, end.section)`; write it to `state["epub"]["chapters"]` (so `build_segments`, status and the chapter table work unchanged). `fixed["1"] = start clip.start` (0.0 if clip 0), `fixed[str(len)] = end clip.start`. `rate = speaking_rate(...)`.
5. `place_chapters` → `align["boundaries"/"low_confidence"/"fallback"]`. Build segments (with `--detect-intro`, audio before `fixed["1"]` becomes the intro via `build_segments`; without it set `boundaries["1"] = 0.0`). For the first `length_outliers` chapter `n`: ask a `chapter_question` at the start of chapter `n+1` (or `n` if it is last), with `closest_chapters` as candidates.
6. No outliers → `align["next"] = len + 1`, then `_finish_align` (outro logic unchanged) → `assemble`.

`_ask(question)`: cut the 10 s clip to `work_dir/questions/<kind>-<clip>.wav` (16 kHz mono via ffmpeg), save `state["pending_question"]`, raise `NeedsInput`. `run()` must raise `NeedsInput` immediately if `pending_question` is set (never advance past it).

- [ ] **Step 1: Write the failing integration tests** (`@pytest.mark.ffmpeg`). They use an `anchored_book` fixture (synthetic audio + `make_epub_with_matter`) and monkeypatch `mp3_epub_m4b.run.transcribe_window` with a fake that returns `speak(ANCHORED_OPENINGS[i], start)` when `start` is within 3 s of `expected_boundaries()[i]`, and `speak("tone", start)` otherwise. They also patch `find_whisper` to return `("whisper", Path("m"))`.

```python
def test_anchored_places_three_chapters_without_asking(anchored_book, tmp_path, fake_whisper):
    p = make_pipeline(anchored_book, tmp_path, align_by="anchored"); run_until(p, "assemble")
    assert [c["title"] for c in p.state["epub"]["chapters"]] == ["First Light", "Tom & Jerry", "Fate = Doom; #3"]
    assert [round(p.state["align"]["boundaries"][k]) for k in "123"] == [round(t) for t in expected_boundaries()]
    assert "pending_question" not in p.state

def test_anchored_asks_for_start_when_nothing_matches(..., fake_whisper_silent):
    with pytest.raises(NeedsInput) as e: run_until(p, "assemble")
    q = e.value.question; assert q["kind"] == "start" and len([c for c in q["choices"] if c["id"].startswith("section:")]) == 5
    assert Path(q["clip_path"]).is_file() and p.state["stage"] == "align"

def test_answer_resumes_and_never_reaches_assemble_while_pending(...):
    # run() with a pending question raises again; after p.answer("section:2") (and further answers if asked) stage reaches assemble

def test_conflicting_chapter_answer_reasks_not_loops(...):   # Review Focus 3: answer chapter:3 at a time that squeezes chapter 2 -> a new chapter question, bounded

def test_resume_reuses_cached_transcripts(..., counting_whisper):  # Review Focus 4
    # interrupt after step 1 (budget=0), new Pipeline on same work dir -> whisper call count unchanged for edge clips
```

- [ ] **Step 2: Run, expect FAIL.** Run: `uv run pytest tests/test_anchored_pipeline.py -v`
- [ ] **Step 3: Implement** the state, options and `run.py` changes above.
- [ ] **Step 4: Run the full suite, expect PASS** (the existing silence/transcribe tests must be unchanged): `uv run pytest`
- [ ] **Step 5: Commit** `feat: anchored alignment mode in the pipeline`

---

### Task 5: Asking in the terminal, CLI defaults, batch and `resolve`

**Files:**
- Create: `src/mp3_epub_m4b/prompt.py`
- Modify: `src/mp3_epub_m4b/converter.py` (`run_pipeline` handles `NeedsInput`; store `state["job"]`; `Options.interactive`)
- Modify: `src/mp3_epub_m4b/options.py` (`Options.interactive: bool = False`)
- Modify: `src/mp3_epub_m4b/cli.py` (default `AlignMode.anchored`; whisper check; `convert` exit 3; batch summary; `resolve` command)
- Test: `tests/test_prompt.py`, `tests/test_cli.py`

**Interfaces:**
- Consumes: `Question`, `NeedsInput`, `Pipeline.answer`, `apply_answer` (Tasks 3–4).
- Produces:
  ```python
  # prompt.py
  def play_clip(path: Path) -> None          # `afplay` on macOS if present, else `ffplay -nodisp -autoexit -loglevel error`; silently no-op if neither
  def ask(question: Question, *, input_fn: Callable[[str], str] = input, play: Callable[[Path], None] = play_clip) -> str
      # prints prompt, "Heard: …", numbered choices (suggested marked "(suggested)"), plays clip;
      # accepts a number, "r" (replay), or Enter (= suggested when set); returns the choice id
  # converter.py
  def run_pipeline(..., ) -> PipelineState   # catches NeedsInput: if o.interactive -> live.stop(); choice = prompt.ask(q); pipeline.answer(choice); live.start(); continue
                                             # else re-raises
  # cli.py
  EXIT_NEEDS_INPUT = 3
  @app.command() def resolve(book: Path) -> None   # audio or epub; reads <work>/state.json; requires state["job"]; asks pending question(s) then convert_book/plan_book with the stored job (interactive=True)
  ```
  `state["job"] = {"audio": str, "epub": str, "out_dir": str, "dry_run": bool, "options": dataclasses.asdict(o) minus readaloud→ stored as dict|None}`, written by `convert_book` / `plan_book` before running. `convert` sets `interactive = sys.stdin.isatty()`. `batch` always sets `interactive=False`, catches `NeedsInput` per book (`logger.warning(f"{label}: waiting for input — run: mp3-epub-m4b resolve {audio}")`), continues, and lists waiting books at the end (exit 3 if any are waiting and none failed). `_require_tools(align_by)` calls `find_whisper` for `anchored`/`transcribe`.

- [ ] **Step 1: Write the failing tests**

```python
def test_ask_numbers_choices_and_uses_suggested_on_enter(question):
    assert ask(question, input_fn=lambda _: "", play=lambda p: None) == question["suggested"]
def test_ask_replay_then_pick(question):
    answers = iter(["r", "2"]); plays = []
    assert ask(question, input_fn=lambda _: next(answers), play=plays.append) == question["choices"][1]["id"]; assert len(plays) == 2
def test_ask_reprompts_on_bad_input(question): ...  # "9" then "1"
def test_convert_non_tty_exits_3_with_question_saved(...):   # Review Focus 5 (CliRunner gives non-tty stdin; fake whisper hears nothing)
    result = runner.invoke(app, ["convert", str(mp3), str(epub), "--work-dir", str(w), "--dry-run"])
    assert result.exit_code == 3 and "pending_question" in read_state(state_path)
def test_resolve_answers_and_finishes_dry_run(...):  # monkeypatch prompt.ask to return the suggested/first section id
def test_default_align_by_is_anchored_and_requires_whisper(monkeypatch):  # find_whisper raises -> exit 2, message mentions "--align-by silence"
def test_batch_continues_past_waiting_book(...): assert "waiting for input" in result.output and result.exit_code == 3
```

- [ ] **Step 2: Run, expect FAIL.** Run: `uv run pytest tests/test_prompt.py tests/test_cli.py -v`
- [ ] **Step 3: Implement** to the interfaces above.
- [ ] **Step 4: Run the full suite, expect PASS**
- [ ] **Step 5: Commit** `feat: interactive questions in the terminal, resolve command, anchored default`

---

### Task 6: Status shows "waiting for input"

**Files:**
- Modify: `src/mp3_epub_m4b/status.py` (`BookStatus.waiting_for_input: bool = False`, `question: str = ""`; set in `read_status`; render in `render_status`/`render_overview`)
- Test: `tests/test_status.py`

**Interfaces:**
- Produces: `BookStatus.waiting_for_input`, `BookStatus.question` (the question's `prompt`).

- [ ] **Step 1: Failing test:** a state with `pending_question` gives `st.waiting_for_input is True`, and the rendered text contains `"waiting for input"` and `"mp3-epub-m4b resolve"`.
- [ ] **Step 2: Run, expect FAIL.** `uv run pytest tests/test_status.py -v`
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run, expect PASS**
- [ ] **Step 5: Commit** `feat: show books waiting for input in status`

---

### Task 7: MCP `pending_question` / `answer_question`

**Files:**
- Modify: `src/mp3_epub_m4b/mcp_server.py`
- Test: `tests/test_mcp_server.py`

**Interfaces:**
- Consumes: `Question`, `apply_answer`, `read_state`/`write_state`, the job record's `cmd`.
- Produces:
  ```python
  @mcp.tool() def pending_question(book: str) -> dict[str, Any]
      # {"waiting": False} or {"waiting": True, **question} (clip_path absolute; heard; choices; suggested)
  @mcp.tool() def answer_question(book: str, choice: str) -> dict[str, Any]
      # ToolError if the job is running, if nothing is pending, or if the choice is invalid;
      # apply_answer + pop pending_question + write_state, then relaunch the recorded job cmd (same Popen as start_conversion);
      # returns {"pid", "log"}
  conversion_status(...)   # adds "waiting_for_input": bool and "question": prompt text when waiting
  start_conversion(..., align_by: str = "anchored")   # passes --align-by
  ```
  Move the `Popen` block of `start_conversion` into `_launch(cmd: list[str], epub: Path, dry_run: bool) -> dict[str, Any]`, shared by both.

- [ ] **Step 1: Failing tests:** update `test_registered_tools` to the 6 names; `pending_question` on a state with/without a question; `answer_question` writes the answer (`state["align"]["anchored"]["start"]["section"] == 2`) and relaunches (monkeypatch `subprocess.Popen`, assert the cmd equals the recorded one); an invalid choice raises `ToolError`; `conversion_status` reports `waiting_for_input`; `start_conversion` cmd contains `["--align-by", "anchored"]`.
- [ ] **Step 2: Run, expect FAIL.** `uv run pytest tests/test_mcp_server.py -v`
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run, expect PASS**
- [ ] **Step 5: Commit** `feat: MCP tools to read and answer a waiting question`

---

### Task 8: The `audiobook-mp3-to-m4b` skill and docs

**Files:**
- Create: `skill/audiobook-mp3-to-m4b/SKILL.md`
- Modify: `README.md`, `docs/convert.md`, `docs/options.md`, `docs/batch.md`, `docs/troubleshooting.md`, `docs/development.md` (module list: `anchors.py`, `questions.py`, `prompt.py`)

**Skill content** (frontmatter `name`/`description` kept from the current synced copy, with its triggers):
- How it works: start and end of the book are found and confirmed by what the narrator says, every chapter is length-checked (±10% or ±15 s), and nothing is encoded until all checks pass.
- With the MCP server: `find_pairs` → `start_conversion` (use `dry_run=true` first for a preview) → poll `conversion_status`. When `waiting_for_input` is true: call `pending_question`, show the person the prompt, what was heard and the numbered choices (mark the suggested one), and open `clip_path` in their audio player (`open` on macOS, `xdg-open` on Linux) because Claude can't play audio. Ask with `AskUserQuestion` when available, then call `answer_question` and keep polling. After `DONE`, call `verify_m4b`.
- Without MCP: run `mp3-epub-m4b convert AUDIO [EPUB]` in a terminal the person can answer in, or `mp3-epub-m4b resolve AUDIO` for books left waiting by `batch`.
- Keep: intro/outro caveats, verification checklist, cover-art note. Drop: the old `scripts/run.py` instructions and hand-patching `state.json`.
- Install note at the top of the file (as an HTML comment): upload this file to claude.ai → Settings → Skills to replace the synced copy.

- [ ] **Step 1: Write the skill file and doc updates.**
- [ ] **Step 2: Verify:** `uv run pytest && uv run mypy src tests && uv run ruff check . && uv run ruff format --check .`. Expect everything to pass. Then `grep -rn "align-by" README.md docs/*.md` shows `anchored` as the default.
- [ ] **Step 3: Commit** `docs: anchored default, resolve command, and updated Claude skill`
